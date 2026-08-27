pragma ComponentBehavior: Bound

// The microphone in the bar: what the assistant is doing, at a glance.
//
// It keeps its own thin socket to the daemon rather than reading state off the
// overlay, because the overlay only exists while the panel is open and the bar
// should still show that a session is live. Clicking it toggles the panel
// through the host, the same path the hotkey takes.

import QtQuick
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.baranskyi.omavoice"

  readonly property string voiceState: client.connected ? client.voiceState : "offline"
  readonly property bool showLabel: {
    const value = setting("showLabel", false)
    if (typeof value === "boolean") return value
    return ["true", "1", "yes", "on"].indexOf(String(value).trim().toLowerCase()) !== -1
  }


  readonly property color tint: {
    switch (voiceState) {
    case "listening": return Color.accent
    case "speaking": return Color.accent
    case "error": return Color.urgent
    default: return bar ? bar.barForeground : Color.foreground
    }
  }

  readonly property string label: {
    switch (voiceState) {
    case "listening": return "listening"
    case "thinking": return "looking"
    case "speaking": return "speaking"
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
      return "Voice · " + client.backend + " · " + client.voice
    }
    onPressed: function (b) {
      if (b === Qt.MiddleButton) client.setBackend(client.backend === "codex" ? "claude" : "codex")
      else root.togglePanel()
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
    TapHandler { onTapped: root.togglePanel() }
  }
}
