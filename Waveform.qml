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

  // --- state colour ----------------------------------------------------------
  //
  // The figure says what the assistant is doing before any word does, so the
  // states that matter get a hue of their own: ready, working, broken.
  //
  // A theme cannot be asked for "green" — it guarantees an accent and nothing
  // else, and on a light theme its accent may be nearly white. So the hue is
  // fixed here and only the lightness is borrowed from the theme, picked
  // against the background it will be drawn on. Green stays green in every
  // theme, and stays readable in all of them.
  StateHues { id: hues }

  readonly property color stateColor:
    hues.colorFor(root.voiceState, Color.menu.background, root.accent)

  // --- barge-in ---------------------------------------------------------------
  // Pulsed when the person talks over the assistant. Not a colour change — the
  // state is about to change on its own — but a physical one: the figure buzzes
  // like a plucked wire and settles. It reads as "heard you, stopping".
  property real buzz: 0
  function bargeIn() { root.buzz = 1.0 }

  // How hard the voice just arrived, which is not the same as how loud it is.
  //
  // Loudness cannot be read here at all: the daemon sends min(1, rms/6000) and
  // 6000 is ordinary speech, so a shout and a sentence both come through as
  // 1.0. What survives that clamp is the *attack* — the level outrunning its
  // own smoothed follower — and that is the thing worth reacting to anyway.
  // Somebody starting to talk loudly is an event; somebody continuing to is a
  // condition, and a figure that stayed violent for as long as you were loud
  // would be exhausting.
  property real surge: 0

  // One number for "something just happened to this signal", whichever it was.
  readonly property real shock: Math.max(root.buzz, root.surge)

  // Deterministic scatter. An integer hash rather than Math.sin: this is
  // evaluated per point, per layer, per frame, and the figure already spends
  // 780 fills a frame in a process shared with the whole desktop bar.
  function scatter(n) {
    n = (n ^ 61) ^ (n >>> 16)
    n = n + (n << 3)
    n = n ^ (n >>> 4)
    n = Math.imul(n, 0x27d4eb2d)
    n = n ^ (n >>> 15)
    return ((n >>> 0) % 1024) / 1024 - 0.5
  }

  implicitWidth: Style.spaceReal(440)
  implicitHeight: Style.spaceReal(120)

  readonly property bool active: voiceState === "listening" || voiceState === "speaking"

  // --- the light this thing throws --------------------------------------------
  // How far the figure is swinging at each slice of its own width, 0..1, so
  // whatever is drawn behind it can be lit by it. The threads are bright and
  // they move; if they were real they would fall on what is underneath, hardest
  // where the wave is loudest and barely at all out at the tapered ends.
  //
  // Published rather than computed twice: the shape comes out of four harmonics
  // weighted by four bands and then a radial term, and a second implementation
  // of that would be a second thing to keep true.
  readonly property int slices: 14
  property var light: new Array(14).fill(0)
  property int lightTick: 0

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

      // Measured before the follower moves, or there is nothing to outrun.
      const attack = Math.max(0, target - root.smoothLevel)
      root.surge = Math.max(root.surge * 0.86, Math.min(1, attack * 2.8))

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

      // Faster than it was. The tremble is meant to read as a flinch, and a
      // flinch that takes a full second to subside is a wobble.
      if (root.buzz > 0.001) root.buzz *= 0.87
      else root.buzz = 0
      if (root.surge <= 0.002) root.surge = 0

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
      // Wider than it looks like it needs to be. This margin is the only
      // room a displaced mark has before the Canvas clips it out of
      // existence, and the scatter below now reaches about 4.8% of the span
      // at full shock. At the old 4.5% the outermost points would have
      // silently stopped being drawn at exactly the moment they were
      // supposed to be flying.
      const pad = Math.max(8, w * 0.060)
      const span = w - pad * 2
      const reach = h * 0.46

      // Two measures per slice, because one is not enough. The peak swing
      // says how hard the wave is working there; the envelope says how much
      // wave there is at all. Averaging the swing instead of peaking it put
      // dead slices wherever a harmonic happened to cross zero, and the text
      // came out barred rather than lit.
      const peak = new Array(root.slices).fill(0)
      const envs = new Array(root.slices).fill(0)
      const hits = new Array(root.slices).fill(0)

      const t = root.phase
      const lvl = root.smoothLevel
      const isError = root.voiceState === "error"
      const thinking = root.voiceState === "thinking"

      const col = root.stateColor
      const cr = col.r, cg = col.g, cb = col.b

      // A broken session should look broken. Horizontal slices slip sideways
      // and the points scatter — the picture of a signal that has lost its
      // footing, rather than a calm figure in a different colour.
      const glitch = isError ? 1 : 0
      const buzz = root.buzz
      const shock = root.shock
      // Re-rolled every frame, so a disturbance grains rather than shifting a
      // fixed pattern around. Coarser than the phase clock on purpose.
      const roll = Math.floor(t * 97)

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

          let amp = env * (idleAmp + lvl * gain) * reach

          // The buzz: a fine tremble at a frequency nothing else in the figure
          // uses, loudest in the middle and gone within a breath.
          if (buzz > 0) {
            amp *= 1 + buzz * 0.72 * Math.sin(s * 47.0 + t * 33.0 + L)
          }

          let px = pad + u * span
          let jitter = 0

          // A disturbance pulls the body apart rather than making it bigger.
          //
          // Bigger was the obvious move and it is the wrong one twice over.
          // The envelope already uses about 95% of the height it is allowed —
          // reach is h*0.46 against a centre line at h/2 — so more amplitude
          // buys flat tops where the Canvas clips, and a clipped wave reads as
          // weaker rather than stronger. And what was asked for is the points
          // coming off each other, which is a different quantity entirely: the
          // coherent part shrinks a little and that energy goes into scatter.
          if (shock > 0.004) {
            // Raised more than the scatter was, not less. Every number below
            // pushes marks further from where they belong, and this is the
            // one that pays for it: the body has to give up more room, or the
            // extra throw lands past the edge of the canvas and simply is not
            // drawn.
            amp *= 1 - shock * 0.28

            // Two scales at once. Slices give it structure — a torn thing,
            // not a fuzzy one — and the per-point grain keeps the slices from
            // reading as three rigid blocks sliding about.
            const slice = Math.floor(u * 11) + roll
            const tear = root.scatter(slice * 7 + L * 131)
            const grain = root.scatter(i * 2749 + L * 9181 + roll * 31)

            px += (tear * 0.068 + grain * 0.026) * span * shock
            jitter = grain * shock
          }

          if (glitch) {
            // Slices, not noise: a band of the figure jumps as a piece.
            const slice = Math.floor(u * 7) + Math.floor(t * 3)
            const jump = ((slice * 2654435761) % 1000) / 1000 - 0.5
            px += jump * span * 0.055
            amp *= 0.55 + Math.abs(jump) * 1.6
          }

          for (let m = 0; m < mirror.length; m++) {
            const arm = mirror[m]
            const yy = arm.skew === 0
              ? y
              : y * Math.cos(arm.skew) + Math.sin(s * 5.3 + lp + arm.skew) * 0.22
            let py = cy + yy * amp * arm.scale
            let pxm = px

            // The two arms are torn apart separately, so the figure comes
            // open along the centre line instead of shuddering as one piece.
            if (jitter !== 0) {
              const arm2 = root.scatter(i * 5051 + L * 313 + m * 78901 + roll * 17)
              py += arm2 * shock * reach * 0.21
              pxm += arm2 * shock * span * 0.016
            }

            const strength = Math.min(1, Math.abs(yy) * 0.5 + env * 0.5)
            let size = (1.3 + env * 2.2 + lvl * env * 2.6) * layerFade * arm.fade
            // Uneven marks. Points that survive a shock at full size next to
            // points that nearly vanish is most of what makes a field look
            // broken rather than merely displaced.
            if (jitter !== 0) size *= 1 + jitter * 1.50
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
            // One layer, one arm: the profile wants the figure's shape, not
            // three copies of it, and this is the loop everything else runs in.
            if (L === 0 && m === 0) {
              const b = Math.min(root.slices - 1, Math.floor(u * root.slices))
              peak[b] = Math.max(peak[b], Math.abs(py - cy) / reach)
              envs[b] += env
              hits[b] += 1
            }

            ctx.fillRect(pxm - size / 2, py - size / 2, size, size)

            // A soft halo under the brightest points, only where it shows.
            if (L === 0 && strength > 0.55) {
              const halo = size * 3.4
              ctx.fillStyle = Qt.rgba(cr, cg, cb, 0.07 * strength)
              ctx.fillRect(pxm - halo / 2, py - halo / 2, halo, halo)
            }
          }
        }
      }

      // Every third frame. The figure is redrawn sixty times a second and
      // nothing reading this needs that: publishing each time would put a
      // fresh array through a dozen bindings per line of text for no gain
      // anyone can see.
      root.lightTick += 1
      if (root.lightTick % 3 === 0) {
        const prev = root.light
        const usable = prev && prev.length === root.slices
        const next = new Array(root.slices)
        for (let b = 0; b < root.slices; b++) {
          const raw = hits[b] > 0
            ? Math.min(1, 0.40 * (envs[b] / hits[b]) + 0.60 * Math.min(1, peak[b] * 2.2))
            : 0
          // Eased across frames, or the text under a consonant flickers.
          next[b] = usable ? prev[b] * 0.72 + raw * 0.28 : raw
        }
        root.light = next
      }
    }
  }
}
