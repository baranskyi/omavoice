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

  clip: true

  // Older lines are nearer the top and fainter; the newest is being typed at
  // the bottom. Four is enough to read as activity and few enough that the
  // figure is never fighting a wall of text.
  readonly property int keep: 4
  ListModel { id: lines }

  // Characters per tick. Fast enough to keep up with an agent that says a
  // sentence and then runs three commands, slow enough that the typing is
  // visible as typing rather than as text appearing.
  readonly property int speed: 3

  property real dissolve: 1.0

  function push(text) {
    const clean = String(text || "").replace(/\s+/g, " ").trim()
    if (clean === "") return
    lines.append({ body: clean, shown: 0, age: 0 })
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
      for (let i = 0; i < lines.count; i++) {
        const row = lines.get(i)
        if (row.shown < row.body.length) {
          lines.setProperty(i, "shown", Math.min(row.body.length, row.shown + root.speed))
        }
        lines.setProperty(i, "age", row.age + 1)
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

  Column {
    id: stack
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    spacing: Style.spaceReal(2)

    // The whole stack slides up as lines arrive, which is what makes the
    // oldest one leave rather than simply stop being drawn.
    add: Transition { NumberAnimation { properties: "y"; duration: 420; easing.type: Easing.OutCubic } }
    move: Transition { NumberAnimation { properties: "y"; duration: 420; easing.type: Easing.OutCubic } }

    Repeater {
      model: lines

      Text {
        id: row
        required property string body
        required property int shown
        required property int index

        width: stack.width
        text: row.body.substring(0, row.shown)
        textFormat: Text.PlainText
        elide: Text.ElideRight
        maximumLineCount: 1
        color: root.tint
        font.family: Style.font.family
        font.pixelSize: Style.font.caption

        // Faint, and fainter the older it is. The newest line is the only one
        // meant to be legible at all, and even that one only just.
        opacity: root.dissolve * Math.max(0, 0.17 - (lines.count - 1 - row.index) * 0.038)
        Behavior on opacity { NumberAnimation { duration: 260 } }
      }
    }
  }
}
