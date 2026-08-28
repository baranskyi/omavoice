pragma ComponentBehavior: Bound

// The floating voice panel.
//
// A layer-shell surface on the overlay layer, dimming the desktop and holding a
// card in the middle of the screen. The shape follows omarchy.emojis: the host
// injects `shell` and `manifest`, calls open(payloadJson) on summon and close()
// on hide, and reads `opened` back to keep its own bookkeeping straight.
//
// The panel owns the microphone's lifetime. Opening it starts a session,
// closing it ends one — a voice assistant that listens while you are not
// looking at it is not a thing worth building.

import QtQuick
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  // Injected by the shell host on load.
  property var shell: null
  property var manifest: null

  // The host reads this back; without it `toggle` desyncs after the first open.
  property bool opened: false

  property bool settingsOpen: false
  property bool helpOpen: false
  property bool consentOpen: false
  // Whether the waterfall is folded away. The answer is not: that is the thing
  // that was asked for, and hiding it along with the log was the eye doing more
  // than it says. What goes is the running commentary — heard, asked, took 8s —
  // which is worth having while you wait and clutter once you have the reply.
  //
  // Not persisted: it is a gesture for the conversation you are in, and a panel
  // that opened silently withholding its own history would be a worse default
  // than the one it replaced.
  property bool logHidden: false
  property string keyError: ""

  // The hint line's travelling light borrows the figure's colour rather than
  // choosing its own. Two marks saying "listening" in two different greens
  // would be two languages for one fact, and the whole point of the palette
  // living in StateHues is that neither of them gets to invent it.
  StateHues { id: hues }
  readonly property color hintGlow:
    hues.colorFor(client.voiceState, Color.menu.background, Color.accent)

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.baranskyi.omavoice"

  // The interface is English because the desktop is, and because the plugin is
  // meant to be installable by anyone. The conversation language is a separate
  // thing entirely: it follows whoever is speaking.
  readonly property string statusText: {
    if (client.errorText) return client.errorText
    if (!client.connected) return "Daemon not running"
    if (!client.hasKey) return "No API key — open settings"
    switch (client.voiceState) {
    case "listening": return client.userText ? "Listening…" : "Speak"
    case "thinking": return "Looking it up"
    case "speaking": return "Answering"
    case "error": return "Error"
    default: return "Ready"
    }
  }

  function open(payloadJson) {
    root.opened = true
    root.settingsOpen = false
    client.wanted = true
    // Not cleared here: reopening after backgrounding should show the work
    // that was going on, and the daemon replays the last stretch of it. A
    // genuinely new conversation is N, or a session that starts from nothing.
    if (!client.connected || !client.backgrounded) client.clearConversation()
    // The socket connects asynchronously; startSession is retried from
    // onConnectedChanged if we beat it here.
    // Fires only when the socket is already up; otherwise onConnectedChanged
    // does it.
    client.startSession()
    client.foreground()
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  // Host-initiated close. It already knows, so do not tell it back.
  // The session survives: an agent that is mid-thought should finish, and the
  // answer should still be spoken. The microphone stops, so nothing is
  // listening to a room with no window on screen.
  function close() {
    root.opened = false
    client.background()
    client.wanted = false
  }

  // User-initiated close (Escape, click on the scrim). Tell the host so its
  // openPanelIds map stays in step and the next toggle opens rather than closes.
  function dismiss() {
    root.opened = false
    client.background()
    client.wanted = false
    if (shell && typeof shell.hide === "function") shell.hide(root.pluginId)
  }

  // The deliberate ending: hang up, drop the session, release everything.
  function endSession() {
    root.opened = false
    client.stopSession()
    client.wanted = false
    if (shell && typeof shell.hide === "function") shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  // Only ever hand xdg-open an argv vector, and only for schemes we recognise:
  // both the URL and the path come from a model summarising untrusted content.
  function openUrl(url) {
    const value = String(url || "")
    if (!/^https?:\/\//.test(value)) return
    Util.execArgv(["xdg-open", value])
  }

  function openPath(path) {
    const value = String(path || "")
    if (!value.startsWith("/")) return
    Util.execArgv(["xdg-open", value])
  }

  // Qt renders MarkdownText's images by fetching them. Nothing in a voice
  // answer needs an image, and the URLs come from summarised web content, so
  // they are stripped rather than trusted.
  function safeMarkdown(text) {
    return String(text || "").replace(/!\[[^\]]*\]\([^)]*\)/g, "")
  }

  Client {
    id: client
    // Felt, not read: the figure buzzes the moment the assistant is talked
    // over, before the state has caught up.
    onBarged: wave.bargeIn()
    onTraced: function (text) { under.push(text) }
    // Both commands are re-sent on connect, not just at open(): the socket
    // connects asynchronously, so anything sent from open() lands before
    // there is a socket to send it on and is silently dropped. That is what
    // left the microphone off after returning from the background.
    onConnectedChanged: {
      if (connected && root.opened) {
        startSession()
        foreground()
      }
    }
  }

  // The bar widget reads state through the host, so expose it by name.
  readonly property string voiceState: client.voiceState
  readonly property bool daemonConnected: client.connected

  IpcHandler {
    target: "io.github.baranskyi.omavoice"
    function open(): void { root.open("{}") }
    function close(): void { root.dismiss() }
    function toggle(): void { root.toggle() }
    function settings(): void { root.settingsOpen = !root.settingsOpen }
    function help(): void { root.helpOpen = !root.helpOpen }
    function access(): void { root.consentOpen = !root.consentOpen }
    function reset(): void { client.reset() }
    function state(): string { return client.voiceState }
    function backend(): string { return client.backend }
  }

  ConsentWindow {
    id: consentWindow
    // Raised by itself the first time, and whenever what was agreed is no
    // longer enough — a folder that has been deleted, a permission withdrawn.
    // Not gated on `root.opened` the way the other two windows are: this one
    // is the reason nothing is happening, and hiding it with the panel would
    // leave the assistant silently refusing every question with the
    // explanation one layer out of reach.
    open: root.consentOpen || (client.accessNeeded && root.opened)
    workspace: client.workspace
    consented: client.consented
    unrestricted: client.unrestricted
    folders: client.folderChoices
    backend: client.backend
    onClosed: root.consentOpen = false
    onRefreshed: client.askAccess()
    onFolderPicked: function (path) { client.setWorkspace(path) }
    onConsentChanged: function (agent, granted) { client.setConsent(agent, granted) }
    onUnrestrictChanged: function (agent, granted) { client.setUnrestricted(agent, granted) }
  }

  HelpWindow {
    id: helpWindow
    open: root.helpOpen && root.opened
    backend: client.backend
    workspace: client.workspace
    onClosed: root.helpOpen = false
  }

  SettingsWindow {
    id: settingsWindow
    open: root.settingsOpen && root.opened
    hasKey: client.hasKey
    voices: client.voiceCatalogue
    currentVoice: client.voice
    backend: client.backend
    audioSources: client.audioSources
    audioInput: client.audioInput
    audioResolved: client.audioResolved
    keyError: root.keyError
    workspace: client.workspace
    consented: client.consented
    unrestricted: client.unrestricted
    onClosed: root.settingsOpen = false
    onAccessRequested: {
      root.settingsOpen = false
      root.consentOpen = true
    }
    onVoicePicked: function (name) { client.setVoice(name) }
    onBackendPicked: function (name) { client.setBackend(name) }
    onInputPicked: function (name) { client.setInput(name) }
    onKeySubmitted: function (key) {
      root.keyError = ""
      client.setApiKey(key)
    }
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"

    WlrLayershell.namespace: "omavoice"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    // Themes may leave menu.scrim fully transparent — the emoji picker sits on
    // a small card where that reads fine. This panel is the focus of a
    // conversation, so it dims regardless of what the theme asked for.
    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim.a > 0.05
        ? Color.menu.scrim
        : Qt.rgba(Color.background.r, Color.background.g, Color.background.b, 0.55)
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      // Top-centre: this is a heads-up display over whatever you are doing,
      // and the middle of the screen is where you are looking at that.
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.top: parent.top
      anchors.topMargin: Style.space(64)
      width: Style.space(560)
      // Grows with its content up to a cap, past which the middle scrolls.
      // The cap used to apply to the card alone while the content kept its
      // full height underneath, so anything taller simply painted over the
      // desktop below the card.
      height: Math.min(
        Style.space(620),
        card.contentTopInset + card.contentBottomInset
          + head.implicitHeight
          + (middle.implicitHeight > 0 ? Style.spacing.panelGap + middle.implicitHeight : 0)
          + (actions.visible ? Style.spacing.panelGap + actions.implicitHeight : 0)
          + Style.spacing.panelGap + footer.height
      )
      radius: Style.cornerRadius
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      // Swallow clicks so hitting the card does not dismiss the panel.
      MouseArea { anchors.fill: parent; onClicked: {} }

      Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

      Item {
        id: keyCatcher
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        // Belt and braces: nothing inside the card may ever paint outside it.
        clip: true
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function (event) {
          if (event.key === Qt.Key_Escape) {
            // Step away without ending anything: the agent keeps working, the
            // answer still gets spoken, and the bar icon keeps the state.
            // Interrupting a runaway answer is I; ending the session is Q.
            root.dismiss()
            event.accepted = true
          } else if (event.key === Qt.Key_I) {
            // A letter, not Ctrl+Space: that combination is voxtype's
            // dictation toggle on this desktop, and a panel that grabs the
            // keyboard exclusively would quietly swallow it.
            client.cancel()
            event.accepted = true
          } else if (event.key === Qt.Key_Q) {
            // Stops both directions and keeps the conversation. Escape steps
            // away and lets the answer finish; this cuts it off. Neither forgets —
            // that is N.
            root.endSession()
            event.accepted = true
          } else if (event.key === Qt.Key_H) {
            root.helpOpen = true
            event.accepted = true
          } else if (event.key === Qt.Key_N) {
            // New conversation: forgets the Realtime history, the agent's
            // thread and everything on screen. Bare N because the panel has
            // no text entry to compete with.
            client.reset()
            // Including the working behind the figure. Leaving the last
            // question's traces dissolving under a fresh conversation would be
            // the one place this effect could mislead rather than reassure.
            under.forget()
            event.accepted = true
          } else if (event.key === Qt.Key_Tab) {
            client.setBackend(client.backend === "codex" ? "claude" : "codex")
            event.accepted = true
          }
        }

        // Fixed head. The waveform and the status line are what the panel is;
        // they stay put while the answer scrolls underneath them.
        Column {
          id: head
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.panelGap

          // --- waveform -------------------------------------------------
          Item {
            width: parent.width
            height: wave.implicitHeight + Style.space(16)

            // Declared before the figure, so it is behind it. That ordering is
            // the entire design: the text is meant to lose.
            Undertext {
              id: under
              anchors.fill: parent
              anchors.leftMargin: Style.spaceReal(10)
              anchors.rightMargin: Style.spaceReal(10)
              anchors.bottomMargin: Style.spaceReal(6)
              working: client.voiceState === "thinking" || client.waiting
              tint: root.hintGlow
              // Lit by the figure drawn over it.
              light: wave.light
            }

            Waveform {
              id: wave
              anchors.centerIn: parent
              width: parent.width
              height: Style.spaceReal(120)
              voiceState: client.connected ? client.voiceState : "error"
              level: client.level
              bands: client.bands
              // Only the speaking colour comes from the theme now; the states
              // that have to be recognised at a glance carry their own hue.
              accent: Color.accent
              dim: Color.menu.text
            }
          }

          // --- status line ----------------------------------------------
          // A Row would let a long status (an API error, say) push past the
          // card's edge, so the text gets an explicit width and wraps.
          Item {
            width: parent.width
            height: Math.max(statusText.implicitHeight, backendTag.implicitHeight)

            Rectangle {
              id: statusDot
              anchors.left: parent.left
              anchors.top: parent.top
              anchors.topMargin: Math.round((statusText.implicitHeight - height) / 2)
              width: Style.space(8)
              height: Style.space(8)
              radius: width / 2
              color: client.voiceState === "listening" ? Color.accent
                   : client.voiceState === "error" || !client.connected ? Color.urgent
                   : Color.menu.text
              opacity: client.voiceState === "idle" ? 0.4 : 1

              SequentialAnimation on opacity {
                running: client.voiceState === "listening"
                loops: Animation.Infinite
                NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutQuad }
                NumberAnimation { to: 1.0; duration: 700; easing.type: Easing.InOutQuad }
              }
            }

            Text {
              id: statusText
              anchors.left: statusDot.right
              anchors.leftMargin: Style.spacing.sm
              anchors.right: backendTag.left
              anchors.rightMargin: Style.spacing.sm
              anchors.top: parent.top
              text: root.statusText
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              maximumLineCount: 3
              elide: Text.ElideRight
              color: client.voiceState === "error" || !client.connected ? Color.urgent : Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              opacity: 0.9
            }

            Row {
              id: backendTag
              anchors.right: parent.right
              anchors.top: parent.top
              spacing: Style.spaceReal(8)

              // Folds the transcript away. Placed here, against the badge,
              // because this row is the boundary: everything above it is what
              // the assistant is doing now, everything below is what it has
              // already said.
              Text {
                id: eye
                anchors.verticalCenter: parent.verticalCenter
                text: root.logHidden ? "\uf070" : "\uf06e"
                textFormat: Text.PlainText
                color: root.logHidden ? Color.accent : Color.menu.text
                opacity: eyeHover.hovered ? 1 : 0.85
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                Behavior on opacity { NumberAnimation { duration: 140 } }

                HoverHandler { id: eyeHover; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: root.logHidden = !root.logHidden }
              }

              AgentBadge {
                anchors.verticalCenter: parent.verticalCenter
                agent: client.backend
                TapHandler {
                  onTapped: client.setBackend(client.backend === "codex" ? "claude" : "codex")
                }
              }

            }
          }
        }

        // --- footer --------------------------------------------------------
        // Chrome, pinned to the card rather than riding at the end of the
        // content. It used to sit in one long column with everything else,
        // which is how it ended up painted below the card's edge and onto the
        // desktop as soon as an answer arrived with action buttons attached.
        //
        // The gear sits here, away from the agent badge: it configures the
        // whole plugin, not the backend it happened to be standing next to.
        Item {
          id: footer
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          height: Math.max(hint.implicitHeight, gear.implicitHeight)

          // The keys, with a light passing slowly behind them.
          //
          // A row of instructions at 35% opacity is the correct weight for
          // something you read once and then stop seeing — but "stop seeing"
          // is also how a person forgets that Q exists. A slow sweep gives the
          // line one moment of legibility every few seconds without ever
          // asking to be looked at, and it costs nothing at rest because the
          // animation only runs while the panel is open.
          //
          // The light is masked by the glyphs themselves rather than drawn as
          // a bar across them: the letters brighten as it passes, which is the
          // difference between a shimmer and a scanline. Same technique the
          // shell uses for its own reveals.
          Item {
            id: hintBox
            anchors.left: parent.left
            anchors.right: gear.left
            anchors.rightMargin: Style.spaceReal(8)
            anchors.verticalCenter: parent.verticalCenter
            height: hint.implicitHeight

            readonly property string line: client.connected
              ? "Esc — background · I — interrupt · N — new · Q — stop · H — help"
              : "Start the daemon:  systemctl --user start omavoice"

            Text {
              id: hint
              width: hintBox.width
              text: hintBox.line
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              opacity: 0.35
            }

            // The same glyphs again, in white and never shown: this is the
            // shape the light is cut to.
            Item {
              id: hintMask
              width: hintBox.width
              height: hintBox.height
              visible: false
              layer.enabled: true

              Text {
                width: hintBox.width
                text: hintBox.line
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: "white"
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            Item {
              width: hintBox.width
              height: hintBox.height
              visible: client.connected
              layer.enabled: true
              layer.smooth: true
              layer.effect: MultiEffect {
                maskEnabled: true
                maskSource: hintMask
                // These two numbers are the whole effect, and the intuitive
                // reading of them is backwards. A wide spread does not soften
                // the mask, it *defeats* it: at 0.02/0.60 the light came
                // through as a plain rectangle sitting on the words, because
                // almost every alpha value cleared the ramp. Tightened until
                // the glyphs themselves carry the light, checked against a
                // rendered frame rather than reasoned about.
                maskThresholdMin: 0.20
                maskSpreadAtMin: 0.25
              }

              Rectangle {
                id: sweep
                y: 0
                height: hintBox.height
                width: Math.max(Style.spaceReal(90), hintBox.width * 0.38)
                gradient: Gradient {
                  orientation: Gradient.Horizontal
                  GradientStop { position: 0.0; color: "transparent" }
                  GradientStop { position: 0.42; color: root.hintGlow }
                  GradientStop { position: 0.58; color: root.hintGlow }
                  GradientStop { position: 1.0; color: "transparent" }
                }

                // Slow, and with a rest between passes. A sweep that restarts
                // the instant it finishes reads as a loading bar.
                SequentialAnimation on x {
                  running: root.opened && client.connected
                  loops: Animation.Infinite
                  NumberAnimation {
                    from: -sweep.width
                    to: hintBox.width
                    duration: 4600
                    easing.type: Easing.InOutSine
                  }
                  PauseAnimation { duration: 1900 }
                }
              }
            }
          }

          Text {
            id: gear
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: "\u2699"
            textFormat: Text.PlainText
            color: root.settingsOpen ? Color.accent : Color.menu.text
            opacity: root.settingsOpen ? 1 : (gearHover.hovered ? 0.9 : 0.4)
            font.family: Style.font.family
            font.pixelSize: Style.font.subtitle

            Behavior on opacity { NumberAnimation { duration: 140 } }

            HoverHandler { id: gearHover }
            TapHandler { onTapped: root.settingsOpen = true }
          }
        }

        // --- buttons ------------------------------------------------------
        Flow {
          id: actions
          // Actions stay reachable. Inside the scroller they fell below the
          // fold the moment an answer was long enough to scroll — visible
          // only to someone who thought to scroll for them.
          anchors.bottom: footer.top
          anchors.bottomMargin: visible ? Style.spacing.panelGap : 0
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.sm
          visible: (client.links && client.links.length > 0) || (client.files && client.files.length > 0)

          Repeater {
            model: client.links
            Button {
              required property var modelData
              text: String(modelData.label || "Ссылка")
              iconText: "↗"
              tooltipText: String(modelData.url || "")
              foreground: Color.menu.text
              accent: Color.accent
              fontFamily: Style.font.family
              bordered: true
              onClicked: root.openUrl(modelData.url)
            }
          }

          Repeater {
            model: client.files
            Button {
              required property var modelData
              text: String(modelData.label || "Файл")
              iconText: "\udb80\ude14"  // file, as a surrogate pair
              tooltipText: String(modelData.path || "")
              foreground: Color.menu.text
              accent: Color.accent
              fontFamily: Style.font.family
              bordered: true
              onClicked: root.openPath(modelData.path)
            }
          }
        }

        // The one scrolling surface in the panel. Everything that can grow
        // without a bound — the waterfall, the answer, the buttons — lives in
        // here and is clipped to whatever room is left between head and
        // footer. One scroller rather than several: nested Flickables fight
        // each other for the wheel, and the answer used to have its own.
        Flickable {
          id: scroller
          anchors.top: head.bottom
          anchors.topMargin: Style.spacing.panelGap
          anchors.bottom: actions.top
          anchors.bottomMargin: Style.spacing.panelGap
          anchors.left: parent.left
          anchors.right: parent.right
          contentHeight: middle.implicitHeight
          clip: true
          interactive: contentHeight > height
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: middle
            width: scroller.width
            spacing: Style.spacing.panelGap

            // --- the waterfall ----------------------------------------------
            EventLog {
              width: parent.width
              visible: !root.logHidden && client.events.count > 0
              model: client.events
              pendingSince: client.pendingSince
              accent: Color.accent
              base: Color.menu.text
              urgent: Color.urgent
            }

            PanelSeparator {
              width: parent.width
              visible: client.markdown !== "" || client.assistantText !== ""
            }

            // --- the answer --------------------------------------------------
            Text {
              id: answer
              width: parent.width
              // The panel is a glance, not a document: markdown while the
              // agent's structured answer is in, the live spoken transcript
              // before that.
              text: client.markdown !== ""
                ? root.safeMarkdown(client.markdown)
                : client.assistantText
              textFormat: client.markdown !== "" ? Text.MarkdownText : Text.PlainText
              wrapMode: Text.Wrap
              color: Color.menu.text
              // The concrete family, not the "monospace" alias every other
              // label uses. Markdown is drawn through a QTextDocument, which
              // re-matches a font per style run; on the alias the bold runs
              // find no bold face and land on a different family altogether,
              // so a line like **Omarchy:** 4.0.1-1 came out in two fonts.
              font.family: Style.font.resolvedFamily
              font.pixelSize: Style.font.body
              onLinkActivated: function (link) { root.openUrl(link) }
            }

          }
        }

        // The clip cuts a line in half when there is more below it, and a
        // hard edge mid-glyph reads as a rendering fault rather than as an
        // invitation to scroll. A short fade in the card's own colour says
        // which of the two it is. Drawn after the Flickable so it sits over
        // the text, and only while there is something to scroll to.
        Rectangle {
          id: fadeTop
          readonly property color bg: Color.menu.background
          anchors.left: scroller.left
          anchors.right: scroller.right
          anchors.top: scroller.top
          height: Style.space(20)
          visible: scroller.contentHeight > scroller.height && !scroller.atYBeginning
          gradient: Gradient {
            GradientStop { position: 0.0; color: fadeTop.bg }
            GradientStop { position: 1.0; color: Qt.rgba(fadeTop.bg.r, fadeTop.bg.g, fadeTop.bg.b, 0) }
          }
        }

        Rectangle {
          id: fadeBottom
          readonly property color bg: Color.menu.background
          anchors.left: scroller.left
          anchors.right: scroller.right
          anchors.bottom: scroller.bottom
          height: Style.space(20)
          visible: scroller.contentHeight > scroller.height && !scroller.atYEnd
          gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.rgba(fadeBottom.bg.r, fadeBottom.bg.g, fadeBottom.bg.b, 0) }
            GradientStop { position: 1.0; color: fadeBottom.bg }
          }
        }

      }
    }
  }
}
