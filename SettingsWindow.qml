pragma ComponentBehavior: Bound

// Settings, as a window of its own.
//
// Its own layer-shell surface rather than a section that unfolds inside the
// conversation panel: settings are a place you go, not a thing that happens
// while you are talking, and expanding the panel mid-conversation pushed the
// answer you were reading off the screen.
//
// English throughout, and deliberately so. The desktop this runs on is
// English, and the plugin is meant to be installable by anyone — the interface
// language is a property of the plugin, while the conversation language
// follows whoever is speaking.

import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  property bool open: false
  property bool hasKey: false
  property var voices: []
  property string currentVoice: ""
  property string backend: "codex"
  property var audioSources: []
  property string audioInput: ""
  property string audioResolved: ""
  property string keyError: ""
  property string workspace: ""
  property var consented: []
  property var unrestricted: []

  signal closed()
  signal voicePicked(string name)
  signal backendPicked(string name)
  signal inputPicked(string name)
  signal keySubmitted(string key)
  signal accessRequested()

  onOpenChanged: {
    if (open) {
      keyField.text = ""
      root.keyError = ""
      Qt.callLater(function () {
        if (!root.hasKey) keyField.forceActiveFocus()
        else keyCatcher.forceActiveFocus()
      })
    }
  }

  PanelWindow {
    id: window
    visible: root.open
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice-settings"
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
      width: Style.space(460)
      height: Math.min(
        Style.space(560),
        body.implicitHeight + card.contentTopInset + card.contentBottomInset
      )
      radius: Style.cornerRadius
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: {} }
      Behavior on height { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }

      Item {
        id: keyCatcher
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function (event) {
          if (event.key === Qt.Key_Escape) {
            root.closed()
            event.accepted = true
          }
        }

        Flickable {
          id: scroller
          anchors.fill: parent
          contentHeight: body.implicitHeight
          clip: true
          interactive: contentHeight > height
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: body
            width: scroller.width
            spacing: Style.spacing.panelGap

            Row {
              width: parent.width
              spacing: Style.spaceReal(8)

              Item {
                width: Style.spaceReal(18)
                height: Style.spaceReal(18)
                anchors.verticalCenter: parent.verticalCenter
                PrimeRadiant { anchors.fill: parent; tint: Color.accent; voiceState: "listening" }
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Voice settings"
                textFormat: Text.PlainText
                color: Color.menu.text
                font.family: Style.font.family
                font.pixelSize: Style.font.title
              }
            }

            PanelSeparator { width: parent.width }

            // --- connection -------------------------------------------------
            PanelSectionHeader { width: parent.width; text: "OpenAI API key" }

            Text {
              width: parent.width
              text: root.hasKey
                ? "A key is configured. Paste a new one to replace it."
                : "The Realtime API needs a paid API key — a ChatGPT subscription does not work for it. The key is stored in ~/.config/omavoice/env with mode 600."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.55
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            // Where the key actually comes from. Obvious once you have done it
            // once, and a dead end if you have not — most people have never seen
            // the API platform, which is a different site from the ChatGPT they
            // already pay for.
            Row {
              spacing: Style.spaceReal(5)

              Text {
                id: keyLink
                text: "platform.openai.com/api-keys"
                textFormat: Text.PlainText
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                font.underline: linkHover.hovered
              }

              Text {
                anchors.verticalCenter: keyLink.verticalCenter
                text: "↗"
                textFormat: Text.PlainText
                color: Color.accent
                opacity: 0.7
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              HoverHandler {
                id: linkHover
                cursorShape: Qt.PointingHandCursor
              }
              // execArgv, not a shell string: the URL is a constant here, but
              // this is the habit that keeps the one that is not from biting.
              TapHandler {
                onTapped: Util.execArgv(["xdg-open", "https://platform.openai.com/api-keys"])
              }
            }

            Row {
              width: parent.width
              spacing: Style.spaceReal(8)

              TextField {
                id: keyField
                width: parent.width - saveButton.width - parent.spacing
                // Masked: this is pasted on a screen that may be shared, and it
                // is never displayed again once saved.
                password: true
                placeholderText: "sk-..."
                foreground: Color.menu.text
                accent: Color.accent
                onAccepted: root.keySubmitted(keyField.text)
              }

              Button {
                id: saveButton
                anchors.verticalCenter: parent.verticalCenter
                text: "Save"
                bordered: true
                foreground: Color.menu.text
                accent: Color.accent
                fontFamily: Style.font.family
                onClicked: root.keySubmitted(keyField.text)
              }
            }

            Text {
              width: parent.width
              visible: root.keyError !== ""
              text: root.keyError
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSeparator { width: parent.width }

            // --- voice --------------------------------------------------------
            PanelSectionHeader { width: parent.width; text: "Voice" }

            Text {
              width: parent.width
              visible: !root.hasKey
              text: "Add a key first — voices are read from the connected account."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.45
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            Flow {
              width: parent.width
              visible: root.hasKey
              spacing: Style.spaceReal(6)

              Repeater {
                model: root.voices

                Rectangle {
                  id: chip
                  required property var modelData

                  readonly property bool selected: String(chip.modelData.name) === root.currentVoice
                  readonly property bool female: String(chip.modelData.gender) === "female"

                  implicitWidth: chipRow.implicitWidth + Style.spaceReal(16)
                  implicitHeight: chipRow.implicitHeight + Style.spaceReal(9)
                  radius: Style.spaceReal(5)

                  color: chip.selected
                    ? Style.selectedFillFor(Color.menu.text, Color.accent)
                    : (hover.hovered ? Style.hoverFillFor(Color.menu.text, Color.accent) : "transparent")
                  border.width: 1
                  border.color: chip.selected
                    ? Color.accent
                    : Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.18)

                  Behavior on color { ColorAnimation { duration: 140 } }

                  Row {
                    id: chipRow
                    anchors.centerIn: parent
                    spacing: Style.spaceReal(5)

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: String(chip.modelData.name)
                      textFormat: Text.PlainText
                      color: chip.selected ? Color.accent : Color.menu.text
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: chip.female ? "♀" : "♂"
                      textFormat: Text.PlainText
                      color: chip.selected ? Color.accent : Color.menu.text
                      opacity: 0.5
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }

                  HoverHandler { id: hover }
                  TapHandler { onTapped: root.voicePicked(String(chip.modelData.name)) }
                }
              }
            }

            Text {
              width: parent.width
              visible: root.hasKey
              // Worth stating: in Russian, Hebrew, Spanish and others the
              // assistant's own verbs change with this choice.
              text: "Changing the voice reconnects the session. In languages that mark gender, the assistant speaks about itself to match."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.4
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSeparator { width: parent.width }

            // --- agent --------------------------------------------------------
            // --- microphone ---------------------------------------------------
            PanelSectionHeader { width: parent.width; text: "Microphone" }

            Text {
              width: parent.width
              // The one line that used to live only in the log, and whose absence
              // made a changed desk look like a broken assistant.
              text: root.audioResolved !== ""
                ? root.audioResolved
                : "Chosen when a conversation starts."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.45
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            Flow {
              id: micFlow
              width: parent.width
              spacing: Style.spaceReal(6)

              Repeater {
                model: [{ name: "", label: "Follow the system" }].concat(root.audioSources)

                Rectangle {
                  id: mic
                  required property var modelData

                  readonly property bool selected: String(mic.modelData.name) === root.audioInput
                  readonly property bool auto: String(mic.modelData.name) === ""

                  // Measured from the row, never from the chip: sizing the label
                  // against its own chip closes a binding loop, and QML answers a
                  // loop by leaving every width at zero — which stacks the whole
                  // list in one spot.
                  readonly property real maxLabel: micFlow.width - Style.spaceReal(24)

                  implicitWidth: micLabel.width + Style.spaceReal(16)
                  implicitHeight: micLabel.implicitHeight + Style.spaceReal(9)
                  radius: Style.spaceReal(5)

                  color: mic.selected
                    ? Style.selectedFillFor(Color.menu.text, Color.accent)
                    : (micHover.hovered ? Style.hoverFillFor(Color.menu.text, Color.accent) : "transparent")
                  border.width: 1
                  border.color: mic.selected
                    ? Color.accent
                    : Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.18)

                  Behavior on color { ColorAnimation { duration: 140 } }

                  Text {
                    id: micLabel
                    anchors.centerIn: parent
                    width: Math.min(implicitWidth, mic.maxLabel)
                    elide: Text.ElideRight
                    horizontalAlignment: Text.AlignHCenter
                    text: String(mic.modelData.label || mic.modelData.name)
                    textFormat: Text.PlainText
                    color: mic.selected ? Color.accent : Color.menu.text
                    opacity: mic.selected ? 1 : (mic.auto ? 0.8 : 0.65)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                  }

                  HoverHandler { id: micHover }
                  TapHandler { onTapped: root.inputPicked(String(mic.modelData.name)) }
                }
              }
            }

            Text {
              width: parent.width
              // Said plainly, because it is the single most useful thing anyone
              // can do about recognition on this machine.
              text: "Following the system picks a headset when one is worn, and routes "
                  + "through the echo canceller when the room is in play. A microphone "
                  + "close to the mouth is worth more than any setting here."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.35
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSectionHeader { width: parent.width; text: "Local agent" }

            Row {
              width: parent.width
              spacing: Style.spaceReal(8)

              Repeater {
                model: ["codex", "claude"]

                AgentBadge {
                  id: badge
                  required property string modelData
                  agent: badge.modelData
                  opacity: badge.modelData === root.backend ? 1 : 0.35
                  Behavior on opacity { NumberAnimation { duration: 160 } }
                  TapHandler { onTapped: root.backendPicked(badge.modelData) }
                }
              }
            }

            Text {
              width: parent.width
              // "Read-only" used to stand here for both. It is true about
              // writing and was being read as a claim about reading, which is a
              // different and much larger promise — codex's sandbox does not
              // make it, as an afternoon with the actual binary established.
              text: "codex runs on the ChatGPT subscription; claude brings its skills "
                  + "and MCP connectors. Neither can change your files."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: 0.4
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            PanelSeparator { width: parent.width }

            // --- access -------------------------------------------------------
            PanelSectionHeader { width: parent.width; text: "Access" }

            Row {
              width: parent.width
              spacing: Style.spaceReal(8)

              Column {
                width: parent.width - accessButton.width - parent.spacing
                spacing: Style.spaceReal(3)

                Text {
                  width: parent.width
                  text: root.workspace === "" ? "No folder chosen" : root.workspace
                  textFormat: Text.PlainText
                  elide: Text.ElideMiddle
                  color: root.workspace === "" ? Color.urgent : Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }

                Text {
                  width: parent.width
                  text: {
                    if (root.consented.length === 0) return "No agent is allowed to answer yet"
                    return "Allowed: " + root.consented.join(", ")
                      + (root.unrestricted.length === 0
                          ? " · held to the folder"
                          : " · unrestricted: " + root.unrestricted.join(", "))
                  }
                  textFormat: Text.PlainText
                  wrapMode: Text.Wrap
                  color: Color.menu.text
                  opacity: 0.4
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }

              Button {
                id: accessButton
                anchors.verticalCenter: parent.verticalCenter
                text: "Change"
                bordered: true
                foreground: Color.menu.text
                accent: Color.accent
                fontFamily: Style.font.family
                onClicked: root.accessRequested()
              }
            }

            Text {
              width: parent.width
              text: "Esc — close"
              textFormat: Text.PlainText
              color: Color.menu.text
              opacity: 0.3
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }
}
