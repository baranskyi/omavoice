pragma ComponentBehavior: Bound

// The socket to omavoiced, and the state it pushes.
//
// Everything the panel and the bar icon draw comes from here. QML never talks
// to OpenAI, never touches audio, never runs the agent — Quickshell has no
// WebSocket module and the shell process is shared with the whole bar, so
// anything slow or networked would freeze the desktop. This is a socket and a
// few properties.
//
// Reconnection recreates the Socket rather than re-setting `connected`: a
// failed connect leaves Quickshell holding a dead QLocalSocket, and asking it
// to connect again is a no-op. Same trick quickshell.spotify uses.

import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  // Set true while the panel is open; drives connect/disconnect.
  property bool wanted: false

  readonly property bool connected: socketLoader.item ? socketLoader.item.connected === true : false

  // --- pushed state ---------------------------------------------------------
  property string voiceState: "idle"          // idle | listening | thinking | speaking | error
  property real level: 0                 // 0..1 loudness
  property var bands: [0, 0, 0, 0]       // per-band energy, so the figure reads timbre
  property string backend: "codex"
  property string voice: ""
  property bool hasKey: false
  property bool backgrounded: false
  property var voiceCatalogue: []
  property string userText: ""           // what the daemon heard us say
  property string queryText: ""          // what the assistant asked the agent
  property string assistantText: ""      // what it is saying back, streamed
  property string markdown: ""
  property var links: []
  property var files: []
  property string errorText: ""

  // --- audio path -----------------------------------------------------------
  // Which microphone the daemon is using, what it could use, and whether the
  // choice is being made for us. Empty `audioInput` means "follow the system",
  // which is the default and picks a headset when one is worn.
  signal barged()

  property var audioSources: []
  property string audioInput: ""
  property string audioResolved: ""
  property bool audioHeadphones: false

  // --- what has been agreed to ----------------------------------------------
  // The folder the agent works in, and which agents have been allowed to work
  // at all. `accessNeeded` is the daemon's own answer to "would a question be
  // refused right now" — asked once there rather than recomputed here, so the
  // panel and the thing doing the refusing cannot disagree.
  property string workspace: ""
  property var consented: []
  property var unrestricted: []
  property bool accessNeeded: false
  property var folderChoices: []

  function backendAllowed(name) {
    return root.consented.indexOf(String(name)) >= 0
  }

  function backendUnrestricted(name) {
    return root.unrestricted.indexOf(String(name)) >= 0
  }

  signal answered()

  // One line of the agent's working, as it happens. Not stored: the panel
  // shows it and lets it go, which is the only honest thing to do with a
  // window onto something that is still running.
  signal traced(string text)

  // The waterfall. Newest first, because the interesting line is always the
  // one that just happened, and it should not move once it has been read.
  property alias events: eventModel
  readonly property int eventLimit: 14

  ListModel { id: eventModel }

  // Set while a question is out with the agent, so the newest line can run a
  // clock instead of just sitting there. Waiting is much easier to tolerate
  // when you can see it being counted.
  property real pendingSince: 0
  readonly property bool waiting: pendingSince > 0

  function pushEvent(message) {
    const kind = String(message.kind || "")
    let detail = ""
    if (kind === "result") {
      const parts = []
      if (message.backend) parts.push(String(message.backend))
      if (message.seconds !== undefined) parts.push(Number(message.seconds).toFixed(1) + "s")
      if (message.links) parts.push(message.links + "↗")
      if (message.files) parts.push(message.files + "⎘")
      detail = parts.join(" · ")
    } else if (kind === "agent") {
      detail = String(message.backend || "")
    }

    // The daemon reports a barge-in as an event like any other; the panel wants
    // it as a moment, so it can be felt rather than read.
    if (kind === "barge") root.barged()

    if (kind === "agent") root.pendingSince = Date.now()
    else if (kind === "result" || kind === "error") root.pendingSince = 0

    eventModel.insert(0, {
      kind: kind,
      text: String(message.text || ""),
      detail: detail,
      at: Number(message.at) || 0
    })
    while (eventModel.count > root.eventLimit) eventModel.remove(eventModel.count - 1)
  }

  readonly property string socketPath: {
    const runtime = Quickshell.env("XDG_RUNTIME_DIR")
    return String(runtime || "/tmp") + "/omavoice.sock"
  }

  // --- commands -------------------------------------------------------------

  function send(payload) {
    const socket = socketLoader.item
    if (!socket || !socket.connected) return false
    socket.write(JSON.stringify(payload) + "\n")
    socket.flush()
    return true
  }

  function startSession() { return send({ cmd: "start" }) }
  function stopSession() { return send({ cmd: "stop" }) }
  function background() { return send({ cmd: "background" }) }
  function foreground() { return send({ cmd: "foreground" }) }
  function cancel() { return send({ cmd: "cancel" }) }
  function reset() {
    // Cleared locally as well as remotely so the panel empties on the
    // keystroke rather than a reconnect later.
    clearConversation()
    return send({ cmd: "reset" })
  }
  function setInput(name) { return send({ cmd: "input", value: String(name || "") }) }
  function setBackend(name) { return send({ cmd: "backend", value: String(name) }) }
  function setVoice(name) { return send({ cmd: "voice", value: String(name) }) }
  function setApiKey(key) { return send({ cmd: "apikey", value: String(key) }) }
  function setWorkspace(path) { return send({ cmd: "workspace", value: String(path || "") }) }
  function setConsent(name, granted) {
    return send({ cmd: "consent", backend: String(name), granted: granted === true })
  }
  function setUnrestricted(name, granted) {
    return send({ cmd: "unrestrict", backend: String(name), granted: granted === true })
  }
  function askAccess() { return send({ cmd: "access", id: 7 }) }

  function clearConversation() {
    eventModel.clear()
    pendingSince = 0
    userText = ""
    queryText = ""
    assistantText = ""
    markdown = ""
    links = []
    files = []
    errorText = ""
  }

  // --- inbound --------------------------------------------------------------

  function handleLine(line) {
    if (!line) return
    let message
    try {
      message = JSON.parse(line)
    } catch (e) {
      return
    }
    if (!message || typeof message !== "object") return

    switch (message.type) {
    case "state":
      root.voiceState = String(message.state || "idle")
      if (root.voiceState !== "error") root.errorText = ""
      break
    case "level":
      root.level = Number(message.rms) || 0
      if (Array.isArray(message.bands)) root.bands = message.bands
      break
    case "backend":
      root.backend = String(message.backend || root.backend)
      break
    case "voice":
      root.voice = String(message.voice || root.voice)
      break
    case "background":
      root.backgrounded = message.background === true
      break
    case "key":
      root.hasKey = message.hasKey === true
      break
    case "voices":
      if (Array.isArray(message.voices)) root.voiceCatalogue = message.voices
      break
    case "access":
      root.workspace = String(message.workspace || "")
      root.consented = Array.isArray(message.consented) ? message.consented : []
      root.unrestricted = Array.isArray(message.unrestricted) ? message.unrestricted : []
      root.accessNeeded = message.needed === true
      if (Array.isArray(message.folders)) root.folderChoices = message.folders
      break
    case "audio":
      root.audioInput = String(message.input || "")
      root.audioResolved = String(message.resolved || "")
      root.audioHeadphones = message.headphones === true
      if (Array.isArray(message.sources)) root.audioSources = message.sources
      break
    case "transcript":
      if (message.role === "user") {
        // The user's line arrives whole, and replaces the previous turn.
        root.userText = String(message.text || "")
        if (message.final === true) {
          root.assistantText = ""
          root.queryText = ""
          root.markdown = ""
          root.links = []
          root.files = []
        }
      } else {
        // The assistant's arrives as deltas until `final`.
        root.assistantText = message.final === true
          ? String(message.text || root.assistantText)
          : root.assistantText + String(message.text || "")
      }
      break
    case "event":
      root.pushEvent(message)
      break
    case "reset":
      root.clearConversation()
      break
    case "query":
      // What the assistant went looking for, in its words — shown as the
      // panel's own note, never as the user's line.
      root.queryText = String(message.text || "")
      break
    case "trace":
      root.traced(String(message.text || ""))
      break
    case "answer":
      root.markdown = String(message.markdown || "")
      root.links = Array.isArray(message.links) ? message.links : []
      root.files = Array.isArray(message.files) ? message.files : []
      root.answered()
      break
    case "error":
      root.errorText = String(message.message || "")
      break
    }
  }

  Component {
    id: socketComponent

    Socket {
      path: root.socketPath
      connected: true
      parser: SplitParser {
        splitMarker: "\n"
        onRead: function (line) { root.handleLine(line) }
      }
      onConnectionStateChanged: {
        if (connected) {
          root.reconnectAttempt = 0
          // The daemon replays its last state on connect, so there is nothing
          // to ask for here.
        } else if (root.wanted) {
          root.voiceState = "idle"
          root.level = 0
        }
      }
    }
  }

  property int reconnectAttempt: 0

  Loader {
    id: socketLoader
    active: false
    sourceComponent: socketComponent
  }

  Timer {
    id: reconnectTimer
    interval: Math.min(2000, 200 + root.reconnectAttempt * 250)
    repeat: true
    triggeredOnStart: true
    running: root.wanted && !root.connected
    onTriggered: {
      root.reconnectAttempt = Math.min(8, root.reconnectAttempt + 1)
      socketLoader.active = false
      socketLoader.active = true
    }
  }

  onWantedChanged: {
    if (!wanted) {
      socketLoader.active = false
      root.level = 0
      root.bands = [0, 0, 0, 0]
    }
  }
}
