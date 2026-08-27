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
import signal
import sys
import time

import json

from . import config, ipc
from .audio import Microphone, NoiseGate, Speaker
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


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.brain = Brain(cfg)
        self.server = ipc.Server(cfg.socket_path, self._on_command)

        self.speaker = Speaker(cfg, on_level=self._on_output_level)
        self.mic = Microphone(cfg, self._on_input_chunk)
        self.gate = NoiseGate(cfg.gate_level, chunk_ms=cfg.chunk_ms)
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
                json.dumps({"voice": self.cfg.voice, "backend": self.brain.backend}, indent=2)
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
        session = self.session
        if self.backgrounded:
            return
        if not (session and session.connected and self.state in ("listening", "speaking")):
            return

        # Not "are we in the speaking state" but "is sound still in the room".
        # The model streams an answer faster than it plays, so the state can
        # flip back to listening while the speakers are still working through
        # the buffer — and that gap is where the assistant used to hear itself.
        threshold = self.cfg.gate_level
        if self._room_is_loud():
            threshold *= _SPEAKING_GATE_MULTIPLIER

        if self.gate.step(level, threshold):
            self._pending_level = max(self._pending_level, level)
            self._pending_bands = bands
        else:
            # Silence of the same length: the stream stays continuous for the
            # server VAD, but there is nothing in it to mistake for a turn.
            chunk = bytes(len(chunk))
            self._pending_level = max(self._pending_level, 0.0)

        asyncio.create_task(session.send_audio(chunk))

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

    async def _on_event(self, event: dict) -> None:
        kind = event.get("type", "")

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
        self._drop_queued_audio()
        self.backgrounded = False
        self._emit("session", self.cfg.model)
        await self.speaker.start()
        await self.mic.start()
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

    async def stop_session(self) -> dict:
        session, self.session = self.session, None
        task, self._session_task = self._session_task, None
        self.backgrounded = False

        await self.mic.stop()
        self._drop_queued_audio()
        play_task, self._play_task = self._play_task, None
        if play_task:
            play_task.cancel()
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
                await self.stop_session()
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

    runner = _headless(cfg) if args.headless else Daemon(cfg).run()
    try:
        return asyncio.run(runner)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
