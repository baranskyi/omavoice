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
import base64
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from . import config
from .config import Config

log = logging.getLogger("omavoice.realtime")

WS_URL = "wss://api.openai.com/v1/realtime"

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


ASK_AGENT_TOOL = {
    "type": "function",
    "name": "ask_agent",
    "description": (
        "Ask the local agent, which has access to this computer's filesystem, its "
        "shell and the internet. Use it for ANY question of fact: files, projects, "
        "code, configs, processes, and equally people, companies, news, "
        "documentation, anything in the outside world."
    ),
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

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        if not self.cfg.api_key:
            # Kept short: this lands in a small panel, not a terminal.
            raise RuntimeError("No OpenAI API key — add one in settings")

        url = f"{WS_URL}?model={self.cfg.model}"
        log.info("connecting to %s", self.cfg.model)
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            max_size=None,
            # Belt and braces around the read loop being briefly busy: a deeper
            # inbound queue, and a pong window long enough to survive a stall
            # rather than tearing the conversation down mid-sentence.
            max_queue=256,
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
                    "tools": [ASK_AGENT_TOOL],
                    "tool_choice": "auto",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": self.cfg.sample_rate},
                            "noise_reduction": {"type": "near_field"},
                            "transcription": {"model": self.cfg.transcription_model},
                            "turn_detection": {
                                "type": "server_vad",
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

    async def close(self) -> None:
        for task in self._tools.values():
            task.cancel()
        self._tools.clear()
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
            return
        async with self._send_lock:
            try:
                await ws.send(json.dumps(event))
            except websockets.ConnectionClosed:
                log.warning("send on a closed socket: %s", event.get("type"))
                self._ws = None

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

        # Audio deltas are the hot path and are not worth logging.
        if kind in ("response.output_audio.delta", "response.audio.delta"):
            delta = event.get("delta")
            if delta:
                await self.on_audio(base64.b64decode(delta))
            return

        if kind == "response.created":
            self._response_active = True
        elif kind in ("response.done", "response.cancelled"):
            self._response_active = False

        if kind == "response.function_call_arguments.done":
            self._start_tool(event)
            return

        if self.cfg.debug and kind != "response.output_audio_transcript.delta":
            log.debug("event %s", kind)

        await self.on_event(event)

    def _start_tool(self, event: dict) -> None:
        """Run the tool without blocking the event loop.

        The agent can take tens of seconds. Blocking here would stop us reading
        the socket, which means we would also stop noticing that the person has
        started speaking again.
        """
        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "")
        if not call_id:
            return

        try:
            args = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        query = str(args.get("query") or "").strip()

        async def _run() -> None:
            try:
                result = await self.on_tool_call(name, query)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("tool %s failed", name)
                result = f"The agent failed: {exc}"

            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"spoken": result}, ensure_ascii=False),
                    },
                }
            )
            await self._send({"type": "response.create"})
            self._tools.pop(call_id, None)

        self._tools[call_id] = asyncio.create_task(_run(), name=f"tool-{call_id}")
