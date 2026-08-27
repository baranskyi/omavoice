pragma ComponentBehavior: Bound

// The waterfall: what the assistant is actually doing, in the open.
//
// This is a Linux desktop, and there is no reason to hide the machinery behind
// a spinner. Waiting eleven seconds for an answer is fine when you can see the
// question that went to the agent and the clock running on it; the same eleven
// seconds staring at a pulsing dot feels broken.
//
// Newest at the top, so the line you want is where you are already looking and
// nothing you have read moves. Older lines fade downward and drop off — the
// stream is the point, not the history.

import QtQuick
import qs.Commons

Item {
  id: root

  property alias model: repeater.model
  property color accent: Color.accent
  property color base: Color.menu.text
  property color urgent: Color.urgent

  // Beyond this the lines are transparent anyway, so they are not drawn.
  readonly property int visibleRows: 7

  // Milliseconds since the outstanding question was sent, 0 when nothing is
  // pending. The newest line reads this and counts up.
  property real pendingSince: 0
  property real now: 0

  Timer {
    interval: 100
    repeat: true
    running: root.visible && root.pendingSince > 0
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  implicitHeight: column.implicitHeight

  function glyphFor(kind) {
    switch (kind) {
    case "heard":   return "‹"    // in from the microphone
    case "agent":   return "→"    // out to the local agent
    case "result":  return "←"    // back from it
    case "said":    return "›"    // out to the speakers
    case "barge":   return "⨯"
    case "error":   return "!"
    case "session": return "◆"
    case "reset":   return "⟳"
    case "voice":   return "♪"
    case "key":     return "⚿"
    default:        return "·"
    }
  }

  function colorFor(kind) {
    switch (kind) {
    case "agent":
    case "result":  return root.accent
    case "error":
    case "barge":   return root.urgent
    default:        return root.base
    }
  }

  function stampFor(at) {
    if (!at) return "--:--:--"
    const d = new Date(at * 1000)
    return Qt.formatTime(d, "HH:mm:ss")
  }

  Column {
    id: column
    width: parent.width
    spacing: Style.spaceReal(3)

    Repeater {
      id: repeater

      delegate: Item {
        id: row
        required property int index
        required property string kind
        required property string text
        required property string detail
        required property real at

        width: column.width
        height: index < root.visibleRows ? line.implicitHeight : 0
        visible: index < root.visibleRows
        clip: true

        // Fades as it is pushed down. Bound rather than set, so an arriving
        // line both fades itself in and pushes the others one step dimmer in
        // the same animation.
        opacity: 0
        Component.onCompleted: opacity = Qt.binding(function () {
          return Math.max(0, 1 - row.index * 0.15)
        })

        Behavior on opacity { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
        Behavior on height { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }

        Row {
          id: line
          width: parent.width
          spacing: Style.spaceReal(6)

          Text {
            id: stamp
            text: root.stampFor(row.at)
            textFormat: Text.PlainText
            color: root.base
            opacity: 0.4
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Text {
            id: glyph
            width: Style.spaceReal(9)
            text: root.glyphFor(row.kind)
            textFormat: Text.PlainText
            color: root.colorFor(row.kind)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            // One line, elided. A three-line transcript in the waterfall would
            // push everything else off the panel.
            width: line.width - stamp.width - glyph.width - detailText.width
                   - line.spacing * 3
            text: row.text
            textFormat: Text.PlainText
            elide: Text.ElideRight
            maximumLineCount: 1
            color: row.kind === "error" ? root.urgent : root.base
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Text {
            id: detailText
            // The top line counts while its answer is outstanding, then
            // freezes at whatever the result reported.
            text: row.index === 0 && row.kind === "agent" && root.pendingSince > 0
              ? row.detail + " · " + ((root.now - root.pendingSince) / 1000).toFixed(1) + "s"
              : row.detail
            textFormat: Text.PlainText
            color: root.accent
            opacity: 0.55
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
