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
  signal tourRequested()

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

  StateHues { id: hues }

  // One state of the crystal: the mark, in the colour it actually uses.
  component Hue: Row {
    id: hue
    required property string state
    required property string name
    required property string what
    width: parent ? parent.width : 0
    spacing: Style.spaceReal(10)

    Rectangle {
      width: Style.spaceReal(9)
      height: Style.spaceReal(9)
      anchors.verticalCenter: parent.verticalCenter
      color: hues.colorFor(hue.state, Color.menu.background, Color.accent)
    }

    Text {
      width: Style.spaceReal(88)
      text: hue.name
      textFormat: Text.PlainText
      color: hues.colorFor(hue.state, Color.menu.background, Color.accent)
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Text {
      width: hue.width - Style.spaceReal(117)
      text: hue.what
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      color: Color.menu.text
      opacity: 0.75
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
        // The tour is the picture version of this window, and this window is
        // where somebody lands when they want it. Offering it only in settings
        // put it behind a heading nobody opens looking for an explanation.
        //
        // A sibling of the head rather than a child of it: the head is a Row,
        // and a Row positions its children itself — an anchored one fights it.
        Button {
          id: tourButton
          anchors.right: parent.right
          anchors.verticalCenter: head.verticalCenter
          text: "Introduction…"
          bordered: true
          foreground: Color.menu.text
          accent: Color.accent
          fontFamily: Style.font.family
          fontSize: Style.font.bodySmall
          onClicked: root.tourRequested()
        }

        Row {
          id: head
          anchors.top: parent.top
          anchors.left: parent.left
          // Stops at the button rather than at the edge, so a longer title in
          // some other language runs out of room instead of running under it.
          anchors.right: tourButton.left
          anchors.rightMargin: Style.spaceReal(12)
          // The button is the taller of the two; the scrolling body starts
          // below both, or it would slide under it.
          height: Math.max(head.implicitHeight, tourButton.height)
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
                what: "Puts the panel away and stops nothing. A click outside "
                    + "does the same."
              }
              KeyRow {
                key: "Q"
                what: "Stops listening and speaking. The conversation is kept."
              }
              KeyRow {
                key: "N"
                what: "A new conversation. Everything said so far is forgotten."
              }
              KeyRow {
                key: "I"
                what: "Cuts off an answer running long. Talking over it does "
                    + "the same."
              }
              KeyRow {
                key: "H"
                what: "This window."
              }
            }

            Text {
              width: parent.width
              text: "Plain letters, because the panel takes the keyboard while "
                  + "it is open. Q is also on the crystal in the bar: "
                  + "right-click it to stop without opening anything."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.45
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSectionHeader { width: parent.width; text: "The crystal in the bar" }

            Text {
              width: parent.width
              text: "It is a status light. The colour is the state; a halo "
                  + "around it means the microphone is open — whether or not "
                  + "the panel is on screen."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.75
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            Column {
              width: parent.width
              spacing: Style.spaceReal(9)

              // Named by state and not by colour. The swatch already says
              // which colour it is, and two of these are close enough that
              // reading "green" beside a green square and "resting" beside
              // another one is a puzzle rather than a legend.
              Hue { state: "idle";      name: "resting";   what: "Nothing running." }
              Hue { state: "listening"; name: "listening"; what: "Open, and hearing you." }
              Hue { state: "thinking";  name: "thinking";  what: "Away, asking the agent." }
              Hue { state: "speaking";  name: "speaking";  what: "Its turn to talk." }
              Hue { state: "error";     name: "broken";    what: "The connection went. N starts over." }
            }

            Text {
              width: parent.width
              text: "Left-click opens the panel, middle-click switches agent, "
                  + "right-click stops."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.45
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSectionHeader { width: parent.width; text: "Long conversations cost more" }

            Text {
              width: parent.width
              text: "The voice re-sends the whole conversation every turn, so "
                  + "the length is charged and not just the talking. Press N "
                  + "when the subject changes: a second to reconnect, and the "
                  + "running total goes back to nothing."
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
              text: "The voice is OpenAI's Realtime API. It hears and speaks, "
                  + "and is forbidden from answering questions of fact — those "
                  + "go to codex or claude, already installed here. Whatever "
                  + "that agent can reach, this can use.\n\n"
                  + "Which is why it asks first: an agent is started only after "
                  + "you name a folder and allow that agent by name, in "
                  + "Settings ▸ Access. Until you widen it there, the folder is "
                  + "also as far as it can read.\n\n"
                  + "While it works, its own narration shows faintly behind the "
                  + "figure."
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
              text: "Splitting it this way is what keeps it cheap: the audio "
                  + "tokens are spent on speech rather than on thinking."
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
