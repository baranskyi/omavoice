pragma ComponentBehavior: Bound

// The floating voice panel.
//
// A layer-shell surface on the overlay layer, dimming the desktop and holding a
// card in the middle of the screen. The shape follows omarchy.emojis: the host
// injects `shell` and `manifest`, calls open(payloadJson) on summon and close()
// on hide, and reads `opened` back to keep its own bookkeeping straight.
//
// The panel owns the microphone's lifetime. Opening it starts a session,
// closing it ends one — a voice assistant that listens while you are not
// looking at it is not a thing worth building.

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  // Injected by the shell host on load.
  property var shell: null
  property var manifest: null

  // The host reads this back; without it `toggle` desyncs after the first open.
  property bool opened: false

  property bool settingsOpen: false
  property string keyError: ""

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.baranskyi.omavoice"

  // The interface is English because the desktop is, and because the plugin is
  // meant to be installable by anyone. The conversation language is a separate
  // thing entirely: it follows whoever is speaking.
  readonly property string statusText: {
    if (client.errorText) return client.errorText
    if (!client.connected) return "Daemon not running"
    if (!client.hasKey) return "No API key — open settings"
    switch (client.voiceState) {
    case "listening": return client.userText ? "Listening…" : "Speak"
    case "thinking": return "Looking it up"
    case "speaking": return "Answering"
    case "error": return "Error"
    default: return "Ready"
    }
  }

  function open(payloadJson) {
    root.opened = true
    root.settingsOpen = false
    client.wanted = true
    // Not cleared here: reopening after backgrounding should show the work
    // that was going on, and the daemon replays the last stretch of it. A
    // genuinely new conversation is N, or a session that starts from nothing.
    if (!client.connected || !client.backgrounded) client.clearConversation()
    // The socket connects asynchronously; startSession is retried from
    // onConnectedChanged if we beat it here.
    // Fires only when the socket is already up; otherwise onConnectedChanged
    // does it.
    client.startSession()
    client.foreground()
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  // Host-initiated close. It already knows, so do not tell it back.
  // The session survives: an agent that is mid-thought should finish, and the
  // answer should still be spoken. The microphone stops, so nothing is
  // listening to a room with no window on screen.
  function close() {
    root.opened = false
    client.background()
    client.wanted = false
  }

  // User-initiated close (Escape, click on the scrim). Tell the host so its
  // openPanelIds map stays in step and the next toggle opens rather than closes.
  function dismiss() {
    root.opened = false
    client.background()
    client.wanted = false
    if (shell && typeof shell.hide === "function") shell.hide(root.pluginId)
  }

  // The deliberate ending: hang up, drop the session, release everything.
  function endSession() {
    root.opened = false
    client.stopSession()
    client.wanted = false
    if (shell && typeof shell.hide === "function") shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  // Only ever hand xdg-open an argv vector, and only for schemes we recognise:
  // both the URL and the path come from a model summarising untrusted content.
  function openUrl(url) {
    const value = String(url || "")
    if (!/^https?:\/\//.test(value)) return
    Util.execArgv(["xdg-open", value])
  }

  function openPath(path) {
    const value = String(path || "")
    if (!value.startsWith("/")) return
    Util.execArgv(["xdg-open", value])
  }

  // Qt renders MarkdownText's images by fetching them. Nothing in a voice
  // answer needs an image, and the URLs come from summarised web content, so
  // they are stripped rather than trusted.
  function safeMarkdown(text) {
    return String(text || "").replace(/!\[[^\]]*\]\([^)]*\)/g, "")
  }

  Client {
    id: client
    // Both commands are re-sent on connect, not just at open(): the socket
    // connects asynchronously, so anything sent from open() lands before
    // there is a socket to send it on and is silently dropped. That is what
    // left the microphone off after returning from the background.
    onConnectedChanged: {
      if (connected && root.opened) {
        startSession()
        foreground()
      }
    }
  }

  // The bar widget reads state through the host, so expose it by name.
  readonly property string voiceState: client.voiceState
  readonly property bool daemonConnected: client.connected

  IpcHandler {
    target: "io.github.baranskyi.omavoice"
    function open(): void { root.open("{}") }
    function close(): void { root.dismiss() }
    function toggle(): void { root.toggle() }
    function settings(): void { root.settingsOpen = !root.settingsOpen }
    function reset(): void { client.reset() }
    function state(): string { return client.voiceState }
    function backend(): string { return client.backend }
  }

  SettingsWindow {
    id: settingsWindow
    open: root.settingsOpen && root.opened
    hasKey: client.hasKey
    voices: client.voiceCatalogue
    currentVoice: client.voice
    backend: client.backend
    keyError: root.keyError
    onClosed: root.settingsOpen = false
    onVoicePicked: function (name) { client.setVoice(name) }
    onBackendPicked: function (name) { client.setBackend(name) }
    onKeySubmitted: function (key) {
      root.keyError = ""
      client.setApiKey(key)
    }
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    // Themes may leave menu.scrim fully transparent — the emoji picker sits on
    // a small card where that reads fine. This panel is the focus of a
    // conversation, so it dims regardless of what the theme asked for.
    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim.a > 0.05
        ? Color.menu.scrim
        : Qt.rgba(Color.background.r, Color.background.g, Color.background.b, 0.55)
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      // Top-centre: this is a heads-up display over whatever you are doing,
      // and the middle of the screen is where you are looking at that.
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.top: parent.top
      anchors.topMargin: Style.space(64)
      width: Style.space(560)
      height: Math.min(
        Style.space(620),
        body.implicitHeight + card.contentTopInset + card.contentBottomInset
      )
      radius: Style.cornerRadius
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      // Swallow clicks so hitting the card does not dismiss the panel.
      MouseArea { anchors.fill: parent; onClicked: {} }

      Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

      Item {
        id: keyCatcher
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function (event) {
          if (event.key === Qt.Key_Escape) {
            // Step away without ending anything: the agent keeps working, the
            // answer still gets spoken, and the bar icon keeps the state.
            // Interrupting a runaway answer is I; ending the session is Q.
            root.dismiss()
            event.accepted = true
          } else if (event.key === Qt.Key_I) {
            // A letter, not Ctrl+Space: that combination is voxtype's
            // dictation toggle on this desktop, and a panel that grabs the
            // keyboard exclusively would quietly swallow it.
            client.cancel()
            event.accepted = true
          } else if (event.key === Qt.Key_Q) {
            // End it for real, as opposed to Escape which only steps away.
            root.endSession()
            event.accepted = true
          } else if (event.key === Qt.Key_N) {
            // New conversation: forgets the Realtime history, the agent's
            // thread and everything on screen. Bare N because the panel has
            // no text entry to compete with.
            client.reset()
            event.accepted = true
          } else if (event.key === Qt.Key_Tab) {
            client.setBackend(client.backend === "codex" ? "claude" : "codex")
            event.accepted = true
          }
        }

        Column {
          id: body
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.panelGap

          // --- waveform -------------------------------------------------
          Item {
            width: parent.width
            height: wave.implicitHeight + Style.space(16)

            Waveform {
              id: wave
              anchors.centerIn: parent
              width: parent.width
              height: Style.spaceReal(120)
              voiceState: client.connected ? client.voiceState : "error"
              level: client.level
              bands: client.bands
              accent: client.voiceState === "error" || !client.connected ? Color.urgent : Color.accent
              dim: Color.menu.text
            }
          }

          // --- status line ----------------------------------------------
          // A Row would let a long status (an API error, say) push past the
          // card's edge, so the text gets an explicit width and wraps.
          Item {
            width: parent.width
            height: Math.max(statusText.implicitHeight, backendTag.implicitHeight)

            Rectangle {
              id: statusDot
              anchors.left: parent.left
              anchors.top: parent.top
              anchors.topMargin: Math.round((statusText.implicitHeight - height) / 2)
              width: Style.space(8)
              height: Style.space(8)
              radius: width / 2
              color: client.voiceState === "listening" ? Color.accent
                   : client.voiceState === "error" || !client.connected ? Color.urgent
                   : Color.menu.text
              opacity: client.voiceState === "idle" ? 0.4 : 1

              SequentialAnimation on opacity {
                running: client.voiceState === "listening"
                loops: Animation.Infinite
                NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutQuad }
                NumberAnimation { to: 1.0; duration: 700; easing.type: Easing.InOutQuad }
              }
            }

            Text {
              id: statusText
              anchors.left: statusDot.right
              anchors.leftMargin: Style.spacing.sm
              anchors.right: backendTag.left
              anchors.rightMargin: Style.spacing.sm
              anchors.top: parent.top
              text: root.statusText
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              maximumLineCount: 3
              elide: Text.ElideRight
              color: client.voiceState === "error" || !client.connected ? Color.urgent : Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              opacity: 0.9
            }

            Row {
              id: backendTag
              anchors.right: parent.right
              anchors.top: parent.top
              spacing: Style.spaceReal(8)

              AgentBadge {
                anchors.verticalCenter: parent.verticalCenter
                agent: client.backend
                TapHandler {
                  onTapped: client.setBackend(client.backend === "codex" ? "claude" : "codex")
                }
              }

            }
          }

          // --- the waterfall ----------------------------------------------
          EventLog {
            width: parent.width
            visible: client.events.count > 0
            model: client.events
            pendingSince: client.pendingSince
            accent: Color.accent
            base: Color.menu.text
            urgent: Color.urgent
          }

          PanelSeparator {
            width: parent.width
            visible: client.markdown !== "" || client.assistantText !== ""
          }

          // --- the answer --------------------------------------------------
          Flickable {
            width: parent.width
            height: Math.min(Style.space(240), answer.implicitHeight)
            contentHeight: answer.implicitHeight
            clip: true
            interactive: contentHeight > height
            visible: answer.text !== ""
            boundsBehavior: Flickable.StopAtBounds

            Text {
              id: answer
              width: parent.width
              // The panel is a glance, not a document: markdown while the
              // agent's structured answer is in, the live spoken transcript
              // before that.
              text: client.markdown !== ""
                ? root.safeMarkdown(client.markdown)
                : client.assistantText
              textFormat: client.markdown !== "" ? Text.MarkdownText : Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              onLinkActivated: function (link) { root.openUrl(link) }
            }
          }

          // --- buttons ------------------------------------------------------
          Flow {
            width: parent.width
            spacing: Style.spacing.sm
            visible: (client.links && client.links.length > 0) || (client.files && client.files.length > 0)

            Repeater {
              model: client.links
              Button {
                required property var modelData
                text: String(modelData.label || "Ссылка")
                iconText: "↗"
                tooltipText: String(modelData.url || "")
                foreground: Color.menu.text
                accent: Color.accent
                fontFamily: Style.font.family
                bordered: true
                onClicked: root.openUrl(modelData.url)
              }
            }

            Repeater {
              model: client.files
              Button {
                required property var modelData
                text: String(modelData.label || "Файл")
                iconText: "\udb80\ude14"  // file, as a surrogate pair
                tooltipText: String(modelData.path || "")
                foreground: Color.menu.text
                accent: Color.accent
                fontFamily: Style.font.family
                bordered: true
                onClicked: root.openPath(modelData.path)
              }
            }
          }

          // --- footer --------------------------------------------------------
          // The gear sits here, away from the agent badge: it configures the
          // whole plugin, not the backend it happened to be standing next to.
          Item {
            width: parent.width
            height: Math.max(hint.implicitHeight, gear.implicitHeight)

            Text {
              id: hint
              anchors.left: parent.left
              anchors.right: gear.left
              anchors.rightMargin: Style.spaceReal(8)
              anchors.verticalCenter: parent.verticalCenter
              text: client.connected
                ? "Esc — background · I — interrupt · N — new · Q — end"
                : "Start the daemon:  systemctl --user start omavoice"
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              opacity: 0.35
            }

            Text {
              id: gear
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: "\u2699"
              textFormat: Text.PlainText
              color: root.settingsOpen ? Color.accent : Color.menu.text
              opacity: root.settingsOpen ? 1 : (gearHover.hovered ? 0.9 : 0.4)
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle

              Behavior on opacity { NumberAnimation { duration: 140 } }

              HoverHandler { id: gearHover }
              TapHandler { onTapped: root.settingsOpen = true }
            }
          }
        }
      }
    }
  }
}
