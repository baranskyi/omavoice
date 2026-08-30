pragma ComponentBehavior: Bound

// The first five screens: what this thing is, before it is talked to.
//
// Drawn rather than written. There is no artwork in this plugin and there
// should not be — so every card shows the interface itself: the crystal in the
// colours it really uses, the agent badges in their vendors' own colours, the
// folder boundary as the boundary, the access row as the row. A person who
// reads these has already seen the parts they will meet.
//
// Deep scrim and a fixed light palette, following the shell's own speed-test
// overlay: with the desktop showing through, a themed foreground is legible on
// one wallpaper and gone on the next. Near-black underneath means the same
// white reads everywhere.

import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  property bool open: false
  property string backend: "codex"
  property string workspace: ""

  signal closed()

  readonly property int pages: 5
  property int page: 0

  // Starting over every time it is opened. This is shown once on its own and
  // afterwards only when asked for, and someone who asks for it wants it from
  // the beginning.
  onOpenChanged: {
    if (open) {
      page = 0
      Qt.callLater(function () { keyCatcher.forceActiveFocus() })
    }
  }

  function next() {
    if (page + 1 < pages) page += 1
    else root.closed()
  }

  function back() {
    if (page > 0) page -= 1
  }

  // -- palette on the scrim ---------------------------------------------------

  // On a card, not on the scrim. Floating the content straight onto a darkened
  // desktop is what the speed test does, and it works there because two large
  // dials carry their own contrast. Five screens of small type do not: over a
  // photograph the wallpaper reads straight through the words, and the whole
  // thing looks like something that failed to finish loading rather than like
  // part of the program. So the tour sits on the same surface every other
  // window here sits on, and keeps the deep scrim outside it.
  readonly property color ink: Color.menu.text
  readonly property color inkDim: Qt.rgba(
    Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.62)
  readonly property color inkFaint: Qt.rgba(
    Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.34)
  readonly property color hairline: Qt.rgba(
    Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.20)
  readonly property color mark: Color.accent

  StateHues { id: hues }

  // -- the pieces the cards are drawn from ------------------------------------

  // A node in a schematic, in the same thin-bordered language the help window
  // uses — but on the scrim rather than on a card.
  component Node: Rectangle {
    id: node
    required property string title
    property string detail: ""
    property color tone: root.mark
    property real span: 1.0
    width: parent ? parent.width * span : 0
    anchors.horizontalCenter: parent ? parent.horizontalCenter : undefined
    height: nodeBody.implicitHeight + Style.spaceReal(14)
    radius: Style.spaceReal(5)
    color: Qt.rgba(1, 1, 1, 0.03)
    border.width: 1
    border.color: Qt.rgba(node.tone.r, node.tone.g, node.tone.b, 0.45)

    Column {
      id: nodeBody
      anchors.centerIn: parent
      width: parent.width - Style.spaceReal(20)
      spacing: Style.spaceReal(2)

      Text {
        width: parent.width
        text: node.title
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        color: node.tone
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }

      Text {
        width: parent.width
        visible: node.detail !== ""
        text: node.detail
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.Wrap
        color: root.inkDim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
  }

  // The connection between two nodes: square dots, the mark the waveform is
  // built from, with the name of what travels along it.
  component Link: Item {
    id: link
    property string label: ""
    property int dots: 3
    width: parent ? parent.width : 0
    height: Style.spaceReal(26)

    Column {
      anchors.centerIn: parent
      spacing: Style.spaceReal(3)

      Repeater {
        model: link.dots
        Rectangle {
          required property int index
          width: Style.spaceReal(3)
          height: Style.spaceReal(3)
          anchors.horizontalCenter: parent.horizontalCenter
          color: root.mark
          opacity: 0.28 + index * 0.18
        }
      }
    }

    Text {
      visible: link.label !== ""
      anchors.verticalCenter: parent.verticalCenter
      anchors.left: parent.horizontalCenter
      anchors.leftMargin: Style.spaceReal(12)
      text: link.label
      textFormat: Text.PlainText
      color: root.inkFaint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  // A key, boxed so it reads as a key and not as a word.
  component KeyCap: Row {
    id: cap
    required property string key
    required property string what
    spacing: Style.spaceReal(10)

    Rectangle {
      width: Style.spaceReal(38)
      height: capLabel.implicitHeight + Style.spaceReal(7)
      radius: Style.spaceReal(4)
      color: "transparent"
      border.width: 1
      border.color: root.hairline

      Text {
        id: capLabel
        anchors.centerIn: parent
        text: cap.key
        textFormat: Text.PlainText
        color: root.mark
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: cap.what
      textFormat: Text.PlainText
      color: root.inkDim
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
    }
  }

  // A small pill, for the things that are conditions rather than parts.
  component Chip: Rectangle {
    id: chip
    required property string text
    property color tone: root.inkDim
    width: chipLabel.implicitWidth + Style.spaceReal(18)
    height: chipLabel.implicitHeight + Style.spaceReal(9)
    radius: height / 2
    color: "transparent"
    border.width: 1
    border.color: Qt.rgba(chip.tone.r, chip.tone.g, chip.tone.b, 0.35)

    Text {
      id: chipLabel
      anchors.centerIn: parent
      text: chip.text
      textFormat: Text.PlainText
      color: chip.tone
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  // One path, inside the folder or outside it. The crossed ones are the point
  // of the card: outside, a file does not come back empty — it is not there.
  component PathRow: Row {
    id: pathRow
    required property string path
    required property bool inside
    spacing: Style.spaceReal(8)

    Rectangle {
      width: Style.spaceReal(5)
      height: Style.spaceReal(5)
      anchors.verticalCenter: parent.verticalCenter
      color: pathRow.inside ? root.mark : root.inkFaint
      opacity: pathRow.inside ? 1.0 : 0.6
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: pathRow.path
      textFormat: Text.PlainText
      color: pathRow.inside ? root.ink : root.inkFaint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: !pathRow.inside
      text: "no such file"
      textFormat: Text.PlainText
      color: root.inkFaint
      opacity: 0.7
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.italic: true
    }
  }

  // The access row as it really is: three settings, one of them chosen.
  component Grant: Column {
    id: grant
    required property string agent
    required property int choice   // 0 not allowed · 1 held to the folder · 2 wide
    spacing: Style.spaceReal(6)
    width: parent ? parent.width : 0

    // No name beside it: the badge is already the name, in the vendor's own
    // colours, and printing "codex" next to a pill that says codex reads as a
    // rendering fault rather than as a label.
    AgentBadge { agent: grant.agent }

    Row {
      spacing: Style.spaceReal(4)

      Repeater {
        model: ["not allowed", "this folder", "everything"]

        Rectangle {
          id: choiceCell
          required property int index
          required property string modelData
          readonly property bool picked: choiceCell.index === grant.choice
          width: cell.implicitWidth + Style.spaceReal(16)
          height: cell.implicitHeight + Style.spaceReal(8)
          radius: Style.spaceReal(4)
          color: choiceCell.picked
            ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.16)
            : "transparent"
          border.width: 1
          border.color: choiceCell.picked
            ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)
            : root.hairline

          Text {
            id: cell
            anchors.centerIn: parent
            text: choiceCell.modelData
            textFormat: Text.PlainText
            color: choiceCell.picked ? root.mark : root.inkFaint
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  // A card's title block. Every card has the same one, so the eye lands in the
  // same place five times rather than hunting.
  component Heading: Column {
    id: heading
    required property string eyebrow
    required property string title
    // Stops short of the dismiss mark in the card's corner. Only the heading
    // needs the clearance, so only the heading gives it up — indenting every
    // card by the same amount would push the whole thing off centre.
    width: parent ? parent.width - Style.spaceReal(26) : 0
    spacing: Style.spaceReal(6)

    Text {
      text: heading.eyebrow.toUpperCase()
      textFormat: Text.PlainText
      color: root.inkFaint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.bold: true
      font.letterSpacing: 2
    }

    Text {
      width: parent.width
      text: heading.title
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      color: root.ink
      font.family: Style.font.family
      font.pixelSize: Style.font.heading
    }
  }

  // The one line under the picture. One line, because the picture is the
  // explanation and this is only what the picture cannot draw.
  component Note: Text {
    width: parent ? parent.width : 0
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: root.inkDim
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
  }

  PanelWindow {
    id: window
    visible: root.open
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice-onboarding"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    // The same depth the shell's speed test uses. It is not decoration: the
    // cards carry their own contrast on any wallpaper only because of it.
    Rectangle {
      anchors.fill: parent
      color: Qt.rgba(0, 0, 0, 0.78)
      MouseArea { anchors.fill: parent; onClicked: root.closed() }
    }

    Item {
      id: keyCatcher
      anchors.fill: parent
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function (event) {
        switch (event.key) {
        case Qt.Key_Escape:
          root.closed(); event.accepted = true; break
        case Qt.Key_Right:
        case Qt.Key_Space:
        case Qt.Key_Return:
        case Qt.Key_Enter:
          root.next(); event.accepted = true; break
        case Qt.Key_Left:
          root.back(); event.accepted = true; break
        }
      }

      // The same surface every other window in this plugin stands on. What was
      // here before — content floating straight on the scrim, the way the
      // shell's speed test does it — reads on a dark wallpaper and disappears
      // on a photograph, and five screens of small type cannot carry their own
      // contrast the way two big dials can.
      BorderSurface {
        id: card
        anchors.centerIn: parent
        // The width of the panel this describes, so what is explained arrives
        // at the size it was explained at.
        width: Style.space(596)
        height: Style.space(468)
        radius: Style.cornerRadius
        color: Color.menu.background
        borderSpec: Border.surfaceSpec(
          "menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
        padding: Style.spacing.panelPadding
        // A short display shrinks the whole card rather than clipping it.
        scale: Math.min(1,
          (keyCatcher.width - Style.space(48)) / Math.max(1, width),
          (keyCatcher.height - Style.space(48)) / Math.max(1, height))

        MouseArea { anchors.fill: parent; onClicked: {} }

        Item {
          id: stage
          anchors.fill: parent
          anchors.topMargin: card.contentTopInset
          anchors.rightMargin: card.contentRightInset
          anchors.bottomMargin: card.contentBottomInset
          anchors.leftMargin: card.contentLeftInset

        Item {
          id: deck
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: foot.top
          anchors.bottomMargin: Style.spacing.panelGap
          clip: true

          Row {
            id: strip
            height: parent.height
            x: -root.page * deck.width
            Behavior on x {
              NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
            }

            // ---- 1. the voice ------------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap

                Heading {
                  eyebrow: "The voice"
                  title: "It hears and speaks. That is all it does."
                }

                // The panel, in miniature: the crystal, the waveform's mark,
                // and a line of what it heard.
                Rectangle {
                  width: parent.width
                  height: Style.space(122)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: root.hairline

                  Column {
                    anchors.centerIn: parent
                    spacing: Style.spaceReal(12)

                    Item {
                      width: Style.spaceReal(26)
                      height: Style.spaceReal(26)
                      anchors.horizontalCenter: parent.horizontalCenter
                      PrimeRadiant {
                        anchors.fill: parent
                        tint: root.mark
                        voiceState: "listening"
                        level: 0.5
                      }
                    }

                    // Fourteen marks, the shape the real waveform is built
                    // from, breathing rather than reacting to a microphone
                    // that is not open.
                    Row {
                      id: figure
                      anchors.horizontalCenter: parent.horizontalCenter
                      spacing: Style.spaceReal(4)
                      property real phase: 0

                      NumberAnimation on phase {
                        running: root.open && root.page === 0
                        loops: Animation.Infinite
                        from: 0; to: Math.PI * 2
                        duration: 2600
                      }

                      Repeater {
                        model: 14
                        Rectangle {
                          required property int index
                          readonly property real wave:
                            0.35 + 0.65 * Math.abs(Math.sin(figure.phase + index * 0.42))
                          width: Style.spaceReal(3)
                          height: Style.spaceReal(4) + Style.spaceReal(22) * wave
                          anchors.verticalCenter: parent.verticalCenter
                          color: root.mark
                          opacity: 0.35 + 0.5 * wave
                        }
                      }
                    }

                    Text {
                      anchors.horizontalCenter: parent.horizontalCenter
                      text: "“what is taking up space in here?”"
                      textFormat: Text.PlainText
                      color: root.inkFaint
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }

                Link { label: "audio"; dots: 3 }

                Node {
                  title: "OpenAI Realtime API"
                  detail: "gpt-realtime · billed per minute of audio"
                }

                Row {
                  anchors.horizontalCenter: parent.horizontalCenter
                  spacing: Style.spaceReal(8)
                  Chip { text: "your own API key"; tone: root.mark }
                  Chip { text: "a ChatGPT subscription does not work here" }
                }
              }
            }

            // ---- 2. the brain ------------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap

                Heading {
                  eyebrow: "The brain"
                  title: "Every question of fact goes to your own agent."
                }

                Node {
                  title: "the voice"
                  detail: "forbidden from answering"
                  tone: root.inkDim
                  span: 0.62
                }

                Link { label: "ask_agent" }

                // The two agents as they appear everywhere else in the plugin,
                // in their vendors' own colours.
                Rectangle {
                  width: parent.width
                  height: Style.space(74)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.45)

                  Column {
                    anchors.centerIn: parent
                    spacing: Style.spaceReal(10)

                    // Each badge at its own size. The badge is a pill that
                    // already carries both the vendor's glyph and the agent's
                    // name; the box it was in before squeezed the pill to a
                    // square and pushed its label out through the sides.
                    Row {
                      anchors.horizontalCenter: parent.horizontalCenter
                      spacing: Style.spaceReal(14)

                      AgentBadge { agent: "codex" }
                      AgentBadge { agent: "claude" }
                    }

                    Text {
                      anchors.horizontalCenter: parent.horizontalCenter
                      text: "install one — whichever you already use"
                      textFormat: Text.PlainText
                      color: root.inkDim
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }

                Link { label: "reads" }

                Node {
                  title: "whatever that agent already reaches"
                  detail: "its own connectors, its own MCP servers, its own settings"
                  tone: root.inkDim
                }

                Note {
                  text: "Nothing is configured twice. The agent you have set up "
                      + "is the agent that answers."
                }
              }
            }

            // ---- 3. the folder -----------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap

                Heading {
                  eyebrow: "The folder"
                  title: "One folder is the context — and the wall."
                }

                Rectangle {
                  width: parent.width
                  height: Style.space(126)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.5)

                  Column {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Style.spaceReal(16)
                    spacing: Style.spaceReal(9)

                    Text {
                      text: root.workspace === ""
                        ? "~/Projects/site"
                        : root.workspace
                      textFormat: Text.PlainText
                      elide: Text.ElideMiddle
                      width: parent.width
                      color: root.mark
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                    }

                    PathRow { path: "src/main.rs";     inside: true }
                    PathRow { path: "notes/todo.md";   inside: true }
                    PathRow { path: "README.md";       inside: true }
                  }
                }

                Column {
                  width: parent.width
                  spacing: Style.spaceReal(9)

                  Text {
                    text: "everywhere else"
                    textFormat: Text.PlainText
                    color: root.inkFaint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    font.letterSpacing: 1
                  }

                  PathRow { path: "~/.ssh/id_ed25519"; inside: false }
                  PathRow { path: "~/Documents";       inside: false }
                }

                Note {
                  text: "Outside the folder a file does not come back empty — it "
                      + "does not exist. Choose the folder in Settings ▸ Access."
                }
              }
            }

            // ---- 4. autonomy -------------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap

                Heading {
                  eyebrow: "Autonomy"
                  title: "You set how far each agent may reach."
                }

                Rectangle {
                  width: parent.width
                  height: Style.space(158)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: root.hairline

                  Column {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Style.spaceReal(16)
                    spacing: Style.spaceReal(16)

                    Grant { agent: "codex";  choice: 1 }
                    Grant { agent: "claude"; choice: 0 }
                  }
                }

                Note {
                  text: "Each agent is off until you allow it by name. Held to the "
                      + "folder is the default; widening it is one switch, and it "
                      + "says so."
                }

                Row {
                  spacing: Style.spaceReal(8)
                  Chip { text: "never writes, in any setting"; tone: root.mark }
                  Chip { text: "changeable any time" }
                }
              }
            }

            // ---- 5. keys and colour ------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap

                Heading {
                  eyebrow: "Keys & colour"
                  title: "Five letters, and a light that says what is happening."
                }

                Row {
                  width: parent.width
                  spacing: Style.spaceReal(26)

                  Column {
                    spacing: Style.spaceReal(9)
                    KeyCap { key: "Esc"; what: "put it away" }
                    KeyCap { key: "Q";   what: "stop listening" }
                    KeyCap { key: "N";   what: "forget and start over" }
                    KeyCap { key: "I";   what: "cut the answer off" }
                    KeyCap { key: "H";   what: "how this works" }
                  }

                  Column {
                    spacing: Style.spaceReal(11)

                    Repeater {
                      model: [
                        { state: "idle",      name: "resting",   what: "nothing running" },
                        { state: "listening", name: "listening", what: "hearing you" },
                        { state: "thinking",  name: "thinking",  what: "asking the agent" },
                        { state: "speaking",  name: "speaking",  what: "its turn" },
                        { state: "error",     name: "broken",    what: "N starts over" }
                      ]

                      Row {
                        id: hueRow
                        required property var modelData
                        spacing: Style.spaceReal(9)
                        // A real colour, not the string "black": colorFor reads
                        // .r/.g/.b off it to decide how light the hue should be,
                        // and a string makes that NaN — which lands on the dark
                        // branch and paints every state too dark to read here.
                        readonly property color hue: hues.colorFor(
                          hueRow.modelData.state, Color.menu.background, root.mark)

                        Rectangle {
                          width: Style.spaceReal(8)
                          height: Style.spaceReal(8)
                          anchors.verticalCenter: parent.verticalCenter
                          color: hueRow.hue
                        }

                        Text {
                          width: Style.spaceReal(66)
                          anchors.verticalCenter: parent.verticalCenter
                          text: hueRow.modelData.name
                          textFormat: Text.PlainText
                          color: hueRow.hue
                          font.family: Style.font.family
                          font.pixelSize: Style.font.bodySmall
                        }

                        Text {
                          anchors.verticalCenter: parent.verticalCenter
                          text: hueRow.modelData.what
                          textFormat: Text.PlainText
                          color: root.inkDim
                          font.family: Style.font.family
                          font.pixelSize: Style.font.caption
                        }
                      }
                    }
                  }
                }

                Note {
                  text: "Plain letters, because the panel takes the keyboard while "
                      + "it is open. A halo around the crystal in the bar means the "
                      + "microphone is open."
                }
              }
            }
          }
        }

        // -- dots, skip, done ---------------------------------------------------

        Item {
          id: foot
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          height: Style.spaceReal(30)

          Text {
            id: skip
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            visible: root.page + 1 < root.pages
            text: "Skip"
            textFormat: Text.PlainText
            color: skipHover.hovered ? root.ink : root.inkFaint
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall

            HoverHandler { id: skipHover }
            TapHandler { onTapped: root.closed() }
          }

          Row {
            anchors.centerIn: parent
            spacing: Style.spaceReal(8)

            Repeater {
              model: root.pages

              Rectangle {
                id: dot
                required property int index
                readonly property bool here: dot.index === root.page
                // The one you are on is a dash, not a brighter dot: on a
                // five-dot row a difference in colour alone is a difference
                // nobody counts.
                width: Style.spaceReal(dot.here ? 16 : 5)
                height: Style.spaceReal(5)
                radius: Style.spaceReal(2)
                anchors.verticalCenter: parent.verticalCenter
                color: dot.here ? root.mark : root.inkFaint

                Behavior on width {
                  NumberAnimation { duration: 200; easing.type: Easing.OutCubic }
                }

                TapHandler { onTapped: root.page = dot.index }
              }
            }
          }

          Rectangle {
            id: nextButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: nextLabel.implicitWidth + Style.spaceReal(26)
            height: nextLabel.implicitHeight + Style.spaceReal(11)
            radius: Style.spaceReal(4)
            color: nextHover.hovered
              ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.18)
              : Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.08)
            border.width: 1
            border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)

            Text {
              id: nextLabel
              anchors.centerIn: parent
              text: root.page + 1 < root.pages ? "Next" : "Start talking"
              textFormat: Text.PlainText
              color: root.mark
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            HoverHandler { id: nextHover }
            TapHandler { onTapped: root.next() }
          }
        }

        // Dismiss in the card's own corner. The scrim and Esc do the same
        // thing; this is the one a person looks for first. The headings stop
        // short of it rather than running underneath.
        Text {
          id: closeMark
          anchors.top: parent.top
          anchors.right: parent.right
          text: "✕"
          textFormat: Text.PlainText
          color: closeHover.hovered ? root.ink : root.inkFaint
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle

          HoverHandler { id: closeHover }
          TapHandler { onTapped: root.closed() }
        }
        }
      }
    }
  }
}
