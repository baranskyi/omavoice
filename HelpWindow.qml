pragma ComponentBehavior: Bound

// What the keys do, and what is actually happening when you talk to this.
//
// A window rather than a tooltip, and the same window the settings live in:
// the questions it answers — what does Q do, why is a long conversation
// expensive, where does the answer come from — are the kind you go and look up
// once, not the kind you hover over.
//
// The diagram is drawn in the panel's own language: thin-bordered nodes and
// connections made of square dots, the same mark the waveform is built from.
// A boxes-and-arrows picture in some other style would be a diagram *about*
// this program rather than a part of it.

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

  onOpenChanged: if (open) Qt.callLater(function () { keyCatcher.forceActiveFocus() })

  // One row of the key table. The key itself is set in the same monospace the
  // panel uses everywhere, boxed, so it reads as a key and not as a word.
  component KeyRow: Row {
    id: keyRow
    required property string key
    required property string what
    width: parent ? parent.width : 0
    spacing: Style.spaceReal(12)

    Rectangle {
      width: Style.spaceReal(46)
      height: keyLabel.implicitHeight + Style.spaceReal(8)
      radius: Style.spaceReal(4)
      color: "transparent"
      border.width: 1
      border.color: Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.28)

      Text {
        id: keyLabel
        anchors.centerIn: parent
        text: keyRow.key
        textFormat: Text.PlainText
        color: Color.accent
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
    }

    Text {
      width: keyRow.width - Style.spaceReal(58)
      text: keyRow.what
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      color: Color.menu.text
      opacity: 0.85
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }
  }

  // A node in the diagram.
  component Node: Rectangle {
    id: node
    required property string title
    property string detail: ""
    property color mark: Color.accent
    width: parent ? parent.width : 0
    height: nodeBody.implicitHeight + Style.spaceReal(16)
    radius: Style.spaceReal(5)
    color: "transparent"
    border.width: 1
    border.color: Qt.rgba(node.mark.r, node.mark.g, node.mark.b, 0.45)

    Column {
      id: nodeBody
      anchors.centerIn: parent
      width: parent.width - Style.spaceReal(24)
      spacing: Style.spaceReal(3)

      Text {
        width: parent.width
        text: node.title
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        color: node.mark
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
        color: Color.menu.text
        opacity: 0.5
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
  }

  // The link between two nodes: square dots, the waveform's own mark, with the
  // name of what travels along it.
  component Link: Item {
    id: link
    required property string label
    width: parent ? parent.width : 0
    height: Style.spaceReal(34)

    Column {
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.spaceReal(3)

      Repeater {
        model: 4
        Rectangle {
          required property int index
          width: Style.spaceReal(3)
          height: Style.spaceReal(3)
          anchors.horizontalCenter: parent.horizontalCenter
          color: Color.accent
          opacity: 0.25 + index * 0.14
        }
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      anchors.left: parent.horizontalCenter
      anchors.leftMargin: Style.spaceReal(14)
      text: link.label
      textFormat: Text.PlainText
      color: Color.menu.text
      opacity: 0.45
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  PanelWindow {
    id: window
    visible: root.open
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice-help"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim.a > 0.05
        ? Color.menu.scrim
        : Qt.rgba(Color.background.r, Color.background.g, Color.background.b, 0.62)
      MouseArea { anchors.fill: parent; onClicked: root.closed() }
    }

    BorderSurface {
      id: card
      anchors.centerIn: parent
      width: Style.space(520)
      height: Math.min(Style.space(620), parent.height - Style.space(80))
      radius: Style.cornerRadius
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        clip: true
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function (event) {
          if (event.key === Qt.Key_Escape || event.key === Qt.Key_H) {
            root.closed()
            event.accepted = true
          }
        }

        // Head and foot are pinned, the middle scrolls. A window that grows
        // past the screen and pushes its own closing instruction off the
        // bottom is a trap, and this one has a lot to say.
        Row {
          id: head
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spaceReal(8)

          Item {
            width: Style.spaceReal(18)
            height: Style.spaceReal(18)
            anchors.verticalCenter: parent.verticalCenter
            PrimeRadiant { anchors.fill: parent; tint: Color.accent; voiceState: "listening" }
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "How this works"
            textFormat: Text.PlainText
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.title
          }
        }

        Text {
          id: foot
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          text: "Esc — close"
          textFormat: Text.PlainText
          color: Color.menu.text
          opacity: 0.3
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }

        Flickable {
          id: scroller
          anchors.top: head.bottom
          anchors.topMargin: Style.spacing.panelGap
          anchors.bottom: foot.top
          anchors.bottomMargin: Style.spacing.panelGap
          anchors.left: parent.left
          anchors.right: parent.right
          contentHeight: body.implicitHeight
          clip: true
          interactive: contentHeight > height
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: body
            width: scroller.width
            spacing: Style.spacing.panelGap

            PanelSectionHeader { width: parent.width; text: "Keys" }

            Column {
              width: parent.width
              spacing: Style.spaceReal(10)

              KeyRow {
                key: "Esc"
                what: "Puts the panel away and stops nothing. The microphone "
                    + "stays open, the conversation carries on, the answer is "
                    + "still spoken. A click outside the panel does the same."
              }
              KeyRow {
                key: "Q"
                what: "Stops. The microphone is released, whatever the "
                    + "assistant was saying is cut off, and a question the "
                    + "agent is still working on is abandoned — but the "
                    + "conversation is kept, and opening the panel again picks "
                    + "it up where it left off."
              }
              KeyRow {
                key: "N"
                what: "A new conversation. Everything said so far is "
                    + "forgotten — by the voice, by the agent, and by this "
                    + "panel."
              }
              KeyRow {
                key: "I"
                what: "Interrupts an answer that is running long, without "
                    + "leaving the conversation. Talking over it does the "
                    + "same thing."
              }
              KeyRow {
                key: "H"
                what: "This window."
              }
            }

            Text {
              width: parent.width
              text: "All of them are plain letters, because the panel takes the "
                  + "keyboard for itself while it is open — a combination "
                  + "assigned elsewhere on the desktop would simply vanish here."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.4
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSectionHeader { width: parent.width; text: "Long conversations cost more" }

            Text {
              width: parent.width
              text: "The voice keeps the whole conversation on its connection "
                  + "and sends it again with every turn, so a discussion gets "
                  + "steadily more expensive the longer it runs — the length "
                  + "is charged, not just the talking.\n\n"
                  + "Press N when the subject changes. It costs a second to "
                  + "reconnect and takes the running total back to nothing."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.8
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            PanelSectionHeader { width: parent.width; text: "Where the answers come from" }

            Text {
              width: parent.width
              text: "The voice is OpenAI's Realtime API. It hears you and "
                  + "speaks, and that is all it does — its instructions forbid "
                  + "it from answering questions of fact.\n\n"
                  + "Everything with substance goes to the agent already "
                  + "installed on this machine: codex or claude, whichever is "
                  + "selected. It reads and reports; neither one can write, "
                  + "edit or delete anything.\n\n"
                  + "That means whatever the agent can already reach, this can "
                  + "use — your files and projects, and the connectors set up "
                  + "for it: mail, calendar, MCP servers, the rest. Nothing "
                  + "was configured twice. Asking out loud reaches the same "
                  + "assistant you have been typing to.\n\n"
                  + "Which is also why it asks first. An agent is only ever "
                  + "started after you have named a folder for it to work in "
                  + "and allowed that particular agent by name — separately for "
                  + "codex and for claude, once, in Settings under Access."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.8
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            PanelSectionHeader { width: parent.width; text: "The path a question takes" }

            Column {
              width: parent.width
              spacing: 0

              Node {
                title: "you"
                detail: "speaking into the microphone"
                mark: Color.menu.text
              }
              Link { label: "voice" }
              Node {
                title: "Realtime API"
                detail: "hears and speaks · never answers"
                mark: Color.accent
              }
              Link { label: "ask_agent" }
              Node {
                title: root.backend
                detail: root.workspace === ""
                  ? "the agent on this machine · never writes"
                  : "in " + root.workspace.split("/").pop() + " · never writes"
                mark: Color.accent
              }
              Link { label: "reads" }
              Node {
                title: "your machine"
                detail: "files · mail · calendar · MCP · the web"
                mark: Color.menu.text
              }
            }

            Text {
              width: parent.width
              text: "Splitting it this way is what makes the assistant local, "
                  + "and what keeps it cheap: the audio tokens — by far the "
                  + "expensive ones — are spent on speech rather than on "
                  + "thinking."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.4
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }
}
