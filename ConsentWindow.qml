pragma ComponentBehavior: Bound

// What this assistant may do, asked once and in plain words.
//
// The honest description of this program is uncomfortable enough to be worth
// stating outright: it takes a sentence said out loud near a laptop and hands
// it to the coding agent already installed on that laptop. Whatever that agent
// can reach — your files, the connectors you set up for it, the web — it can
// reach on behalf of a sentence. That is the entire point, and it is also the
// entire risk. A room can be overheard. A transcription can be wrong.
//
// So there are two questions here and no defaults for either. Which folder,
// and which agent. Neither can be guessed on someone's behalf: the folder that
// is safe to work in is a fact about a person's life rather than about their
// filesystem, and permission that was assumed is not permission.
//
// Asked once, not per question. A confirmation dialog in front of every
// spoken sentence would end up being clicked without being read, which buys
// the appearance of consent at the cost of the thing itself — and it would
// destroy the one property that makes talking to a computer worth doing,
// which is that you can do it while your hands are busy.

import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  property bool open: false
  property string workspace: ""
  property var consented: []
  property var folders: []
  property string backend: "codex"

  signal closed()
  signal folderPicked(string path)
  signal consentChanged(string agent, bool granted)
  signal refreshed()

  function allowed(name) {
    return root.consented.indexOf(String(name)) >= 0
  }

  onOpenChanged: if (open) {
    root.refreshed()
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  // One agent, and the sentence you are agreeing to about it. The button says
  // what will be true afterwards rather than what it does, because "Allow" and
  // "Allowed" a pixel apart is how people grant things by accident.
  component Grant: Rectangle {
    id: grant
    required property string agent
    required property string detail
    required property bool granted

    signal toggled(bool value)

    width: parent ? parent.width : 0
    height: grantBody.implicitHeight + Style.spaceReal(22)
    radius: Style.spaceReal(6)
    color: "transparent"
    border.width: 1
    border.color: grant.granted
      ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.5)
      : Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.18)
    Behavior on border.color { ColorAnimation { duration: 160 } }

    Column {
      id: grantBody
      anchors.centerIn: parent
      width: parent.width - Style.spaceReal(28)
      spacing: Style.spaceReal(9)

      Row {
        width: parent.width
        spacing: Style.spaceReal(10)

        AgentBadge {
          id: grantBadge
          agent: grant.agent
          anchors.verticalCenter: parent.verticalCenter
          // Dimmed to say "not yet", not to say "absent". At 0.4 on a light
          // theme the mark stopped being readable, and an agent you cannot
          // identify is a poor thing to be granting anything to.
          opacity: grant.granted ? 1 : 0.65
          Behavior on opacity { NumberAnimation { duration: 160 } }
        }

        Text {
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - grantBadge.width - grantButton.width - parent.spacing * 2
          text: grant.granted
            ? "Allowed to answer, in the folder above."
            : "Not allowed. Questions meant for it are refused."
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          color: grant.granted ? Color.accent : Color.menu.text
          opacity: grant.granted ? 0.9 : 0.5
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }

        Button {
          id: grantButton
          anchors.verticalCenter: parent.verticalCenter
          text: grant.granted ? "Withdraw" : "Allow"
          bordered: true
          foreground: Color.menu.text
          accent: Color.accent
          fontFamily: Style.font.family
          onClicked: grant.toggled(!grant.granted)
        }
      }

      Text {
        width: parent.width
        text: grant.detail
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: Color.menu.text
        opacity: 0.55
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
  }

  PanelWindow {
    id: window
    visible: root.open
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice-consent"
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
      width: Style.space(560)
      height: Math.min(Style.space(680), parent.height - Style.space(80))
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
          if (event.key === Qt.Key_Escape) {
            root.closed()
            event.accepted = true
          }
        }

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
            PrimeRadiant { anchors.fill: parent; tint: Color.accent; voiceState: "thinking" }
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "What it may do"
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
          text: "Esc — close · both answers can be changed here at any time"
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
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

            Text {
              width: parent.width
              text: "This does not answer anything itself. It listens, and hands "
                  + "what it heard to the coding agent already installed on this "
                  + "machine — the same one you type to. So it can do what that "
                  + "agent can do, including the connectors you have set up for "
                  + "it.\n\n"
                  + "Which is worth saying plainly, because speech is not "
                  + "typing: a room can be overheard, and a transcription can "
                  + "be wrong. Two answers are needed before anything is asked "
                  + "of the agent, and neither is guessed for you."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.8
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            PanelSeparator { width: parent.width }

            PanelSectionHeader { width: parent.width; text: "1 · Where it works" }

            Text {
              width: parent.width
              text: "The agent starts here and treats it as the work at hand, "
                  + "instead of starting in your home directory as it used to. "
                  + "It decides what a question is about — not, on its own, what "
                  + "the agent is able to read. The two paragraphs below say "
                  + "exactly what each one enforces."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.6
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            Rectangle {
              width: parent.width
              height: chosenText.implicitHeight + Style.spaceReal(18)
              radius: Style.spaceReal(5)
              color: "transparent"
              border.width: 1
              border.color: root.workspace === ""
                ? Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.45)
                : Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.45)

              Text {
                id: chosenText
                anchors.centerIn: parent
                width: parent.width - Style.spaceReal(24)
                text: root.workspace === "" ? "No folder chosen yet" : root.workspace
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                horizontalAlignment: Text.AlignHCenter
                color: root.workspace === "" ? Color.urgent : Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }
            }

            Flow {
              id: folderFlow
              width: parent.width
              spacing: Style.spaceReal(6)

              Repeater {
                model: root.folders

                Rectangle {
                  id: folderChip
                  required property var modelData
                  readonly property bool selected: String(folderChip.modelData.path) === root.workspace
                  width: Math.min(folderLabel.implicitWidth + Style.spaceReal(20), folderFlow.width)
                  height: folderLabel.implicitHeight + Style.spaceReal(10)
                  radius: Style.spaceReal(4)
                  color: folderChip.selected
                    ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.14)
                    : "transparent"
                  border.width: 1
                  border.color: folderChip.selected
                    ? Color.accent
                    : Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b,
                              folderHover.hovered ? 0.4 : 0.18)

                  Text {
                    id: folderLabel
                    anchors.centerIn: parent
                    width: Math.min(implicitWidth, folderFlow.width - Style.spaceReal(20))
                    text: String(folderChip.modelData.label)
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: folderChip.selected ? Color.accent : Color.menu.text
                    opacity: folderChip.selected ? 1 : 0.65
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                  }

                  HoverHandler { id: folderHover }
                  TapHandler {
                    onTapped: root.folderPicked(String(folderChip.modelData.path))
                  }
                }
              }
            }

            Row {
              width: parent.width
              spacing: Style.spaceReal(8)

              TextField {
                id: folderField
                width: parent.width - folderButton.width - parent.spacing
                placeholderText: "…or type a path"
                foreground: Color.menu.text
                accent: Color.accent
                onAccepted: root.folderPicked(folderField.text)
              }

              Button {
                id: folderButton
                anchors.verticalCenter: parent.verticalCenter
                text: "Use"
                bordered: true
                foreground: Color.menu.text
                accent: Color.accent
                fontFamily: Style.font.family
                onClicked: root.folderPicked(folderField.text)
              }
            }

            PanelSeparator { width: parent.width }

            PanelSectionHeader { width: parent.width; text: "2 · Which agent may answer" }

            Text {
              width: parent.width
              text: "Asked separately for each, because they are not the same "
                  + "program and are not connected to the same things. Allowing "
                  + "one says nothing about the other."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.6
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            Grant {
              agent: "codex"
              granted: root.allowed("codex")
              detail: "Runs on your ChatGPT subscription with everything your own "
                    + "codex configuration gives it. Started in the folder above, "
                    + "and it cannot write anywhere at all. Its sandbox bounds "
                    + "writing and not reading, so a question that leads to a "
                    + "file elsewhere on this machine will reach one."
              onToggled: function (value) { root.consentChanged("codex", value) }
            }

            Grant {
              agent: "claude"
              granted: root.allowed("claude")
              detail: "Brings your skills and MCP connectors — mail, calendar, "
                    + "whatever else you have connected. Started in the folder "
                    + "above, and it cannot edit or create files. It can read "
                    + "past the folder in the same way codex can."
              onToggled: function (value) { root.consentChanged("claude", value) }
            }

            Text {
              width: parent.width
              text: "Those two paragraphs were measured rather than assumed — "
                  + "both agents were handed a file outside the folder to see "
                  + "what they would do with it. So the permission above is the "
                  + "control that means something, and it is why nothing at all "
                  + "is asked of an agent you have not allowed.\n\n"
                  + "Nothing is sent anywhere except OpenAI, for the voice, and "
                  + "the agent you allowed."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.35
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }
}
