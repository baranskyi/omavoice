pragma ComponentBehavior: Bound

// The microphone in the bar: what the assistant is doing, at a glance.
//
// It keeps its own thin socket to the daemon rather than reading state off the
// overlay, because the overlay only exists while the panel is open and the bar
// should still show that a session is live. Clicking it toggles the panel
// through the host, the same path the hotkey takes.

import QtQuick
import QtQuick.Effects
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.baranskyi.omavoice"

  readonly property string voiceState: client.connected ? client.voiceState : "offline"
  readonly property bool showLabel: {
    // Same default as the manifest: without a stored value the fallback here is
    // what actually decides, and a mismatch would leave the setting looking on
    // and behaving off.
    const value = setting("showLabel", true)
    if (typeof value === "boolean") return value
    return ["true", "1", "yes", "on"].indexOf(String(value).trim().toLowerCase()) !== -1
  }


  StateHues { id: hues }

  // Whether the microphone is open. Not "is the panel on screen" — hiding the
  // panel stops nothing, so the bar is the only thing left that can say a
  // microphone is live, and it had better say it.
  readonly property bool micOpen: hues.microphoneIsOpen(root.voiceState)

  readonly property color barGround: root.bar && root.bar.barBackground
    ? root.bar.barBackground
    : Color.background

  readonly property color tint: {
    if (root.voiceState === "error") return Color.urgent
    if (!root.micOpen) return root.bar ? root.bar.barForeground : Color.foreground
    // The same greens the panel uses, so one fact has one colour.
    return hues.colorFor(root.voiceState, root.barGround, Color.accent)
  }

  // The privacy light. It breathes rather than sitting still, because a steady
  // glow in a bar full of steady icons stops being noticed within a day — and
  // the whole point is that it should still be noticed on the hundredth.
  property real glow: 0

  NumberAnimation on glow {
    running: root.micOpen
    from: 0.45
    to: 1.0
    duration: 1400
    easing.type: Easing.InOutSine
    loops: Animation.Infinite
    onRunningChanged: if (!running) root.glow = 0
  }

  // Ticks only while the agent is out, which is the one state worth counting:
  // waiting ten seconds is fine when the number is moving, and the same ten
  // seconds against a still icon read as a hang.
  property real now: 0

  Timer {
    interval: 1000
    repeat: true
    running: client.pendingSince > 0
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  readonly property int waitedSeconds: client.pendingSince > 0
    ? Math.max(0, Math.round((root.now - client.pendingSince) / 1000))
    : 0

  // One word, so it can live in a bar without becoming a second panel. It
  // exists to answer one question from across the room: can I talk now.
  readonly property string label: {
    // An outstanding question outranks the voice state, and is checked rather
    // than inferred from it: a question asked in text never flips the state to
    // "thinking", so keying off the state alone left the bar claiming to be
    // listening while the agent was away.
    if (client.pendingSince > 0)
      return root.waitedSeconds > 0 ? "looking " + root.waitedSeconds + "s" : "looking"

    switch (voiceState) {
    case "listening": return "listening"
    case "thinking": return "looking"
    case "speaking": return "speaking"
    case "error": return "error"
    default: return ""
    }
  }

  // The Prime Radiant instead of a microphone glyph: a crystal you consult to
  // see what is going on reads better at 16 px, and is not the same mark as
  // every other audio widget in the bar.
  Component {
    id: radiantIcon
    PrimeRadiant {
      tint: root.tint
      voiceState: root.voiceState
      level: client.level
    }
  }

  function refresh() { client.wanted = true }

  function togglePanel() {
    if (bar && bar.shell && typeof bar.shell.toggle === "function")
      bar.shell.toggle("io.github.baranskyi.omavoice", "{}")
  }

  // The same thing Q does inside the panel, reachable without opening it.
  //
  // Worth having on the icon precisely because the icon is where you look when
  // you want this: the glow says the microphone is open, and the thing you
  // want at that moment is for it to stop — not to open a window first, find
  // the key, and press it. The gesture is on the mark that told you.
  //
  // Not `endSession` verbatim, though. That one also drops the panel's socket,
  // which for the bar would put out the status light along with the
  // microphone, and a widget that goes dark when you ask it to stop listening
  // is indistinguishable from one that crashed.
  function stopListening() {
    client.stopSession()
    if (bar && bar.shell && typeof bar.shell.hide === "function")
      bar.shell.hide("io.github.baranskyi.omavoice")
  }

  // Always connected: the icon is a status light, and a status light that only
  // works while you are looking at the panel is pointless.
  Client { id: client; wanted: true }

  implicitWidth: button.implicitWidth + (labelText.visible ? labelText.implicitWidth + Style.space(6) : 0)
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    bar: root.bar
    iconComponent: radiantIcon
    active: root.voiceState === "listening" || root.voiceState === "speaking"
    slotSize: Style.bar.statusSlot
    tooltipText: {
      if (!client.connected) return "Voice — daemon not running"
      if (client.errorText) return client.errorText
      if (!client.hasKey) return "Voice — no API key yet"
      const what = "Voice · " + client.backend + " · " + client.voice
      // Said only while it is true. A gesture nobody knows about is not a
      // feature, but an instruction to stop something that is not running is
      // just noise in a tooltip that is read at a glance.
      return root.micOpen ? what + "\nRight-click to stop listening" : what
    }
    onPressed: function (b) {
      if (b === Qt.MiddleButton) client.setBackend(client.backend === "codex" ? "claude" : "codex")
      else if (b === Qt.RightButton) root.stopListening()
      else root.togglePanel()
    }

    // The glow itself: a coloured shadow with no offset, which is what a halo
    // is. Drawn from the icon's own shape, so it follows the crystal rather
    // than sitting behind it as a disc.
    layer.enabled: root.micOpen
    layer.effect: MultiEffect {
      shadowEnabled: true
      shadowColor: root.tint
      shadowBlur: 1.0
      shadowScale: 1.3
      shadowHorizontalOffset: 0
      shadowVerticalOffset: 0
      shadowOpacity: root.glow
    }

    SequentialAnimation on opacity {
      running: root.voiceState === "thinking"
      loops: Animation.Infinite
      NumberAnimation { to: 0.4; duration: 600; easing.type: Easing.InOutQuad }
      NumberAnimation { to: 1.0; duration: 600; easing.type: Easing.InOutQuad }
    }
  }

  Text {
    id: labelText
    anchors.left: button.right
    anchors.leftMargin: Style.space(4)
    anchors.verticalCenter: parent.verticalCenter
    visible: root.showLabel && root.label !== "" && !root.vertical
    text: root.label
    textFormat: Text.PlainText
    color: root.tint
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
    font.pixelSize: Style.font.body

    // The word glows with the icon. Two marks saying the same thing is not
    // redundancy here: whichever one the eye lands on, it says the microphone
    // is open.
    layer.enabled: root.micOpen
    layer.effect: MultiEffect {
      shadowEnabled: true
      shadowColor: root.tint
      shadowBlur: 0.9
      shadowScale: 1.15
      shadowHorizontalOffset: 0
      shadowVerticalOffset: 0
      shadowOpacity: root.glow * 0.85
    }

    TapHandler { onTapped: root.togglePanel() }
  }
}
