pragma ComponentBehavior: Bound

// The voice figure.
//
// Built on the same idea as the dwitter torus that inspired it: a parametric
// curve whose radius is modulated by a wave travelling outward from the
// centre, drawn three times with a phase offset so the copies interleave into
// something that reads as one shimmering body rather than three lines.
//
//     k = cos(y*5)*21                 fast harmonics make the filaments
//     q = ... + k*cos(y/2)*2*sin(o*3 - e/2 - t)    radius pulses outward
//     c = o*e/19 + t/8 + i%3*8        three copies, offset
//
// Unrolled from a torus into a horizontal band: `o`, distance from the centre
// of the figure, becomes |s|, distance from the centre of the band. An
// envelope pins the ends to nothing and lets the middle breathe, which is what
// makes a line of dots read as a voice rather than as a graph.
//
// Four harmonics, one per spectrum band, so the shape changes with timbre and
// not just with volume: a vowel swells the low harmonics into a slow heavy
// curve, a sibilant lights the top one into a fine spray. That difference is
// the whole reason it feels alive.
//
// Canvas rather than a Repeater of rectangles: several hundred points redrawn
// every frame is a few thousand fillRect calls, which Canvas does happily,
// while several hundred QML items with per-frame bindings would not.

import QtQuick
import qs.Commons

Item {
  id: root

  property string voiceState: "idle"
  property real level: 0
  property var bands: [0, 0, 0, 0]
  property color accent: Color.accent
  property color dim: Color.muted

  implicitWidth: Style.spaceReal(440)
  implicitHeight: Style.spaceReal(120)

  readonly property bool active: voiceState === "listening" || voiceState === "speaking"

  // --- smoothed inputs -------------------------------------------------------
  // Rises fast so a consonant registers on the frame it happens, falls slowly
  // so the figure glides down instead of collapsing between syllables.
  property real smoothLevel: 0
  property real b0: 0
  property real b1: 0
  property real b2: 0
  property real b3: 0
  property real phase: 0
  property real thinkHead: -1.4

  function approach(current, target, rise, fall) {
    const rate = target > current ? rise : fall
    return current + (target - current) * rate
  }

  Timer {
    // 60 fps while there is a voice to follow, 30 while idling. The panel
    // shares a process with the bar, so half the frames when nothing is
    // happening is half the cost of the whole desktop stuttering.
    interval: root.active || root.voiceState === "thinking" ? 16 : 33
    repeat: true
    running: root.visible
    onTriggered: {
      const src = root.bands || []
      const target = root.active ? root.level : 0

      root.smoothLevel = root.approach(root.smoothLevel, target, 0.5, 0.09)
      root.b0 = root.approach(root.b0, root.active ? (src[0] || 0) : 0, 0.45, 0.10)
      root.b1 = root.approach(root.b1, root.active ? (src[1] || 0) : 0, 0.45, 0.11)
      root.b2 = root.approach(root.b2, root.active ? (src[2] || 0) : 0, 0.50, 0.13)
      root.b3 = root.approach(root.b3, root.active ? (src[3] || 0) : 0, 0.55, 0.16)

      root.phase += root.voiceState === "speaking" ? 0.052
                  : root.voiceState === "listening" ? 0.038
                  : root.voiceState === "thinking" ? 0.030
                  : 0.013

      if (root.voiceState === "thinking") {
        root.thinkHead += 0.019
        if (root.thinkHead > 1.4) root.thinkHead = -1.4
      }

      canvas.requestPaint()
    }
  }

  Canvas {
    id: canvas
    anchors.fill: parent
    renderStrategy: Canvas.Cooperative
    antialiasing: false

    readonly property int pointsPerLayer: 132
    readonly property int layers: 3

    onPaint: {
      const ctx = getContext("2d")
      const w = width
      const h = height
      ctx.reset()
      ctx.clearRect(0, 0, w, h)
      if (w <= 4 || h <= 4) return

      const cy = h / 2
      const pad = Math.max(6, w * 0.045)
      const span = w - pad * 2
      const reach = h * 0.46

      const t = root.phase
      const lvl = root.smoothLevel
      const isError = root.voiceState === "error"
      const thinking = root.voiceState === "thinking"

      const col = isError ? root.dim : root.accent
      const cr = col.r, cg = col.g, cb = col.b

      // The figure has to be worth looking at in silence too — a listening
      // panel that shows a flat line reads as broken. So it keeps real
      // amplitude at rest, and your voice buys the rest.
      const idleAmp = root.active ? 0.46 : (thinking ? 0.30 : 0.34)
      const gain = root.active ? 0.52 : 0.0

      // Mirroring each point about the centre line turns a wavy line into a
      // body with a waist: fat in the middle, tapering to nothing at the ends.
      // The pair is not a perfect reflection — a slightly different amplitude
      // and phase on the lower half keeps it from reading as a mirror trick.
      const mirror = [
        { scale: 1.0, skew: 0.0, fade: 1.0 },
        { scale: -0.86, skew: 0.7, fade: 0.82 }
      ]

      for (let L = 0; L < canvas.layers; L++) {
        // The torus drew three copies at i%3*8 radians apart. Here the offset
        // is in phase and in a slight speed difference, so the layers drift
        // through each other instead of staying locked.
        const lp = t * (1 + L * 0.14) + L * 2.09
        const layerFade = 1 - L * 0.19

        for (let i = 0; i < canvas.pointsPerLayer; i++) {
          const u = i / (canvas.pointsPerLayer - 1)
          const s = u * 2 - 1
          const away = Math.abs(s)

          // Ends pinned to nothing, middle free — the shape of a voice.
          const env = Math.pow(Math.max(0, 1 - s * s), 1.35)
          if (env < 0.004) continue

          let y
          if (thinking) {
            // A pulse sweeping the band: the agent is working, nothing is
            // being heard, and a level-driven figure would be a lie.
            const d = s - root.thinkHead
            const bump = Math.exp(-(d * d) / 0.030)
            y = Math.sin(s * 9.0 - t * 3.2 + L * 0.8) * (0.10 + bump * 1.55)
          } else {
            // Four harmonics, one per band. Each keeps a small floor so the
            // figure stays interesting in silence, and grows with its band.
            const w0 = 0.30 + root.b0 * 1.00
            const w1 = 0.18 + root.b1 * 0.86
            const w2 = 0.10 + root.b2 * 0.72
            const w3 = 0.05 + root.b3 * 0.60

            y  = Math.sin(s * 3.1  + lp * 1.00) * w0
            y += Math.sin(s * 6.7  - lp * 1.37) * w1
            y += Math.sin(s * 11.3 + lp * 0.83) * w2
            y += Math.sin(s * 19.1 - lp * 1.71) * w3

            // Normalised against the weights, because four harmonics mostly
            // cancel: unnormalised the sum hovers near a quarter of its own
            // range and the figure collapses into a flat thread.
            y /= (w0 + w1 + w2 + w3)

            // The inherited move: a wave travelling from the centre outward,
            // modulating everything else. This is what makes it shimmer
            // instead of merely oscillate.
            const radial = Math.sin(away * 6.2 - t * 2.4 + L * 0.9)
            y *= 0.72 + radial * 0.34
          }

          const amp = env * (idleAmp + lvl * gain) * reach
          const px = pad + u * span

          for (let m = 0; m < mirror.length; m++) {
            const arm = mirror[m]
            const yy = arm.skew === 0
              ? y
              : y * Math.cos(arm.skew) + Math.sin(s * 5.3 + lp + arm.skew) * 0.22
            const py = cy + yy * amp * arm.scale

            const strength = Math.min(1, Math.abs(yy) * 0.5 + env * 0.5)
            const size = (1.3 + env * 2.2 + lvl * env * 2.6) * layerFade * arm.fade
            if (size < 0.4) continue

            const alpha = Math.min(
              1, (0.16 + env * 0.72 + strength * 0.26) * layerFade * arm.fade
            )

            // Peaks bleach toward white. Themes only guarantee accent, so the
            // highlight is the accent lifted rather than a colour of its own.
            const lift = strength * strength * 0.6
            ctx.fillStyle = Qt.rgba(
              cr + (1 - cr) * lift,
              cg + (1 - cg) * lift,
              cb + (1 - cb) * lift,
              alpha
            )
            ctx.fillRect(px - size / 2, py - size / 2, size, size)

            // A soft halo under the brightest points, only where it shows.
            if (L === 0 && strength > 0.55) {
              const halo = size * 3.4
              ctx.fillStyle = Qt.rgba(cr, cg, cb, 0.07 * strength)
              ctx.fillRect(px - halo / 2, py - halo / 2, halo, halo)
            }
          }
        }
      }
    }
  }
}
