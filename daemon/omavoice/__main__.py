"""The daemon: wires the microphone, the Realtime session, the brain and the panel together.

State is a small machine, and the UI is a pure function of it:

    idle       nothing is happening; the panel is closed or waiting
    listening  the microphone is hot and the person may be speaking
    thinking   the local agent is working on a question
    speaking   an answer is coming out of the speakers
    error      something broke and the panel should say so

Audio only flows while a session is running, which starts when the panel opens
and stops when it closes. A voice assistant that keeps the microphone open when
you are not looking at it is not a thing worth building.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import time

import json

from . import config, devices as device_choice, ipc
from .audio import (
    AutoGain,
    Microphone,
    NoiseGate,
    Speaker,
    clipped_samples,
    rms_full_scale,
)
from .brain import Brain
from .config import Config
from .realtime import RealtimeSession

log = logging.getLogger("omavoice")

# The waveform redraws at 20 fps. Every 20 ms chunk would be four times that
# for no visible benefit, and each one is a socket write per connected client.
_LEVEL_INTERVAL = 0.05

# While the assistant is speaking, the bar for what counts as speech goes up.
# The echo canceller measures about -41 dB on this hardware, but it converges
# over a second or two and can slip when the volume changes — and the failure
# mode is the worst one there is: it hears itself, answers itself, and never
# stops. Talking over it still works, because a real interruption clears this
# comfortably while residual echo does not.
_SPEAKING_GATE_MULTIPLIER = 2.6

# The room keeps ringing after the speakers stop. Holding the raised bar for a
# moment past the last sample covers the reverberant tail, which is exactly
# what was getting through and coming back as "Почему ты можешь мне помочь?"
# a beat after the assistant said "Чем могу помочь?".
_ECHO_TAIL_SECONDS = 0.9


def _open_dump(path: str):
    """Open one of the audio taps, or None when it is not asked for."""
    return open(path, "ab", buffering=0) if path else None


def _log_task_failure(task: asyncio.Task) -> None:
    """Surface a background task that died.

    Audio is sent without awaiting — one chunk must never hold up the next.
    The cost is that an exception in there has nobody to raise to, and the
    failure looks like a microphone that stopped working for no reason.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task failed: %r", exc)


def _opens_conversation(heard: str) -> bool:
    """Has the person actually said something?

    The bar is deliberately low: any transcript with a character in it counts.
    What this guards is only "do not speak before you are spoken to" — the
    session opens on a keypress, so without it the assistant greets the room.

    It used to demand two words, on the theory that a lone interjection is
    usually noise. That was the wrong trade. When anything upstream went wrong
    and transcripts arrived truncated, the rule turned a half-heard question
    into total silence, which reads as a broken assistant rather than as a
    careful one. Judging whether a heard sentence deserves an answer is the
    model's job, and its prompt already covers it.
    """
    return any(c.isalnum() for c in heard)


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.brain = Brain(cfg)
        self.server = ipc.Server(cfg.socket_path, self._on_command)

        self.speaker = Speaker(cfg, on_level=self._on_output_level)
        self.mic = Microphone(cfg, self._on_input_chunk)
        # The gate must hold open longer than the server waits before calling a
        # turn finished. Otherwise a pause between words outlives the hangover,
        # the gate substitutes digital silence for the rest of it, and we hand
        # the server a cleaner silence than the room ever produced — talking it
        # into ending a sentence the person is still in the middle of.
        self._recovering = False
        self._speech_chunks = 0
        self._speech_since = 0.0
        self.autogain = AutoGain(chunk_ms=cfg.chunk_ms)
        self.devices: device_choice.Devices | None = None
        # Microphones that failed to produce audio while this daemon has been
        # up. Remembered so a broken device is discovered once, not at the
        # start of every conversation.
        self._bad_inputs: set[str] = set()
        self._mic_dump = _open_dump(cfg.mic_dump)
        self._dump = _open_dump(cfg.dump_path)
        self._voice_dump = _open_dump(cfg.voice_dump)
        self.gate = NoiseGate(
            cfg.gate_level,
            hangover_ms=cfg.silence_ms + 300,
            chunk_ms=cfg.chunk_ms,
        )
        self.session: RealtimeSession | None = None

        # Playback runs on its own task, fed by this queue. Writing to pw-play
        # directly from the socket-reading path meant that whenever the speaker
        # buffer filled, drain() stalled the read loop — incoming frames piled
        # up, the keepalive ping went unanswered, and the library killed the
        # connection with "keepalive ping timeout" mid-sentence. The model
        # streams an answer far faster than it plays, so a long answer made
        # this near-certain.
        self._play_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._play_task: asyncio.Task | None = None

        self._prefs_path = cfg.state_dir / "preferences.json"
        self._load_preferences()

        self.state = "idle"
        # True while the session is alive but the panel is not on screen.
        self.backgrounded = False
        # Wall-clock moment after which the room can be trusted again.
        self._quiet_after = 0.0
        self._session_task: asyncio.Task | None = None
        self._level_task: asyncio.Task | None = None
        self._pending_level = 0.0
        self._pending_bands = [0.0, 0.0, 0.0, 0.0]
        self._stopping = asyncio.Event()

    # -- preferences ----------------------------------------------------------

    def _save_preferences(self) -> None:
        """Remember voice and backend across restarts.

        Kept next to the daemon's own state rather than in shell.json: these
        are the daemon's settings, and writing to the shell's config from here
        would race with the bar rewriting it.
        """
        try:
            self._prefs_path.write_text(
                json.dumps(
                    {
                        "voice": self.cfg.voice,
                        "backend": self.brain.backend,
                        # "" means follow the system; see devices.resolve.
                        "input": self.cfg.input_target,
                    },
                    indent=2,
                )
            )
        except OSError as exc:
            log.warning("could not save preferences: %s", exc)

    def _load_preferences(self) -> None:
        try:
            data = json.loads(self._prefs_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        voice = str(data.get("voice") or "")
        if voice in config.VOICE_GENDER:
            self.cfg.voice = voice
        backend = str(data.get("backend") or "")
        if backend in ("codex", "claude"):
            self.brain.backend = backend
        # An environment variable is a deliberate override and outranks a
        # remembered choice from the settings window.
        if not os.environ.get("OMAVOICE_INPUT"):
            self.cfg.input_target = str(data.get("input") or "")

    # -- the event stream ----------------------------------------------------

    def _emit(self, kind: str, text: str = "", **extra) -> None:
        """One line for the panel's waterfall.

        This is a Linux desktop, not a black box: showing what was heard, what
        was asked of the agent and how long it took is more reassuring while
        you wait than a spinner is. One short line per event.
        """
        payload = {"type": "event", "kind": kind, "text": text, "at": time.time()}
        payload.update(extra)
        self.server.broadcast(payload)

    # -- state ---------------------------------------------------------------

    def _set_state(self, state: str, *, message: str = "") -> None:
        if state == self.state:
            return
        self.state = state
        log.info("state -> %s%s", state, f" ({message})" if message else "")
        payload = {"type": "state", "state": state}
        if message:
            payload["message"] = message
        self.server.broadcast(payload)

    # -- audio ---------------------------------------------------------------

    def _on_input_chunk(self, chunk: bytes, level: float, bands: list[float]) -> None:
        # Called from the mic pump; keep it cheap and never await here.
        if self._mic_dump is not None:
            # Before anything of ours has touched it. Paired with the tap after
            # the gate, this turns "somewhere between the microphone and the
            # API" into a question with a byte offset for an answer.
            self._mic_dump.write(chunk)
        session = self.session
        if self.backgrounded:
            return
        if not (session and session.connected and self.state in ("listening", "speaking")):
            return

        # Not "are we in the speaking state" but "is sound still in the room".
        # The model streams an answer faster than it plays, so the state can
        # flip back to listening while the speakers are still working through
        # the buffer — and that gap is where the assistant used to hear itself.
        # A threshold is passed only while the speakers are working: raised, so
        # residual echo cannot pose as speech, and explicit so the gate knows
        # not to fold this stretch into its estimate of the room. The rest of
        # the time it uses — and updates — its own measured level.
        threshold = None
        if self._room_is_loud():
            threshold = self.gate.opening_level * _SPEAKING_GATE_MULTIPLIER

        # Two questions, but only one of them decides when the gate opens. The
        # level says this stands out from the room; the second says what stands
        # out is loud enough to be a voice once the quiet-microphone gain is
        # applied, which is what keeps an amplified fan from being answered.
        # Neither may close a gate that is already carrying a sentence.
        loudness = rms_full_scale(chunk)
        # What the gain learns from and what the gate opens on are deliberately
        # different questions. Learning from "stands out from the room" rather
        # than from "already passed" breaks a deadlock: on a very quiet
        # microphone the gate cannot open until the gain is up, and the gain
        # cannot rise until something passes. Measured, that deadlock silenced
        # a voice twelve times quieter than this one completely.
        stands_out = level >= (
            threshold if threshold is not None else self.gate.opening_level
        )
        passed = self.gate.step(
            level, threshold, can_open=self.autogain.is_speech_after_gain(loudness)
        )
        if self.cfg.debug:
            # The one number that settles "why did it not hear me": the level
            # the microphone actually delivered, against the threshold it had
            # to clear. Once a second, so a conversation stays readable.
            self._peak_level = max(getattr(self, "_peak_level", 0.0), level)
            self._clipped = getattr(self, "_clipped", 0) + clipped_samples(chunk)
            self._passed_chunks = getattr(self, "_passed_chunks", 0) + (1 if passed else 0)
            self._level_chunks = getattr(self, "_level_chunks", 0) + 1
            if self._level_chunks * self.cfg.chunk_ms >= 1000:
                log.debug(
                    "mic: peak=%.4f clip=%d gain=%.1fx gate=%.4f floor=%.4f passed=%d/%d sent=%d",
                    self._peak_level,
                    self._clipped,
                    self.autogain.gain,
                    threshold if threshold is not None else self.gate.opening_level,
                    self.gate.noise_floor,
                    self._passed_chunks,
                    self._level_chunks,
                    session.sent_events,
                )
                self._peak_level = 0.0
                self._clipped = 0
                self._passed_chunks = 0
                self._level_chunks = 0

        # Measured before the gain, so the gate keeps judging the microphone
        # against its own noise floor rather than against how loud we made it.
        # Never trained on the assistant's own voice. Echo passes the gate on
        # speakers, and gain learned from it is gain applied to it — the loop
        # feeding itself.
        self.autogain.observe(loudness, stands_out and threshold is None)

        if passed:
            # Gain is for a quiet person, never for the room while the speakers
            # are working. Whatever leaks past the canceller in that window is
            # the assistant's own voice, and multiplying it by ten is how a
            # residue that transcribed as nothing becomes a sentence it answers.
            if threshold is None:
                chunk = self.autogain.apply(chunk)
            self._pending_level = max(self._pending_level, level)
            self._pending_bands = bands
        else:
            # Silence of the same length: the stream stays continuous for the
            # server VAD, but there is nothing in it to mistake for a turn.
            chunk = bytes(len(chunk))
            self._pending_level = max(self._pending_level, 0.0)

        if self._dump is not None:
            self._dump.write(chunk)

        task = asyncio.create_task(session.send_audio(chunk))
        task.add_done_callback(_log_task_failure)

        # Counted here, judged in the watchdog: how much of what we are sending
        # actually looks like a voice. Silence and room noise must never make
        # the case that a session has stopped listening.
        if passed and self.autogain.is_speech_after_gain(loudness):
            self._speech_chunks += 1
        self._check_deaf_server(session)

    def _room_is_loud(self) -> bool:
        if self.speaker.playing:
            self._quiet_after = (
                asyncio.get_running_loop().time() + self.speaker.remaining + _ECHO_TAIL_SECONDS
            )
            return True
        return asyncio.get_running_loop().time() < self._quiet_after

    def _on_output_level(self, level: float, bands: list[float]) -> None:
        self._pending_level = max(self._pending_level, level)
        # While the assistant speaks, its own voice drives the figure — the
        # panel should look like it is talking, not like it is waiting.
        self._pending_bands = bands

    async def _level_pump(self) -> None:
        """Coalesce levels into a steady, low-rate stream for the waveform."""
        try:
            while True:
                await asyncio.sleep(_LEVEL_INTERVAL)

                # The one place that knows the answer has stopped being heard.
                if self.state == "speaking" and self.session and not self.speaker.playing:
                    self._set_state("listening")

                self.server.broadcast(
                    {
                        "type": "level",
                        "rms": round(self._pending_level, 3),
                        "bands": [round(b, 3) for b in self._pending_bands],
                    }
                )
                self._pending_level = 0.0
        except asyncio.CancelledError:
            pass

    # -- realtime callbacks --------------------------------------------------

    async def _on_audio(self, pcm: bytes) -> None:
        # Never awaits on the speaker: this runs inside the socket read loop.
        self._set_state("speaking")
        if self._voice_dump is not None:
            self._voice_dump.write(pcm)
        self._play_queue.put_nowait(pcm)

    async def _play_pump(self) -> None:
        """Drain the playback queue into pw-play, at the speed of sound."""
        try:
            while True:
                pcm = await self._play_queue.get()
                if pcm:
                    await self.speaker.write(pcm)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("playback pump died")

    def _drop_queued_audio(self) -> None:
        """Throw away audio that has not reached the speaker yet."""
        while not self._play_queue.empty():
            try:
                self._play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _on_tool_call(self, name: str, query: str) -> str:
        if name != "ask_agent":
            return f"Неизвестный инструмент: {name}"

        self._set_state("thinking")
        answer = await self.ask_brain(query)
        # Back to listening: the model is about to speak, and _on_audio will
        # flip us to "speaking" the moment the first sample lands. Unless the
        # panel closed while the agent was working — then the session is gone
        # and claiming to listen would leave a live-looking mic icon in the bar.
        if self.session is not None:
            self._set_state("listening")
        return answer.spoken

    async def ask_brain(self, query: str):
        """The one path to the agent, so the waterfall sees every question.

        Both the voice tool call and the text-mode `ask` command come through
        here; otherwise a typed question would leave the panel blank while the
        agent worked.
        """
        log.info("brain(%s): %s", self.brain.backend, query)
        self._emit("agent", query, backend=self.brain.backend)
        started = time.monotonic()

        answer = await self.brain.ask(query)

        self._emit(
            "result",
            answer.spoken,
            backend=self.brain.backend,
            seconds=round(time.monotonic() - started, 1),
            links=len(answer.links),
            files=len(answer.files),
        )
        self.server.broadcast(answer.as_ui_payload())
        return answer

    # How long a session may say nothing at all before we stop believing in it.
    #
    # Six seconds looked generous when the only audio reaching this check was
    # speech. It is not: room noise clears the gate too, so "audio flowing"
    # stopped meaning "someone is talking", and the watchdog began rebuilding
    # healthy sessions in the middle of conversations — which is what the panel
    # was reporting as "session stopped responding".
    #
    # The wedge this exists for is unmistakable by comparison: in the case it
    # was written for, the server emitted nothing for twenty-one seconds while
    # the socket kept accepting audio. Twenty catches that and leaves ordinary
    # quiet alone.
    _DEAF_AFTER_SECONDS = 20.0
    # ...and how much of that window has to have been a voice. Without this the
    # watchdog fires on a quiet room, because room noise clears the gate and
    # "audio flowing" stopped meaning "someone is talking". It cut two good
    # conversations short before this was added.
    _DEAF_NEEDS_SPEECH_SECONDS = 10.0

    def _check_deaf_server(self, session: RealtimeSession) -> None:
        """Notice a session that has stopped hearing, and rebuild it.

        Twice now a session has gone quiet mid-conversation: the socket stays
        open and accepts everything we send — the sent counter keeps climbing —
        but the server stops emitting events entirely, so speech is never
        detected and nothing is ever answered. From the outside it looks
        exactly like a microphone that died, which is the worst thing it could
        look like, because nothing in the panel or the log says otherwise.

        We cannot fix the far end, but we can stop pretending. If the gate is
        passing audio — someone is talking — and the server has said nothing at
        all for six seconds, the connection is rebuilt. That costs a second and
        keeps the microphone, the speaker and the echo canceller untouched.
        """
        if self.state != "listening" or self._recovering:
            return
        now = asyncio.get_running_loop().time()
        # Any sign of life resets the case being built against the session.
        if session.last_activity > self._speech_since:
            self._speech_since = session.last_activity
            self._speech_chunks = 0
        # Read off the session rather than kept here: the daemon only sees the
        # events it is interested in, and during a spoken answer that is none
        # of them.
        quiet_for = now - session.last_activity
        if quiet_for < self._DEAF_AFTER_SECONDS:
            return
        # And the second half of the case: someone has been talking through it.
        # Ten seconds of speech-level audio answered by nothing at all is a
        # wedge; twenty seconds of a quiet room is a person thinking.
        speaking_seconds = self._speech_chunks * self.cfg.chunk_ms / 1000
        if speaking_seconds < self._DEAF_NEEDS_SPEECH_SECONDS:
            return
        log.warning(
            "server has not answered for %.1fs while audio was flowing — reconnecting",
            quiet_for,
        )
        self._recovering = True
        task = asyncio.create_task(self._rebuild_session(), name="recover")
        task.add_done_callback(_log_task_failure)

    async def _rebuild_session(self) -> None:
        try:
            self._emit("reconnect", "session stopped responding")
            self._speech_chunks = 0
            await self.stop_session(keep_audio=True)
            await self.start_session()
        finally:
            self._recovering = False

    async def _on_event(self, event: dict) -> None:
        kind = event.get("type", "")
        # Any event at all means the far end is still processing what we send.


        if kind == "input_audio_buffer.speech_started":
            # Barge-in. Drop what is queued for the speakers and stop the model
            # mid-sentence; anything less and it talks over the person.
            session = self.session
            if self.state == "speaking":
                self._drop_queued_audio()
                await self.speaker.flush_now()
                self._quiet_after = 0.0
                if session:
                    await session.cancel_response()
                self._emit("barge", "interrupted")
            self._set_state("listening")

        elif kind == "response.output_audio_transcript.delta":
            delta = event.get("delta")
            if delta:
                self.server.broadcast(
                    {"type": "transcript", "role": "assistant", "text": delta, "final": False}
                )

        elif kind == "response.output_audio_transcript.done":
            spoken = str(event.get("transcript") or "").strip()
            log.info("said: %s", spoken[:160])
            if spoken:
                self._emit("said", spoken)
            self.server.broadcast(
                {
                    "type": "transcript",
                    "role": "assistant",
                    "text": str(event.get("transcript") or ""),
                    "final": True,
                }
            )

        elif kind == "conversation.item.input_audio_transcription.completed":
            heard = str(event.get("transcript") or "").strip()
            # Logged because when a voice assistant misbehaves, the first
            # question is always "what did it think it heard" — and if that is
            # its own last sentence, the echo path is leaking.
            log.info("heard: %s", heard or "(nothing)")
            if heard:
                self._emit("heard", heard)
            self.server.broadcast(
                {"type": "transcript", "role": "user", "text": heard, "final": True}
            )

            session = self.session
            if session and session.awaiting_first_turn:
                if _opens_conversation(heard):
                    await session.open_floor()
                else:
                    # Shown in the waterfall regardless, so silence here reads
                    # as a decision rather than as a dead microphone.
                    log.info("not an opening turn, staying quiet: %r", heard)

        elif kind == "response.done":
            # Deliberately not flipping to listening here: the answer has
            # finished arriving, not finished playing. The level pump moves us
            # back once the speakers are actually done.
            pass

        elif kind == "error":
            detail = event.get("error") or {}
            message = str(detail.get("message") or "Ошибка Realtime API")
            # Races we lose harmlessly: cancelling a response that just
            # finished, or committing an empty buffer. Neither is worth
            # showing a person, and neither ends the conversation.
            if "no active response" in message or "buffer is empty" in message.lower():
                log.debug("ignoring benign realtime error: %s", message)
                return
            log.error("realtime error: %s", message)
            self._emit("error", message)
            self.server.broadcast({"type": "error", "message": message})
            self._set_state("error", message=message)

    # -- session lifecycle ---------------------------------------------------

    async def start_session(self) -> dict:
        if self.session is not None:
            return {"ok": True, "already": True}

        # A new session is a new conversation: nothing from the last one should
        # be replayed into the freshly opened panel. A previous failure clears
        # too — otherwise one missing key leaves the panel red forever, even
        # after the key is added and the daemon restarted.
        self.server.forget("answer", "transcript", "error")
        if self.state == "error":
            self._set_state("idle")

        session = RealtimeSession(
            self.cfg,
            on_audio=self._on_audio,
            on_event=self._on_event,
            on_tool_call=self._on_tool_call,
        )
        try:
            await session.connect()
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            log.error("could not start session: %s", message)
            self.server.broadcast({"type": "error", "message": message})
            self._set_state("error", message=message)
            return {"ok": False, "error": message}

        self.session = session
        self.brain.reset()
        self.gate.reset()
        self.autogain.reset()
        self._drop_queued_audio()
        self.backgrounded = False
        self._emit("session", self.cfg.model)

        # Picked here rather than once at startup: headphones come and go, and
        # so does the display they are competing with. `echo-cancel-source`
        # looks identical in every case, which is exactly why the substitution
        # used to be invisible — so say out loud what was chosen.
        self.devices = await device_choice.resolve(
            self.cfg.input_target, self.cfg.output_target, avoid=self._bad_inputs
        )
        log.info("audio: %s", self.devices.describe())
        asyncio.create_task(self._broadcast_audio()).add_done_callback(_log_task_failure)
        self.mic.target = self.devices.input_target
        self.mic.fallback_target = self.devices.fallback_input
        self.mic.on_fault = self._on_mic_fault
        self.speaker.target = self.devices.output_target

        # Microphone first. On a Bluetooth headset, opening the microphone is
        # what asks WirePlumber to switch the card into its headset profile,
        # and that switch tears down the A2DP sink — taking a player started
        # ahead of it with it. Let the profile settle, then attach playback.
        await self.mic.start()
        await self.speaker.start()
        if self._play_task is None:
            self._play_task = asyncio.create_task(self._play_pump(), name="playback")
        self._set_state("listening")
        self._session_task = asyncio.create_task(self._run_session(session), name="realtime")
        return {"ok": True}

    async def _run_session(self, session: RealtimeSession) -> None:
        try:
            await session.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("realtime session died")
            self.server.broadcast({"type": "error", "message": str(exc)})
            self._set_state("error", message=str(exc))
        finally:
            if self.session is session:
                await self.stop_session()

    async def stop_session(self, keep_audio: bool = False) -> dict:
        """Close the conversation. With keep_audio, leave the pipes running.

        Starting a new conversation has nothing to do with the microphone —
        the history lives on the WebSocket — but tearing the capture and
        playback streams down took the echo canceller with them. It learns the
        delay between speaker and microphone from live audio (delay_agnostic),
        and a restart throws that away: for the next few seconds it subtracts
        nothing, the assistant hears itself, and the transcript fills with
        short fragments in languages nobody spoke. Pressing "new conversation"
        four times bought four windows of deafness.
        """
        session, self.session = self.session, None
        task, self._session_task = self._session_task, None
        self.backgrounded = False

        if not keep_audio:
            await self.mic.stop()
        self._drop_queued_audio()
        play_task, self._play_task = self._play_task, None
        if play_task:
            play_task.cancel()
        # Killing the player is how an answer in flight is cut off. When the
        # audio is being kept, do it only if something is actually playing —
        # otherwise a reset in silence would still suspend the canceller.
        if not keep_audio or self.speaker.playing:
            await self.speaker.flush_now()
        if session:
            await session.close()
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self.state != "error":
            self._set_state("idle")
        # A tool task that finishes after this point must not resurrect
        # "listening"; the guard in _on_tool_call keys off session being None,
        # which it now is.
        return {"ok": True}

    def _on_mic_fault(self, message: str) -> None:
        """A microphone problem, said where it can be seen.

        The panel showing "listening" over a dead capture device was the single
        most misleading thing this program has done.
        """
        log.warning("microphone: %s", message)
        if self.devices and self.devices.input_target:
            self._bad_inputs.add(self.devices.input_target)
        self._emit("error", message)
        self.server.broadcast({"type": "error", "message": message})

    async def _broadcast_audio(self) -> None:
        """Tell the panel what the audio path is and what else it could be.

        Broadcast rather than answered on request: the IPC replays the last
        message of each type to a late joiner, so a settings window opened at
        any moment already knows, without a round trip.
        """
        self.server.broadcast(
            {
                "type": "audio",
                "input": self.cfg.input_target,
                # The log gets the full technical line; the panel gets the
                # short one. Putting `describe()` in a settings window was a
                # mistake — it read as a stack trace where a name belonged.
                "resolved": self.devices.summary if self.devices else "",
                "headphones": bool(self.devices and self.devices.headphones),
                "sources": await device_choice.list_sources(),
            }
        )

    # -- commands from the panel --------------------------------------------

    async def _on_command(self, message: dict) -> dict | None:
        command = str(message.get("cmd") or "")

        if command == "start":
            return await self.start_session()

        if command == "stop":
            return await self.stop_session()

        if command == "background":
            # Put the conversation in the background: the panel is going away
            # but the agent keeps working and the answer still gets spoken.
            #
            # The microphone stops. A session that keeps listening with no
            # window on screen is both a privacy problem and a billing one —
            # and the reason to background this is to wait for an answer, not
            # to keep talking at a panel you cannot see.
            if self.session is None:
                return {"ok": True, "background": False}
            self.backgrounded = True
            await self.mic.stop()
            self.gate.reset()
            self._emit("background", "listening paused")
            self.server.broadcast({"type": "background", "background": True})
            return {"ok": True, "background": True}

        if command == "foreground":
            if self.session is None:
                return {"ok": True, "background": False}
            self.backgrounded = False
            await self.mic.start()
            self._emit("foreground", "listening resumed")
            self.server.broadcast({"type": "background", "background": False})
            return {"ok": True, "background": False}

        if command == "reset":
            # Start over without closing the panel. Three separate memories
            # have to go, or "forget that" only half works:
            #   the Realtime conversation, which the API keeps per connection
            #   the agent's thread, which codex/claude resume by id
            #   the panel's own transcript and waterfall
            # The Realtime API offers no "clear history" event — items can only
            # be deleted one by one, by id — so the honest way to drop it is to
            # reconnect. That costs a second or two and is unambiguous.
            self.brain.reset()
            self.server.forget("answer", "transcript", "error", "query")
            self.server.broadcast({"type": "reset"})

            was_live = self.session is not None
            if was_live:
                await self.stop_session(keep_audio=True)
                result = await self.start_session()
                if not result.get("ok"):
                    return result
            self._emit("reset", "new conversation")
            return {"ok": True, "restarted": was_live}

        if command == "cancel":
            # Shut the assistant up without ending the conversation.
            self._drop_queued_audio()
            await self.speaker.flush_now()
            if self.session:
                await self.session.cancel_response()
            await self.brain.cancel()
            if self.session:
                self._set_state("listening")
            return {"ok": True}

        if command == "apikey":
            key = str(message.get("value") or "").strip()
            if not key:
                return {"ok": False, "error": "empty key"}
            # Shape check only, and never logged: a typo here costs a
            # confusing failure several steps later, at connect time.
            if not key.startswith("sk-"):
                return {"ok": False, "error": "does not look like an OpenAI key"}

            try:
                config.save_api_key(key)
            except OSError as exc:
                log.error("could not save the API key: %s", exc)
                return {"ok": False, "error": str(exc)}

            self.cfg.api_key = key
            log.info("API key updated (%d chars)", len(key))
            self._emit("key", "API key saved")
            self.server.broadcast({"type": "key", "hasKey": True})

            # A key is usually entered because there was no working session.
            if self.session is not None:
                await self.stop_session()
            return {"ok": True, "hasKey": True}

        if command == "voice":
            name = str(message.get("value") or "")
            if name not in config.VOICE_GENDER:
                return {"ok": False, "error": f"unknown voice: {name}"}
            if name == self.cfg.voice:
                return {"ok": True, "voice": name}

            self.cfg.voice = name
            self._save_preferences()

            # The Realtime API fixes the voice for the life of a session — it
            # cannot be changed once the model has produced audio. So switching
            # means reconnecting, which also picks up the new gender rule in
            # the instructions. Only worth doing if a session is actually open.
            if self.session is not None:
                await self.stop_session()
                await self.start_session()
            self._emit("voice", f"{name} · {config.gender_of(name)}")
            self.server.broadcast({"type": "voice", "voice": name})
            return {"ok": True, "voice": name}

        if command == "backend":
            value = str(message.get("value") or "")
            ok = self.brain.set_backend(value)
            if ok:
                self._save_preferences()
                self.server.broadcast({"type": "backend", "backend": self.brain.backend})
            return {"ok": ok, "backend": self.brain.backend}

        if command == "ask":
            # Text-only path: no microphone, no speech. This is how
            # omavoice-ctl exercises the brain on its own.
            answer = await self.ask_brain(str(message.get("query") or ""))
            return {"ok": True, "spoken": answer.spoken, **answer.as_ui_payload()}

        if command == "say":
            # Debug handle: make the assistant speak a specific line, so echo
            # behaviour can be tested without a person in the room.
            if not self.session:
                return {"ok": False, "error": "no session"}
            await self.session.say(str(message.get("text") or "Проверка связи."))
            return {"ok": True}

        if command == "audio":
            # Everything the settings window needs to show the audio path and
            # let someone change it: what is in use, what it resolved to and
            # why, and what else this machine could listen on.
            return {
                "ok": True,
                "input": self.cfg.input_target,
                "resolved": self.devices.summary if self.devices else "",
                "headphones": bool(self.devices and self.devices.headphones),
                "sources": await device_choice.list_sources(),
            }

        if command == "input":
            # "" restores following the system, which is the default and the
            # right answer for almost everyone: it picks the headset when one
            # is worn and the echo canceller when the room is in play.
            value = str(message.get("value") or "")
            if value == self.cfg.input_target:
                return {"ok": True, "input": value}
            self.cfg.input_target = value
            self._save_preferences()
            # The microphone is chosen when a session opens, so an open one has
            # to be rebuilt for the change to mean anything.
            if self.session is not None:
                await self.stop_session()
                result = await self.start_session()
                if not result.get("ok"):
                    return result
            await self._broadcast_audio()
            return {"ok": True, "input": value}

        if command == "status":
            return {
                "ok": True,
                "state": self.state,
                "backend": self.brain.backend,
                "voice": self.cfg.voice,
                "gender": config.gender_of(self.cfg.voice),
                "session": self.session is not None,
                "background": self.backgrounded,
                "hasKey": bool(self.cfg.api_key),
                # Which microphone is actually feeding the session. Worth a line
                # of its own: every device in play can present itself under the
                # same name, and a silent substitution reads as a broken
                # assistant rather than a changed desk.
                "input": self.devices.input_target if self.devices else "",
                "output": self.devices.output_target if self.devices else "",
                "headphones": bool(self.devices and self.devices.headphones),
                "audio": self.devices.describe() if self.devices else "no session yet",
            }

        log.warning("unknown command: %s", command)
        return {"ok": False, "error": f"unknown command: {command}"}

    # -- run -----------------------------------------------------------------

    async def run(self) -> int:
        await self.server.start()
        self._level_task = asyncio.create_task(self._level_pump(), name="levels")
        self.server.broadcast({"type": "state", "state": "idle"})
        self.server.broadcast({"type": "backend", "backend": self.brain.backend})
        self.server.broadcast({"type": "voice", "voice": self.cfg.voice})
        self.server.broadcast({"type": "key", "hasKey": bool(self.cfg.api_key)})
        # The catalogue lives in one place — here — so the panel never has a
        # stale copy of which voices exist or which gender each speaks in.
        self.server.broadcast(
            {
                "type": "voices",
                "voices": [
                    {"name": name, "gender": gender, "label": label}
                    for name, gender, label in config.VOICES
                ],
            }
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stopping.set)

        log.info("ready (backend=%s, key=%s)", self.brain.backend, "yes" if self.cfg.api_key else "NO")
        # So a panel opened before the first conversation already knows what
        # the audio path would be, rather than showing an empty settings page.
        await self._broadcast_audio()
        await self._stopping.wait()

        log.info("shutting down")
        if self._level_task:
            self._level_task.cancel()
        try:
            await asyncio.wait_for(self.stop_session(), timeout=4)
        except asyncio.TimeoutError:
            log.warning("session did not close cleanly; leaving it")
        await self.server.stop()
        return 0


async def _headless(cfg: Config) -> int:
    """Voice with no panel: talk, get an answer, Ctrl-C to stop."""
    daemon = Daemon(cfg)
    await daemon.server.start()
    daemon._level_task = asyncio.create_task(daemon._level_pump(), name="levels")

    result = await daemon.start_session()
    if not result.get("ok"):
        print(f"не удалось начать сессию: {result.get('error')}", file=sys.stderr)
        return 1

    print("Говори. Ctrl-C чтобы выйти.")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon._stopping.set)
    await daemon._stopping.wait()

    if daemon._level_task:
        daemon._level_task.cancel()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(daemon.stop_session(), timeout=4)
    await daemon.server.stop()
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="omavoice", description="Voice assistant daemon for Omarchy")
    parser.add_argument("--headless", action="store_true", help="start a session immediately, without the panel")
    parser.add_argument("--backend", choices=("codex", "claude"), default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = config.load()
    if args.backend:
        cfg.backend = args.backend
    if args.debug:
        cfg.debug = True

    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Debug mode is for diagnosing this daemon, not the websocket library: its
    # frame log buries every line worth reading under pages of base64 audio.
    logging.getLogger("websockets").setLevel(logging.WARNING)

    runner = _headless(cfg) if args.headless else Daemon(cfg).run()
    try:
        return asyncio.run(runner)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
