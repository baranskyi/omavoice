"""Stream a PCM file into the Realtime API and print what it heard.

The microphone is the hardest part of this program to reason about, and for a
long stretch it was also the only way to reproduce a fault: to see the bug
again, someone had to speak. That makes every experiment a different
experiment — a different sentence, a different distance, a different room — and
two runs cannot be compared, so a wrong theory survives far longer than it
should.

This removes the microphone, the room and the person from the loop. The same
bytes go in every time; the only variable left is what the far end does with
them. If a file that plainly contains a sentence comes back as two words, the
fault is at or above the socket and no amount of gate tuning will touch it. If
it comes back whole, the fault is below — in capture, in the gate, in the way
chunks are handed to the socket — and that is a much smaller place to look.

The session is configured by the daemon's own code, not a copy of it: a probe
that configures itself differently would answer a question nobody asked.

    python -m omavoice.probe material.raw
    python -m omavoice.probe material.raw --chunk-ms 100 --pace 2.0

Input is raw PCM in the format the daemon sends: signed 16-bit little-endian,
mono, at the configured sample rate. `OMAVOICE_VOICE_DUMP=/tmp/material.raw`
makes the assistant record its own voice in exactly that shape.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from . import config
from .realtime import RealtimeSession

log = logging.getLogger("omavoice.probe")


async def _emit(
    session: RealtimeSession, chunk: bytes, detach: bool, pending: list[asyncio.Task]
) -> None:
    """Hand one frame to the socket, awaited or detached.

    The daemon detaches: a chunk that waited for the socket would hold up the
    microphone behind it. Whether that costs ordering or content is exactly the
    kind of thing worth being able to switch on and off under a fixed input.
    """
    if not detach:
        await session.send_audio(chunk)
        return
    pending.append(asyncio.create_task(session.send_audio(chunk)))


async def _probe(
    path: Path,
    cfg: config.Config,
    chunk_ms: int,
    pace: float,
    wait: float,
    detach: bool,
) -> int:
    heard: list[str] = []
    marks: list[str] = []

    async def on_audio(_pcm: bytes) -> None:
        # The probe never plays anything. Answers are irrelevant here — what is
        # being measured is what the far end thinks it was told.
        return None

    async def on_event(event: dict) -> None:
        kind = str(event.get("type") or "")
        stamp = f"{time.monotonic() - started:6.2f}s"
        if kind == "conversation.item.input_audio_transcription.completed":
            text = str(event.get("transcript") or "").strip()
            heard.append(text)
            print(f"{stamp}  heard: {text!r}")
        elif kind in (
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
        ):
            marks.append(kind)
            print(f"{stamp}  {kind.rsplit('.', 1)[-1]}")
        elif kind == "session.updated":
            audio_in = ((event.get("session") or {}).get("audio") or {}).get("input") or {}
            print(f"{stamp}  applied: {audio_in.get('turn_detection')}")
        elif kind == "error":
            print(f"{stamp}  error: {event.get('error')}")

    async def on_tool_call(_name: str, _query: str) -> str:
        return "The probe has no agent."

    pcm = path.read_bytes()
    frame = cfg.chunk_bytes if chunk_ms == cfg.chunk_ms else int(
        cfg.sample_rate * chunk_ms / 1000
    ) * cfg.channels * 2
    seconds = len(pcm) / (cfg.sample_rate * cfg.channels * 2)
    print(
        f"{path}: {len(pcm)} bytes = {seconds:.2f}s at {cfg.sample_rate} Hz, "
        f"sending {frame}-byte frames ({chunk_ms} ms) at {pace}x"
    )

    session = RealtimeSession(
        cfg, on_audio=on_audio, on_event=on_event, on_tool_call=on_tool_call
    )
    started = time.monotonic()
    await session.connect()
    reader = asyncio.create_task(session.run(), name="probe-read")

    try:
        # Paced deliberately rather than dumped in one go: the server's turn
        # detection works on a stream, and a file arriving all at once is not
        # the thing we are trying to reproduce.
        pending: list[asyncio.Task] = []
        for offset in range(0, len(pcm) - frame + 1, frame):
            await _emit(session, pcm[offset : offset + frame], detach, pending)
            await asyncio.sleep((chunk_ms / 1000) / max(0.01, pace))
        if pending:
            await asyncio.gather(*pending)
        print(f"{time.monotonic() - started:6.2f}s  sent {len(pcm) // frame} frames")

        # Silence afterwards, so server VAD sees the end of the turn the same
        # way it would with a person who stopped talking.
        silence = bytes(frame)
        for _ in range(int(wait * 1000 / chunk_ms)):
            await _emit(session, silence, detach, pending)
            await asyncio.sleep((chunk_ms / 1000) / max(0.01, pace))
        if pending:
            await asyncio.gather(*pending)
    finally:
        reader.cancel()
        await session.close()

    print()
    if not heard:
        print("VERDICT: nothing was transcribed at all.")
        return 2
    joined = " ".join(heard)
    print(f"VERDICT: {len(heard)} turn(s), {len(joined.split())} words: {joined!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omavoice.probe", description=__doc__.splitlines()[0]
    )
    parser.add_argument("file", type=Path, help="raw PCM16 mono at the configured rate")
    parser.add_argument(
        "--chunk-ms", type=int, default=0, help="frame size to send (default: the daemon's)"
    )
    parser.add_argument(
        "--pace", type=float, default=1.0, help="1.0 is real time; 2.0 is twice as fast"
    )
    parser.add_argument(
        "--wait", type=float, default=2.5, help="seconds of silence to send afterwards"
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="send frames the way the daemon does: one task each, not awaited",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)

    cfg = config.load()
    if not cfg.api_key:
        print("No OpenAI API key — see ~/.config/omavoice/env", file=sys.stderr)
        return 1
    if not args.file.exists():
        print(f"{args.file}: no such file", file=sys.stderr)
        return 1

    return asyncio.run(
        _probe(
            args.file,
            cfg,
            args.chunk_ms or cfg.chunk_ms,
            args.pace,
            args.wait,
            args.detach,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
