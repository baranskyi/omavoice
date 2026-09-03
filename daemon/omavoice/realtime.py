"""The OpenAI Realtime session — the voice and the ears, and nothing more.

The model here is deliberately not the thing that answers questions. Its
instructions make it a conversational front end that routes anything factual to
the `ask_agent` tool, which runs a local coding agent with real access to this
machine. Two reasons, and both matter:

  * It is what makes the assistant local. A model answering from its own
    weights cannot tell you which of your projects mentions Omarchy.
  * Audio tokens are the expensive ones. Spending them on speech rather than on
    reasoning is the difference between about two cents a minute and twenty.

A plain websocket, not the SDK: the SDK's realtime helper adds a dependency and
an abstraction over what is already just JSON frames over a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import base64
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from . import config
from .config import Config

log = logging.getLogger("omavoice.realtime")

WS_URL = "wss://api.openai.com/v1/realtime"

# The biggest thing the Realtime API sends is one audio delta: base64 of PCM16
# at the session's rate. At 24 kHz that is 48 000 bytes of audio per second, and
# base64 spends four characters on every three bytes, so a second of speech is
# 64 000 characters. Eight seconds is the ceiling — deltas stream as the model
# speaks and are a fraction of a second each, so this is a bound on what the
# server could plausibly have meant to send, not a working size.
MAX_AUDIO_DELTA_B64 = 8 * 64_000

# One inbound frame, sized to hold that delta plus its JSON envelope and no
# more. Everything else the API sends — transcripts, the session echo,
# response.done, a function call's arguments — is kilobytes. websockets
# compares this against the length in the frame header and raises PayloadTooBig
# before reading the payload, so an oversized frame is refused rather than
# allocated, and a fragmented message is charged against the same budget
# fragment by fragment.
MAX_FRAME_BYTES = 512 * 1024

# How many frames may wait unread. The read loop hands each audio delta to the
# speaker before taking the next, so some slack absorbs a brief stall; past this
# mark websockets stops reading the socket and the stall reaches the server as
# TCP backpressure. The depth is also what turns the frame ceiling into a memory
# ceiling: the two multiplied, sixteen megabytes, is the most a remote producer
# can make this process hold — against no bound at all when frames were
# unlimited and 256 of them could queue.
MAX_INBOUND_QUEUE = 32

# The session declares exactly one tool, and the brain runs one agent at a time,
# so a second concurrent ask_agent has nothing of its own to run on. Refusing it
# here keeps the number of agent jobs this class can start at one, whatever the
# model emits.
MAX_ACTIVE_TOOL_CALLS = 1

# A function call's arguments as they arrive, JSON text. The schema has a single
# field holding a spoken question; 8 KiB is longer than any turn a person
# speaks, and short of a blob worth parsing on trust.
MAX_TOOL_ARGUMENTS = 8 * 1024

# And the query taken out of it, which is what reaches the agent. The prompt
# asks for one self-contained question carrying the context of earlier turns —
# a sentence or two. Longer than this is not a question any more.
MAX_QUERY_CHARS = 2000

# Many languages mark the speaker's gender — Russian and Hebrew on past-tense
# verbs, Arabic and Spanish on adjectives — and a female voice using masculine
# forms is the first thing a native speaker notices. English speakers never see
# this rule fire, which is exactly why it has to be stated rather than assumed.
_GENDER_RULE = {
    "female": (
        "You are female and your voice is female. In every language that marks "
        "grammatical gender on verbs or adjectives, always use FEMININE forms "
        "when speaking about yourself (Russian: посмотрела, нашла, я готова; "
        "Hebrew: עשיתי as a woman; Spanish: lista, not listo). Using masculine "
        "forms about yourself is a serious error — watch it in every sentence."
    ),
    "male": (
        "You are male and your voice is male. In every language that marks "
        "grammatical gender on verbs or adjectives, always use MASCULINE forms "
        "when speaking about yourself (Russian: посмотрел, нашёл, я готов; "
        "Spanish: listo, not lista). Using feminine forms about yourself is a "
        "serious error — watch it in every sentence."
    ),
}


def instructions_for(gender: str) -> str:
    # First, not last. A rule buried at the end of a long prompt loses to the
    # model's own defaults over a conversation, and the masculine past tense is
    # exactly the default it drifts back to.
    return _GENDER_RULE.get(gender, _GENDER_RULE["female"]) + "\n\n" + INSTRUCTIONS


INSTRUCTIONS = """\
You are the voice of a local assistant running on this computer. You are its \
ears and its mouth — you are NOT its memory and NOT its source of facts.

You never speak first. A session opens because someone pressed a key, not \
because they said anything: the panel simply shows that it is listening, and \
the first word of the conversation is theirs. Do not greet, do not introduce \
yourself, do not ask what they need, do not fill the silence.

Hard rule: any question whose answer depends on facts — about files, projects, \
configs, running processes on this machine, or about people, companies, news, \
prices, code, documentation, anything in the outside world — you MUST send to \
the ask_agent tool. Never answer such a question yourself, even when you are \
confident, even when it seems trivial. You will almost certainly be wrong: you \
have no access to this computer and no access to today.

You answer directly, without the tool, only for conversational turns: \
greetings, thanks, "say that again", "cancel", "louder", or a clarifying \
question about what the person just said.

Calling the tool: pass ask_agent a complete, self-contained question that \
includes the context from earlier turns. Not "and the second one?" but "tell me \
more about the second project in that list — the one about Omarchy". Write the \
query in the language of the conversation.

Before calling the tool, say a very short filler — "one sec", "let me look", \
"checking". One or two words, not a sentence.

When the tool returns, speak its `spoken` field in your own words. Lively and \
short: one to three sentences. Never read markdown, links, file paths or \
numbered lists out loud — the person can see those on screen. If the answer is \
a list, say how many items it has and name the two that matter.

LANGUAGE: always reply in the language the person is speaking to you in, and \
switch the moment they switch. There is no default language — follow theirs, \
whatever it is. Tone: calm and friendly, like a colleague sitting next to \
them, not like a newsreader.

About silence: the microphone sits in a room and sometimes mistakes noise for \
speech — a fan, a keyboard, a fragment of sound from the speakers. If what you \
heard is disjointed words, a lone interjection, a phrase in a language nobody \
was speaking to you in, or simply unintelligible — SAY NOTHING. Do not answer, \
do not ask them to repeat, do not greet them. Just wait for a real turn. \
Missing one sentence is better than talking to an empty room.
"""


def ask_agent_tool(cfg) -> dict:
    """The tool description, told the truth about what the agent can reach.

    This used to be a constant saying the agent had "this computer's
    filesystem, its shell and the internet" — which is what the agent has when
    the person has widened it, and a lie in the default state, where reading is
    held to one folder, the web tools are off and the connectors are disabled.
    Two things went wrong with that. The model routed web and shell questions to
    an agent that could not serve them and then spoke the refusal, or something
    it made up, in the person's own language. And the permissive account was the
    one that reached the model while the restrictive one reached the human, so
    the two halves of the product described different programs.
    """
    wide = cfg.backend in cfg.unrestricted
    if wide:
        reach = (
            "It has this computer's filesystem, its shell, its connectors and "
            "the internet."
        )
    else:
        folder = str(cfg.brain_cwd) if cfg.brain_cwd else "a folder the person chose"
        reach = (
            f"It can read {folder} and nothing else of theirs — no other folder, "
            "no web search, no connectors, and no shell. If a question needs "
            "something outside that, say so plainly instead of guessing; the "
            "person can widen it in the panel's settings."
        )
    return dict(
        ASK_AGENT_SHAPE,
        description=(
            "Ask the local agent. Use it for ANY question of fact: files, "
            "projects, code, configs, processes, and equally people, companies, "
            "news, documentation, anything in the outside world. " + reach
        ),
    )


ASK_AGENT_SHAPE = {
    "type": "function",
    "name": "ask_agent",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A complete, self-contained question in the language of the "
                    "conversation, including all context from earlier turns."
                ),
            }
        },
    },
}


class RealtimeSession:
    """One live conversation with the Realtime API.

    Everything the rest of the daemon needs to react to arrives through the
    callbacks; everything it wants to say goes through send_audio / respond.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        on_audio: Callable[[bytes], Awaitable[None]],
        on_event: Callable[[dict], Awaitable[None]],
        on_tool_call: Callable[[str, str], Awaitable[str]],
    ) -> None:
        self.cfg = cfg
        self.on_audio = on_audio
        self.on_event = on_event
        self.on_tool_call = on_tool_call
        self._ws: websockets.ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._tools: dict[str, asyncio.Task] = {}
        # response.cancel on a finished response is an error, and the API says
        # so out loud — which would surface in the panel as a red failure. So
        # we track what is actually in flight instead of cancelling hopefully.
        self._response_active = False
        # The floor belongs to the person until they have used it once. See
        # open_floor() for why this is a switch in the daemon and not a line
        # in the prompt.
        self._await_first_turn = True
        self.sent_events = 0
        self._warned_no_socket = False
        # Loop time of the last frame from the server, of any kind.
        self.last_activity = 0.0

    @property
    def awaiting_first_turn(self) -> bool:
        return self._await_first_turn

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        if not self.cfg.api_key:
            # Kept short: this lands in a small panel, not a terminal.
            raise RuntimeError("No OpenAI API key — add one in settings")

        url = f"{WS_URL}?model={self.cfg.model}"
        log.info("connecting to %s", self.cfg.model)
        self._await_first_turn = True
        self.last_activity = asyncio.get_running_loop().time()
        self.sent_events = 0
        self._warned_no_socket = False
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            # A finite protocol budget. Both of these used to be set the other
            # way — no frame limit at all, 256 of them queued — which put how
            # much this process allocates entirely in the remote end's hands.
            max_size=MAX_FRAME_BYTES,
            max_queue=MAX_INBOUND_QUEUE,
            # A pong window long enough to survive a busy read loop rather than
            # tearing the conversation down mid-sentence.
            ping_interval=20,
            ping_timeout=60,
        )
        await self._configure()

    async def _configure(self) -> None:
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": instructions_for(config.gender_of(self.cfg.voice)),
                    "tools": [ask_agent_tool(self.cfg)],
                    "tool_choice": "auto",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": self.cfg.sample_rate},
                            "noise_reduction": {"type": "near_field"},
                            "transcription": {"model": self.cfg.transcription_model},
                            "turn_detection": {
                                "type": "server_vad",
                                # Off until the person has spoken once: with it
                                # on, server VAD answers whatever it decided was
                                # a turn, and at the moment a panel opens that is
                                # usually the room rather than a person.
                                "create_response": not self._await_first_turn,
                                "threshold": self.cfg.vad_threshold,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": self.cfg.silence_ms,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": self.cfg.sample_rate},
                            "voice": self.cfg.voice,
                        },
                    },
                },
            }
        )

    async def open_floor(self) -> None:
        """Hand the conversation over to the model, and answer this turn.

        A session opens on a keypress, so the first thing the microphone hears
        is a room, not a request — and with the server creating responses on
        its own, the assistant greeted an empty desk and then chatted with the
        fan. The prompt asks it not to; a prompt is a request, and this is the
        guarantee: the server is told not to answer at all until the daemon
        has seen a transcript that reads like someone addressing it.

        Only the opening turn pays for this. From here on the session behaves
        normally, answering the moment VAD hears the end of a sentence rather
        than waiting for its transcript.
        """
        if not self._await_first_turn:
            return
        self._await_first_turn = False
        await self._configure()
        await self._send({"type": "response.create"})

    async def cancel_tools(self) -> None:
        """Abandon every question the agent is still working on.

        Cancelling the task is what actually stops an answer, and it has to
        happen before the tool result is sent: once `function_call_output` and
        `response.create` are on the wire, the model will speak, and no amount
        of muting afterwards unsays it. Cancellation reaches the agent
        subprocess through the brain, which reaps it.
        """
        tasks = list(self._tools.values())
        self._tools.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def close(self) -> None:
        # Awaited, not merely cancelled. A bare cancel() returns before the task
        # has seen it, so the session closed with agent jobs still pending and
        # still holding whatever the brain had started for them.
        await self.cancel_tools()
        ws, self._ws = self._ws, None
        if ws:
            # close() waits for the server's half of the handshake, which a
            # dropped connection never sends.
            try:
                await asyncio.wait_for(ws.close(), timeout=2)
            except (asyncio.TimeoutError, Exception):  # noqa: B014
                log.debug("websocket close timed out")

    # -- outbound -----------------------------------------------------------

    async def _send(self, event: dict) -> None:
        ws = self._ws
        if ws is None:
            # Audio is sent fire-and-forget, so a socket that quietly went away
            # would otherwise show up as nothing at all: the microphone keeps
            # metering, the panel keeps saying "listening", and the server
            # never hears another word. Say it once, loudly.
            if not self._warned_no_socket:
                self._warned_no_socket = True
                log.warning("dropping %s — no socket", event.get("type"))
            return
        async with self._send_lock:
            try:
                await ws.send(json.dumps(event))
                self.sent_events += 1
            except websockets.ConnectionClosed as closed:
                log.warning(
                    "send on a closed socket: %s (code=%s)", event.get("type"), closed.code
                )
                self._ws = None
            except Exception:  # noqa: BLE001
                # Anything else used to vanish into an un-awaited task.
                log.exception("send failed: %s", event.get("type"))

    async def send_audio(self, pcm: bytes) -> None:
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
        )

    async def cancel_response(self) -> None:
        """Stop the answer in flight — the person started talking over it."""
        if not self._response_active:
            return
        self._response_active = False
        await self._send({"type": "response.cancel"})

    async def say(self, text: str) -> None:
        """Have the model speak, following a one-off instruction.

        Passed through as the response instruction rather than wrapped in
        "say this verbatim" — the wrapper made the model discuss the request
        instead of answering it.
        """
        await self._send({"type": "response.create", "response": {"instructions": text}})

    # -- inbound ------------------------------------------------------------

    async def run(self) -> None:
        """Read events until the socket closes. Raises on transport failure."""
        ws = self._ws
        if ws is None:
            raise RuntimeError("run() before connect()")
        try:
            async for raw in ws:
                # The transport already refused anything larger, at the frame
                # header. Stating the ceiling again here is what keeps it true
                # of the parsing path itself rather than of a keyword argument
                # thirty lines away.
                if len(raw) > MAX_FRAME_BYTES:
                    log.warning("dropping an oversized frame (%d)", len(raw))
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle(event)
        except websockets.ConnectionClosed as closed:
            # Which side hung up, and why, is the whole diagnosis when a
            # conversation ends by itself mid-sentence.
            log.warning(
                "realtime socket closed: code=%s reason=%r",
                closed.code,
                (closed.reason or "")[:200],
            )
            raise
        log.info("realtime stream ended cleanly")

    async def _handle(self, event: dict) -> None:
        kind = event.get("type", "")

        # Every frame counts as a sign of life, deltas included. They are the
        # only thing arriving during a long spoken answer, and leaving them out
        # made a healthy session look silent for as long as the answer lasted —
        # which the daemon's watchdog then cut off as a wedge.
        self.last_activity = asyncio.get_running_loop().time()

        # Audio deltas are the hot path and are not worth logging.
        if kind in ("response.output_audio.delta", "response.audio.delta"):
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            # Checked in the encoded form, so the decision is made before the
            # three-quarters-as-large decoded copy exists.
            if len(delta) > MAX_AUDIO_DELTA_B64:
                log.warning("dropping an oversized audio delta (%d)", len(delta))
                return
            try:
                pcm = base64.b64decode(delta)
            except ValueError:
                log.warning("undecodable audio delta")
                return
            await self.on_audio(pcm)
            return

        if kind in ("session.created", "session.updated"):
            # What the server actually applied, which is not necessarily what
            # we asked for: an unknown or misplaced field is dropped in
            # silence, and the session then runs on defaults nobody chose.
            session = event.get("session") or {}
            audio_in = (session.get("audio") or {}).get("input") or {}
            log.info(
                "%s: turn_detection=%s input_format=%s",
                kind,
                audio_in.get("turn_detection"),
                (audio_in.get("format") or {}),
            )

        if kind == "response.created":
            self._response_active = True
        elif kind in ("response.done", "response.cancelled"):
            self._response_active = False

        if kind == "response.function_call_arguments.done":
            await self._start_tool(event)
            return

        if self.cfg.debug and kind != "response.output_audio_transcript.delta":
            log.debug("event %s", kind)

        await self.on_event(event)

    async def _answer_call(self, call_id: str, spoken: str, *, respond: bool) -> None:
        """Hand one function call its result, and optionally ask to speak.

        A refusal issued while another job is in flight sets respond=False:
        that job sends its own response.create when it finishes, and two of
        them for one turn make the model answer twice.
        """
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"spoken": spoken}, ensure_ascii=False),
                },
            }
        )
        if respond:
            await self._send({"type": "response.create"})

    async def _start_tool(self, event: dict) -> None:
        """Run the tool without blocking the event loop.

        The agent can take tens of seconds. Blocking here would stop us reading
        the socket, which means we would also stop noticing that the person has
        started speaking again.
        """
        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "")
        if not call_id:
            return

        if call_id in self._tools:
            # The same call arriving twice. Nothing goes back on the wire: the
            # task already holding this call_id will send the one
            # function_call_output it is owed, and a second output for the same
            # id is a protocol error. Overwriting the entry, which is what used
            # to happen, left the first task running with nobody holding its
            # handle — so cancelling the conversation never reached it.
            log.warning("ignoring a repeat of tool call %s", call_id)
            return

        if len(self._tools) >= MAX_ACTIVE_TOOL_CALLS:
            log.warning(
                "refusing tool call %s — %d already running", call_id, len(self._tools)
            )
            await self._answer_call(
                call_id,
                "Still working on the previous question — ask again after it.",
                respond=False,
            )
            return

        raw_args = event.get("arguments") or "{}"
        if not isinstance(raw_args, str) or len(raw_args) > MAX_TOOL_ARGUMENTS:
            log.warning("refusing tool call %s — arguments of %d", call_id, len(raw_args))
            await self._answer_call(
                call_id,
                "That question did not come through — ask again.",
                respond=not self._tools,
            )
            return
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        query = str(args.get("query") or "").strip()
        if len(query) > MAX_QUERY_CHARS:
            log.warning("truncating a %d character query", len(query))
            query = query[:MAX_QUERY_CHARS]

        async def _run() -> None:
            try:
                try:
                    result = await self.on_tool_call(name, query)
                except Exception as exc:  # noqa: BLE001
                    log.exception("tool %s failed", name)
                    result = f"The agent failed: {exc}"
                # Cancellation is a BaseException and passes straight through
                # here, which is the point: a cancelled question must not send
                # its answer, because once it is on the wire the model speaks.
                await self._answer_call(call_id, result, respond=True)
            finally:
                # However this ended — answered, failed or cancelled — the slot
                # goes back, or the cap would leak one call at a time. Only if
                # it is still ours: cancel_tools() empties the dict from under
                # us, and a later call may have reused the id by then.
                if self._tools.get(call_id) is asyncio.current_task():
                    del self._tools[call_id]

        self._tools[call_id] = asyncio.create_task(_run(), name=f"tool-{call_id}")
