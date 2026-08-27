pragma ComponentBehavior: Bound

// Which agent is answering, in its own brand colours.
//
// The backend switch matters — codex runs on the ChatGPT subscription, claude
// brings skills and MCP — so it should be readable at a glance rather than
// being one more word in the theme's foreground colour.
//
// Anthropic's accent is the flat terracotta #D97757 from their palette. Codex
// marks itself with a lavender-to-blue gradient, so it gets the gradient — a
// flat blue would read as "some blue chip" rather than as Codex.

import QtQuick
import qs.Commons

Rectangle {
  id: root

  property string agent: "codex"
  readonly property bool isClaude: agent === "claude"

  // Brand constants, deliberately not theme colours: the point is to look
  // like the vendor, not like the desktop.
  readonly property color claudeTerracotta: "#D97757"
  readonly property color codexLavender: "#A5A2F6"
  readonly property color codexBlue: "#3B4DF6"
  readonly property color paper: "#FAF9F5"

  // Fixed height, not derived from the text: the two badges carry different
  // glyphs, and letting each size itself made claude stand a pixel taller than
  // codex wherever they appear side by side.
  readonly property real rowHeight: Style.spaceReal(19)

  implicitWidth: label.implicitWidth + Style.spaceReal(15)
  implicitHeight: rowHeight
  radius: Style.spaceReal(5)

  // Anthropic's colour is flat, Codex's is a gradient — so only one of the two
  // ever paints at a time; `gradient` wins over `color` when both are set.
  color: root.isClaude ? root.claudeTerracotta : "transparent"
  gradient: root.isClaude ? null : codexGradient

  Gradient {
    id: codexGradient
    GradientStop { position: 0.0; color: root.codexLavender }
    GradientStop { position: 1.0; color: root.codexBlue }
  }

  border.width: 1
  border.color: root.isClaude
    ? Qt.darker(root.claudeTerracotta, 1.15)
    : Qt.darker(root.codexBlue, 1.2)

  Behavior on color { ColorAnimation { duration: 180 } }

  Row {
    id: label
    anchors.centerIn: parent
    spacing: Style.spaceReal(4)

    // Anthropic's mark is an eight-spoked burst. Drawn rather than typed:
    // the nearest glyph (U+2733) renders at a different weight and baseline
    // from the monospace text beside it, which is what made this look crooked.
    Item {
      id: mark
      visible: root.isClaude
      anchors.verticalCenter: parent.verticalCenter
      width: Style.spaceReal(9)
      height: Style.spaceReal(9)

      Canvas {
        anchors.fill: parent
        renderStrategy: Canvas.Cooperative
        onPaint: {
          const ctx = getContext("2d")
          ctx.reset()
          ctx.clearRect(0, 0, width, height)
          const cx = width / 2
          const cy = height / 2
          const outer = Math.min(width, height) / 2
          // Long spokes on the axes, short ones on the diagonals — the
          // proportion that reads as Anthropic's asterisk rather than as a
          // snowflake.
          ctx.strokeStyle = root.paper
          ctx.lineWidth = Math.max(1, Math.round(outer * 0.30))
          ctx.lineCap = "round"
          for (let i = 0; i < 8; i++) {
            const angle = i * Math.PI / 4
            const reach = (i % 2 === 0) ? outer : outer * 0.66
            ctx.beginPath()
            ctx.moveTo(cx, cy)
            ctx.lineTo(cx + Math.cos(angle) * reach, cy + Math.sin(angle) * reach)
            ctx.stroke()
          }
        }
      }
    }

    Text {
      visible: !root.isClaude
      anchors.verticalCenter: parent.verticalCenter
      text: ">_"
      textFormat: Text.PlainText
      color: root.paper
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.bold: true
      opacity: 0.9
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: root.agent
      textFormat: Text.PlainText
      color: root.paper
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.bold: true
    }
  }
}
