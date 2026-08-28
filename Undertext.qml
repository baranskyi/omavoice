pragma ComponentBehavior: Bound

// What the agent is doing, written behind the figure and never quite readable.
//
// There is a gap in this program between asking and hearing, and for a real
// question it is twenty or thirty seconds of a panel that looks asleep. It is
// not asleep: the agent is narrating its plan and running commands, and all of
// that has been arriving on a pipe the whole time with nobody to show it to.
//
// The obvious thing to do with that is print it. The obvious thing is wrong —
// a log under the waveform turns a calm panel into a terminal, and invites
// reading during the one moment the person should be waiting rather than
// working. So it goes *behind*: faint enough that the figure wins, typed
// rather than pasted, drifting upward and dissolving. You can tell that
// something is happening and roughly what kind of something, and if you lean
// in you can read a line. That is the whole intended dose.
//
// It fades out rather than clearing when the agent finishes, because the
// answer arriving is not a reason for the working to vanish as if it had never
// happened.

import QtQuick
import qs.Commons

Item {
  id: root

  // Whether the agent is working. False starts the dissolve.
  property bool working: false
  // The hue the figure is using, so the two agree.
  property color tint: Color.menu.text

  // How brightly the figure is burning at each slice of its width, from the
  // figure itself. This is what makes the text look lit rather than printed:
  // the wave is a row of bright threads swinging above the words, and a real
  // one would throw light down on them — hard under the middle where it
  // thrashes, almost nothing under the tapered ends.
  property var light: []
  readonly property int slices: 14

  // Where each slice falls in the line. The face is monospace, so a column of
  // characters is a fixed distance across and the slice a character belongs to
  // can be worked out from its index — no per-glyph measuring, and the light
  // lands on the same part of the block whatever the line happens to say.
  TextMetrics {
    id: gauge
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    text: "MMMMMMMMMM"
  }
  readonly property real charWidth: gauge.advanceWidth > 0 ? gauge.advanceWidth / 10 : 7
  readonly property int columns: Math.max(root.slices, Math.floor(root.width / root.charWidth))
  readonly property int perSlice: Math.max(1, Math.ceil(root.columns / root.slices))

  clip: true

  // Older lines are nearer the top and fainter; the newest is being typed at
  // the bottom. As many as the block will hold, so the working fills the space
  // behind the figure rather than pooling under its waist — the figure is
  // widest in the middle, and text that stops at the centre line reads as a
  // second panel rather than as something showing through.
  readonly property int lineStep: Math.max(1, Style.font.caption + Style.spaceReal(2))
  readonly property int keep: Math.max(4, Math.min(14, Math.floor(root.height / root.lineStep)))
  ListModel { id: lines }

  // Characters per tick. Fast enough to keep up with an agent that says a
  // sentence and then runs three commands, slow enough that the typing is
  // visible as typing rather than as text appearing.
  readonly property int speed: 3

  property real dissolve: 1.0

  // Pixels a line climbs per tick. Slow enough that a line crosses the block
  // in about twenty seconds, which is roughly how long the agent takes — so a
  // question's working occupies the height once and drifts off the top as the
  // answer arrives, rather than piling up at the bottom waiting to be pushed.
  readonly property real drift: Math.max(0.15, root.height / 520)

  function push(text) {
    const clean = String(text || "").replace(/\s+/g, " ").trim()
    if (clean === "") return
    lines.append({ body: clean, shown: 0, rise: 0 })
    while (lines.count > root.keep) lines.remove(0)
    root.dissolve = 1.0
  }

  function forget() {
    lines.clear()
    root.dissolve = 1.0
  }

  onWorkingChanged: if (working) root.dissolve = 1.0

  Timer {
    interval: 45
    repeat: true
    running: root.visible && (lines.count > 0)
    onTriggered: {
      // Only the last line types; the ones above it are finished by
      // definition, because a new line only ever arrives after them.
      for (let i = lines.count - 1; i >= 0; i--) {
        const row = lines.get(i)
        if (row.shown < row.body.length) {
          lines.setProperty(i, "shown", Math.min(row.body.length, row.shown + root.speed))
        }
        // Everything rises, always — a line does not wait to be pushed up by
        // the next one. That waiting was what kept the whole thing pooled
        // along the bottom edge instead of living in the block.
        lines.setProperty(i, "rise", row.rise + root.drift)
        if (row.rise > root.height) lines.remove(i)
      }

      // The dissolve. Not a clear: the working that produced an answer should
      // still be there, going, while the answer is read.
      if (!root.working) {
        root.dissolve -= 0.010
        if (root.dissolve <= 0) {
          root.dissolve = 0
          lines.clear()
        }
      }
    }
  }

  Repeater {
    model: lines

    Row {
      id: row
      required property string body
      required property int shown
      required property real rise

      // Each line owns its height in the block. Placed rather than stacked,
      // because a Column would tie every line's position to the arrival of
      // the next one, and the point is that they move on their own.
      y: root.height - root.lineStep - row.rise
      spacing: 0

      readonly property string revealed: row.body.substring(0, row.shown)

      // Brightest where it appears, gone by the time it reaches the top. The
      // curve is steep enough that the upper half of the block only ever
      // carries a suggestion of text.
      readonly property real travelled:
        Math.max(0, Math.min(1, row.rise / Math.max(1, root.height)))
      readonly property real base:
        root.dissolve * 0.17 * Math.pow(1 - row.travelled, 1.7)

      // One Text per slice rather than one per line, because a Text carries a
      // single opacity and the whole idea here is that one line is not evenly
      // lit. The pieces are consecutive substrings in a fixed-width face, so
      // they sit flush and read as one line with a light moving over it.
      Repeater {
        model: root.slices

        Text {
          required property int index
          text: row.revealed.substring(index * root.perSlice, (index + 1) * root.perSlice)
          textFormat: Text.PlainText
          color: root.tint
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          opacity: row.base * (0.26 + 1.20 * (root.light[index] || 0))
        }
      }
    }
  }
}
