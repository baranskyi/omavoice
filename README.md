# omavoice

A voice assistant for [Omarchy](https://omarchy.org). Press `SUPER+CTRL+M`, a
panel with a pixel waveform comes up in the middle of the screen, you talk, and
it talks back. The thing Siri kept promising to be.

What makes it different from the voice mode in the ChatGPT app is that **the
agent is local**. It searches the web, but it also searches *this machine* —
files, projects, configs, running processes. You can ask "how many plugins do I
have in omarchy" out loud, and the answer is a real one instead of an invented
one.

![The panel mid-conversation](preview.png)

## How it works

Two parts, and the split between them is the whole design.

**The voice** is the OpenAI Realtime API (`gpt-realtime-2.1-mini`). It hears, it
speaks, it lets you interrupt it. That is all it does: its system prompt
explicitly forbids it from answering questions of fact.

**The brain** is `codex exec` running on a ChatGPT subscription (or `claude -p`,
switchable at runtime). Every question with substance goes to it through an
`ask_agent` function call. It reads the filesystem and the web and returns
strict JSON: what to say out loud, what to put on screen, which buttons to
offer.

The split buys two things at once. The assistant becomes genuinely local, and
the audio tokens — by far the expensive ones — are spent on speech instead of
reasoning. A conversation costs about 1.6 cents a minute instead of twenty.

```
SUPER+CTRL+M ─► omarchy-shell shell toggle <plugin-id>
                          │
   ┌─ QML plugin (Quickshell) ──────────────────────┐
   │  Overlay.qml   centred panel, waveform, MD     │
   │  BarWidget.qml state icon in the bar           │
   │  Client.qml    Unix socket, NDJSON             │
   └────────────────────────────────────────────────┘
                          │  $XDG_RUNTIME_DIR/omavoice.sock
   ┌─ omavoiced (Python, systemd --user) ──────┐
   │  pw-record ──► Realtime API ──► pw-play        │
   │                    │                            │
   │                    └─ ask_agent ─► codex/claude │
   └─────────────────────────────────────────────────┘
```

There is no networking and no audio in the QML, and that is not a stylistic
choice: the system has no QtWebSockets, and the plugin shares a process with
the bar — anything slow or networked in there would hang the whole desktop.

## Requirements

Everything here is external to the plugin and none of it is installed for you.

| | Why | Note |
|---|---|---|
| **Omarchy 4.0+** with `omarchy-shell` | the plugin is Quickshell QML | already there if you run Omarchy |
| **A paid OpenAI API key** | the Realtime API bills per audio token | **a ChatGPT subscription does not work for this** — get one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| **`codex` or `claude` on your `PATH`** | this is the brain | `codex` runs on a ChatGPT subscription, `claude` on your Claude limits |
| **PipeWire** with `pw-record` / `pw-play` | audio in and out | standard on Omarchy |
| **Python 3.11+** | the daemon | `uv` is used if present, otherwise `python -m venv` |

The only thing downloaded during setup is `websockets` from PyPI, into a
virtualenv under `~/.local/share/omavoice/`. Nothing is installed system
wide and nothing asks for `sudo`.

## Install

```bash
omarchy plugin add https://github.com/baranskyi/omavoice --enable
bash ~/.config/omarchy/plugins/io.github.baranskyi.omavoice/scripts/setup.sh
```

`omarchy plugin add` only puts the QML in place. `setup.sh` does the rest: it
creates the virtualenv, installs the systemd user unit, drops in the echo
cancellation config, and creates an empty key file at
`~/.config/omavoice/env` with mode `600`. It never overwrites a config you
already have — if one is there and differs, it says so and leaves it alone.

Then three steps it deliberately does not do for you:

1. **The key.** Paste it in the interface — ⚙ at the bottom of the panel →
   `OpenAI API key` → `Save` — or write it into `~/.config/omavoice/env`
   by hand.
2. **The daemon**
   ```bash
   systemctl --user enable --now omavoice
   ```
3. **The hotkey**, in `~/.config/hypr/bindings.lua`:
   ```lua
   o.bind("SUPER + CTRL + M", "Voice assistant", "omarchy-shell shell toggle io.github.baranskyi.omavoice")
   ```

### Echo cancellation — the important part

Without it the assistant hears its own voice through the speakers, takes that
for your speech, and answers itself: it says goodbye to its own goodbye, then to
that goodbye, and so on until you stop it. The config ships with the plugin and
`setup.sh` installs it at

```
~/.config/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf
```

Reload PipeWire once afterwards: `systemctl --user restart pipewire`.

**Both ends have to go through the canceller.** It subtracts a reference signal
from the microphone — precisely what went through *its own* sink. Listening on
`echo-cancel-source` while playing to the system default output leaves it with
silence as its reference: it subtracts nothing, and the loop comes back. So
`OMAVOICE_INPUT=echo-cancel-source` and
`OMAVOICE_OUTPUT=omavoice_playback`, both at once. Measured
suppression on this machine is around −41 dB (RMS 0.0438 → 0.0004).

Neither node becomes a system default — only omavoice goes through them,
and the rest of the system never notices.

Two more layers sit on top of the canceller, because one is not enough:

- **A noise gate**, which measures its own threshold. A microphone hears the
  fan, the keyboard and the room; transcription turns that into confident
  nonsense — `はい。`, `Gjiliv.` — and the assistant duly answers it. Everything
  below the threshold is *replaced with silence* rather than dropped, because
  the server side VAD needs a continuous stream.

  The threshold is not a constant, because a constant is only ever right for
  the microphone it was picked on. A laptop mic at talking distance and a
  display mic across the desk are an order of magnitude apart, and the
  canceller's noise suppression moves the floor again; tuned for one, the same
  number either answers the fan or swallows half a sentence. So the gate tracks
  the room's noise floor and opens at eight times it — falling fast, so a
  different microphone is adopted in seconds, and rising only while the gate is
  shut, so a long answer of your own cannot walk the threshold up behind you.
  `OMAVOICE_GATE` takes a number if you would rather pin it.

  It holds open for longer than the server waits before calling a turn
  finished (`OMAVOICE_SILENCE_MS`, 1100 ms). That ordering matters: a shorter
  hold means the gate substitutes digital silence for the pause between two
  words, handing the server a cleaner silence than the room ever produced and
  talking it into ending a sentence you are still in the middle of.
- **Counting real playback time.** The model sends its answer far faster than it
  is spoken, so "the last chunk arrived" and "the room went quiet" are different
  moments, sometimes seconds apart. The microphone's threshold stays raised
  until `pw-play` has drained its buffer, plus 0.9 s for room reverberation.
  Without that, echo slipped through exactly in the gap: two seconds after "How
  can I help?" the assistant would hear "Why can you help me?" and answer it.

None of this stops you interrupting: live speech clears the threshold with room
to spare and residual echo does not. If something still gets through, pin
`OMAVOICE_GATE` to a number above what leaks; headphones remove the question
entirely.

When a conversation misbehaves, run the daemon with `OMAVOICE_DEBUG=1` and read
one line per second:

```
mic: peak=0.3241 gate=0.0032 floor=0.0002 passed=50/50 sent=248
```

`peak` against `gate` settles "why did it not hear me" in one glance, and `sent`
settles the harder question of whether the audio ever reached the server at
all.

## Using it

| Action | How |
|---|---|
| Open the panel | `SUPER+CTRL+M`, or click the crystal in the bar |
| Send it to the background | `Esc`, a click outside the panel, or the same hotkey |
| Stop, keeping the conversation | `Q` in the panel, or `omavoice-ctl stop` |
| Interrupt an answer | `I` in the panel, or `omavoice-ctl cancel` |
| Start a new conversation | `N` in the panel, or `omavoice-ctl reset` |
| Settings | ⚙ in the panel, or `omarchy-shell io.github.baranskyi.omavoice settings` |
| Change the voice | ⚙ → the voice chip, or `omavoice-ctl voice cedar` |
| List the voices | `omavoice-ctl voice` |
| Switch agent | `Tab` in the panel, click the badge, or `omavoice-ctl backend claude` |
| Ask in writing | `omavoice-ctl ask "..."` |
| Make it say a phrase | `omavoice-ctl say "..."` — for testing echo with nobody in the room |
| Inspect the state | `omavoice-ctl status` · `make logs` |

The bar icon is an indicator: a microphone in the theme accent means the session
is live, a crossed out one means the daemon is not running.

### Background and ending

Closing the panel and ending the conversation are different things, and they
live on different keys.

- **`Esc` (and a click outside) sends it to the background.** Nothing stops: the
  session stays alive, the microphone stays open, the agent finishes what it
  started, and the answer is still spoken out loud. This is for when the question turns out to be a long one and there is no
  reason to sit in front of the panel while it is computed. The crystal in the
  bar keeps showing state — it pulses while the agent works.
- **`Q` stops it.** The microphone is released and whatever the assistant was
  saying is cut off — but the connection stays up and so does the conversation.
  Open the panel again and it picks up where it left off, remembering what was
  said. Stopping is not forgetting; forgetting is `N`.

Nothing else ends a session. The Realtime API keeps the conversation on its
connection and offers no way to clear it, so closing the socket is the only way
to forget — which makes it the one thing that must never happen on its own. A
connection that goes quiet is reported, never rebuilt: an earlier version
rebuilt it automatically and spent its time cutting short conversations that
were going fine.
- **`I` interrupts** an answer that is running long, without leaving the
  conversation.

All three are letters rather than chords: the panel takes the keyboard
exclusively, so any system-wide combination assigned elsewhere would simply
vanish here. `Ctrl+Space`, for one, is Omarchy's dictation key.

**Backgrounding changes nothing at all.** The microphone stays open, the
conversation carries on, answers are still spoken. Hiding a window is not the
same as ending a conversation, and it should not have to be explained which one
you meant.

An earlier version released the microphone here, reasoning that a session
listening with no window on screen is a privacy question and a billing one.
Both are real, and both belong to the person rather than to this program —
having to reopen a panel in order to be heard made the panel the point instead
of the talking. `Q` stops everything when stopping is what is wanted, and
without forgetting the conversation.

When you return you can see what happened while you were away: the daemon keeps
the last fourteen waterfall lines and replays them to a newly opened panel along
with the answer. An empty log after coming back would hide exactly the work the
panel was minimised for.

### A new conversation

`N` in the panel wipes the context and starts clean. Three separate memories
have to be forgotten, or "forget everything" only half works:

- the conversation in the Realtime API, which lives on the connection;
- the agent's thread, which `codex`/`claude` resume by id;
- the transcript and the waterfall in the panel itself.

The Realtime API cannot clear its history: items are deleted one at a time by
id. So the honest way is to reconnect, which is what happens. It costs a second
or two and leaves no doubt about what it still remembers. The voice, the agent
and the key survive it.

### The waterfall

The panel does not hide its work behind a spinner. Every event is a line: what
was heard, what was asked of the agent, how long it thought, what it answered.
New on top, old sinking and fading.

```
22:48:31  ←  There are 8 plugins in the directory   codex · 9.9s
22:48:21  →  How many plugins in omarchy?           codex
22:48:19  ‹  how many plugins do i have
22:48:12  ◆  gpt-realtime-2.1-mini
```

While the agent works, the top line counts out loud — `codex · 6.9s` and on.
Waiting ten seconds is fine when you can see what is being computed; the same
ten seconds facing a pulsing dot feel like a failure.

The arrows are literal: `‹` inbound from the microphone, `→` out to the agent,
`←` back from it, `›` out to the speakers.

### Voice and grammatical gender

Russian agrees its past tense with the speaker, so the voice is not only a
timbre. A female voice saying *посмотрел* sounds like a glitch, and it is the
first thing anyone notices. Every voice in the catalogue is marked ♀ or ♂, and
the choice injects the matching rule into the prompt — **at the beginning** of
the instructions, because in the tail of a long prompt the model drifts back to
masculine.

Changing the voice recreates the session: the Realtime API fixes the voice for
the lifetime of a connection and refuses to change it after the first audio. The
choice is remembered in `~/.local/state/omavoice/preferences.json`.

The agent badge wears vendor colours: terracotta `#D97757` and the Anthropic
star for `claude`, a lavender-blue gradient and the Codex `>_` for `codex`.

## Removal

```bash
bash ~/.config/omarchy/plugins/io.github.baranskyi.omavoice/scripts/uninstall.sh
omarchy plugin remove io.github.baranskyi.omavoice
```

The script stops and removes the systemd unit, deletes the virtualenv under
`~/.local/share/omavoice/`, and removes the `omavoice-ctl` symlink
from `~/.local/bin` if it put one there.

Two things are left behind on purpose, and it tells you so:

- `~/.config/omavoice/env` — your API key.
- `~/.config/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf` — echo
  cancellation, which other things on the machine may be relying on by now.

Delete either by hand once you are sure. Also drop the `bindings.lua` line if
you added one.

## Development

```bash
make sync       # copy the checkout into the plugins directory
make watch      # re-copy on every save; the shell reloads by itself
make validate   # Omarchy manifest check, symlink check, qmllint
make logs       # journalctl for the daemon
```

The parts are testable separately, bottom up:

```bash
# the audio path, no network and no key
~/.local/share/omavoice/venv/bin/python -m omavoice.audio --loopback

# the brain, no microphone
~/.local/share/omavoice/venv/bin/python -m omavoice.brain "how much disk space?"

# the voice, no panel
~/.local/share/omavoice/venv/bin/python -m omavoice --headless
```

Four traps that cost time:

- **`keepalive ping timeout` in the middle of a long answer.** The socket read
  loop must never be made to wait on the speakers. The model delivers an answer
  much faster than it is spoken, and writing into `pw-play` straight from
  `async for raw in ws` means that on a full buffer `drain()` stops the reading:
  incoming frames pile up, the keepalive ping goes unanswered, and the library
  tears the connection down from the inside — code 1011, by itself, mid
  sentence. The longer the answer, the surer it is. The cure is decoupling:
  `_on_audio` only enqueues, and a separate task drains the queue into the
  speakers. Verified against 49 seconds of continuous speech.
- **The QML engine caches compiled types**, and editing a file that is not an
  entry point (`Waveform.qml`) is picked up neither by saving nor by
  `rescanPlugins`. It needs `omarchy-restart-shell`. If a QML error points at a
  line that is no longer in the file, that is the type cache, not your code.
- **A virtualenv cannot live inside the plugin folder.** Omarchy's validator
  rejects a plugin containing symlinks, and every virtualenv has a few
  (`bin/python`, `lib64`). That is why the environment lives under
  `~/.local/share/omavoice/` and the daemon is reached through
  `PYTHONPATH`.
- **When the assistant behaves strangely, the first question is what it thinks
  it heard.** `journalctl --user -u omavoice | grep -E "heard:|said:"`
  answers it immediately: if `heard:` contains its own last sentence, echo is
  leaking; if it is incoherent junk, the gate is set too low.

## Layout

```
manifest.json       kinds: overlay + bar-widget, keepLoaded
Overlay.qml         the panel, layer-shell above everything
SettingsWindow.qml  settings in their own window: key, voice, agent
Waveform.qml        a figure of points on a Canvas
EventLog.qml        the event waterfall
PrimeRadiant.qml    the logo and the bar icon
AgentBadge.qml      the vendor badge
BarWidget.qml       the state icon
Client.qml          socket and state

daemon/omavoice/
  __main__.py   the state machine, where everything is joined
  realtime.py   WebSocket to the Realtime API, the "voice, not brain" prompt
  audio.py      pw-record / pw-play, RMS, interruption
  brain.py      codex exec / claude -p, parsing the answer
  ipc.py        Unix socket, NDJSON, broadcast
  ctl.py        omavoice-ctl
  schemas/answer.json   the shape of the agent's answer

systemd/    the user unit, as a template setup.sh fills in
pipewire/   the echo cancellation config
scripts/    setup.sh, uninstall.sh
bin/        omavoice-ctl
```

## Security and privacy

This plugin runs unsandboxed, like every Omarchy plugin, and it does more than
draw a widget. Plainly, what it does:

- **It installs a systemd user unit** (`omavoice.service`) that runs a
  Python daemon. `setup.sh` writes it; nothing is enabled without you running
  `systemctl --user enable`.
- **It records audio** while a session is open, and streams it to the OpenAI
  Realtime API. The microphone is released the moment the panel goes to the
  background or the session ends.
- **It runs `codex` or `claude` as a subprocess**, in a **read-only** sandbox:
  the agent reads and reports, it changes nothing. Speech is too easy to get
  wrong, and transcription adds its own error on top.
- **Requests that consist of a destructive command** (`rm -rf`, `mkfs`,
  `shutdown`) never reach the agent.
- **The key** sits in `~/.config/omavoice/env` with mode `600` and is read
  only by the unit — it is not visible to the rest of your processes.
- **Links and paths from an answer** are opened through an argv vector with a
  scheme check: their source is a retelling of untrusted content.
- **Images are stripped out of markdown** so the shell never fetches them.
- **No `sudo`, no `curl | sh`, no package installation outside the virtualenv.**

## Cost

You pay for the voice only. The brain runs on a subscription.

| Model | Per minute of conversation |
|---|---|
| `gpt-realtime-2.1-mini` | ~$0.016 |
| `gpt-realtime-2.1` | ~$0.05 |

Answer latency is 9–30 seconds depending on the question: that is what starting
codex and letting it walk the filesystem costs. Realtime covers the pause with a
short filler ("one second, looking"), but on simple questions it is noticeable.
Reasoning effort is pinned to `low`; `none` is slower on this model — the agent
compensates with extra tool calls — and `minimal` is refused.

If the bill is noticeably higher than this, it means Realtime is answering by
itself instead of calling `ask_agent`, and the thing to fix is the prompt in
`realtime.py`.

## License

MIT — see [LICENSE](LICENSE).
