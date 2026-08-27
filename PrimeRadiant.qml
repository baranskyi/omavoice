pragma ComponentBehavior: Bound

// The Prime Radiant: this plugin's mark.
//
// In Foundation it is a small crystal that holds the whole of psychohistory and
// projects it as light — an object you consult to see what is really going on.
// That is a better fit for a voice assistant that reads your machine than a
// generic microphone glyph is, and at bar size a crystal reads as a shape
// while a microphone reads as a smudge.
//
// Drawn as an octahedron in the flattest possible projection: a diamond
// outline, its equator, its vertical edge, and a core that carries the state.
// Everything lands on integer coordinates because this is sixteen pixels wide
// and a half-pixel line is a grey blur.

import QtQuick
import qs.Commons

Item {
  id: root

  property color tint: Color.accent
  property string voiceState: "idle"
  property real level: 0

  readonly property bool alive: voiceState === "listening" || voiceState === "speaking"

  // The core breathes when idle, beats with the voice when active, and blinks
  // while the agent is working.
  property real pulse: 0

  Timer {
    interval: root.alive ? 40 : 90
    repeat: true
    running: root.visible
    onTriggered: {
      root.pulse += root.voiceState === "thinking" ? 0.16
                  : root.alive ? 0.11
                  : 0.045
      canvas.requestPaint()
    }
  }

  onTintChanged: canvas.requestPaint()
  onVoiceStateChanged: canvas.requestPaint()

  Canvas {
    id: canvas
    anchors.fill: parent
    renderStrategy: Canvas.Cooperative

    onPaint: {
      const ctx = getContext("2d")
      const w = width
      const h = height
      ctx.reset()
      ctx.clearRect(0, 0, w, h)
      if (w < 6 || h < 6) return

      // Snap to the pixel grid: an odd centre on an even canvas is what makes
      // small line art look soft.
      const cx = Math.round(w / 2)
      const cy = Math.round(h / 2)
      // Sized to match the Nerd Font glyphs beside it, not to fill the slot.
      // A 13 px glyph in a 16 px slot paints about 10 px tall; at 0.40/0.46 of
      // the canvas this crystal came out half again as large and read as a
      // button someone had dropped into the bar.
      const rx = Math.max(3, Math.round(w * 0.29))
      const ry = Math.max(3, Math.round(h * 0.33))

      const c = root.tint
      const rgba = function (a) { return Qt.rgba(c.r, c.g, c.b, a) }

      const breathing = 0.5 + 0.5 * Math.sin(root.pulse)
      const energy = root.alive ? Math.min(1, root.level * 1.6) : 0

      ctx.lineWidth = 1

      // Outer crystal.
      ctx.strokeStyle = rgba(root.voiceState === "offline" ? 0.45 : 1.0)
      ctx.beginPath()
      ctx.moveTo(cx + 0.5, cy - ry + 0.5)
      ctx.lineTo(cx + rx + 0.5, cy + 0.5)
      ctx.lineTo(cx + 0.5, cy + ry + 0.5)
      ctx.lineTo(cx - rx + 0.5, cy + 0.5)
      ctx.closePath()
      ctx.stroke()

      // An inner facet rather than crosshairs: two crossing lines made it read
      // as a reticle, while a second diamond reads as a crystal with depth.
      const ix = Math.max(1, Math.round(rx * 0.48))
      const iy = Math.max(1, Math.round(ry * 0.48))
      ctx.strokeStyle = rgba(0.45 + 0.30 * breathing)
      ctx.beginPath()
      ctx.moveTo(cx + 0.5, cy - iy + 0.5)
      ctx.lineTo(cx + ix + 0.5, cy + 0.5)
      ctx.lineTo(cx + 0.5, cy + iy + 0.5)
      ctx.lineTo(cx - ix + 0.5, cy + 0.5)
      ctx.closePath()
      ctx.stroke()

      // The core carries the state: steady when idle, beating with the voice,
      // blinking while the agent works.
      const coreSize = root.voiceState === "offline" ? 1
        : Math.max(1, Math.round(1 + energy * 1.6))
      const coreAlpha = root.voiceState === "offline" ? 0.45
        : root.voiceState === "thinking" ? 0.45 + 0.55 * breathing
        : 0.85 + 0.15 * breathing

      ctx.fillStyle = rgba(coreAlpha)
      ctx.fillRect(cx - coreSize + 0.5, cy - coreSize + 0.5, coreSize * 2, coreSize * 2)

      // A halo only when it is actually doing something, so the bar stays calm.
      if (energy > 0.25) {
        ctx.fillStyle = rgba(0.12 * energy)
        const halo = coreSize * 3
        ctx.fillRect(cx - halo + 0.5, cy - halo + 0.5, halo * 2, halo * 2)
      }
    }
  }
}
