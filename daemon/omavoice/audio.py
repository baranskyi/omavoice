"""Microphone in, speaker out — both through PipeWire's own CLI.

pw-record and pw-play in --raw mode speak exactly the format the Realtime API
wants (PCM16, 24 kHz, mono), and they resample for us, so there is no audio
library here and nothing to build. Two long-lived subprocesses, one asyncio
task each.

The only subtle part is barge-in. When the person starts talking over the
assistant, the audio already handed to pw-play is committed — it will play out
of the buffer no matter what we stop sending. The only way to cut it off is to
kill the player and start a new one, which costs about 50 ms and is well worth
it: an assistant that keeps talking over you is the single thing that makes a
voice interface feel broken.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
from collections.abc import Callable

from .config import Config

log = logging.getLogger("omavoice.audio")

# Kill children with the daemon, however the daemon dies. This is Omarchy's own
# pattern for shell-spawned processes (see plugins/clipboard/Clipboard.qml) and
# it is what keeps a crashed daemon from leaving a hot microphone behind.
_PDEATHSIG = ["setpriv", "--pdeathsig", "TERM"]

# Roughly the RMS of comfortable speech in 16-bit samples. Dividing by it maps
# normal talking to most of the meter without clipping on a loud laugh.
_FULL_SCALE = 6000.0


# Four bands across the speech range. Loudness alone makes a waveform that
# only breathes; these let it change shape with the voice — a vowel and a
# sibilant land in different bands and the figure reads differently for each.
# Geometric spacing because pitch perception is, and because the top band
# doing all the work is what makes speech look like speech.
_BANDS_HZ = (200.0, 600.0, 1600.0, 4000.0)

# Speech energy falls off steeply with frequency, so each band gets its own
# ceiling. Without this the top two bands never leave the floor and the figure
# only ever moves as one lump.
_BAND_SCALE = (2200.0, 1600.0, 700.0, 320.0)

# A 20 ms chunk resolves to 50 Hz bins and leaks badly enough that a 200 Hz
# tone lights every band. Analysing the last ~43 ms instead resolves to ~23 Hz,
# which is the difference between four bands and four copies of one band.
_ANALYSIS_SAMPLES = 1024


def _samples_of(pcm: bytes) -> array.array:
    if len(pcm) < 2:
        return array.array("h")
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return samples


def rms_level(pcm: bytes) -> float:
    """Normalised 0..1 loudness of a PCM16 chunk, for the waveform.

    Hand-rolled because audioop was removed in Python 3.13 — and this is four
    lines, so there is nothing to miss.
    """
    samples = _samples_of(pcm)
    if not samples:
        return 0.0
    mean_square = sum(s * s for s in samples) / len(samples)
    return min(1.0, math.sqrt(mean_square) / _FULL_SCALE)


class BandAnalyser:
    """Rolling four-band spectrum, cheap enough to run on every audio chunk.

    Goertzel rather than an FFT: we want four specific bins, not a spectrum,
    and four Goertzels over a 1024-sample window is a few thousand
    multiply-adds — no numpy to install, no C extension to build.
    """

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self._buffer = array.array("h")
        # A Hann window costs one multiply per sample and buys perhaps 30 dB
        # of sidelobe rejection; without it the bands bleed into each other
        # badly enough to be useless.
        self._window = [
            0.5 - 0.5 * math.cos(2.0 * math.pi * i / (_ANALYSIS_SAMPLES - 1))
            for i in range(_ANALYSIS_SAMPLES)
        ]
        self._coeffs = [
            2.0 * math.cos(2.0 * math.pi * round(hz * _ANALYSIS_SAMPLES / rate) / _ANALYSIS_SAMPLES)
            for hz in _BANDS_HZ
        ]

    def push(self, pcm: bytes) -> list[float]:
        chunk = _samples_of(pcm)
        if not chunk:
            return [0.0] * len(_BANDS_HZ)

        self._buffer.extend(chunk)
        if len(self._buffer) > _ANALYSIS_SAMPLES:
            del self._buffer[: len(self._buffer) - _ANALYSIS_SAMPLES]
        if len(self._buffer) < _ANALYSIS_SAMPLES:
            return [0.0] * len(_BANDS_HZ)

        windowed = [v * w for v, w in zip(self._buffer, self._window)]

        out = []
        for coeff, scale in zip(self._coeffs, _BAND_SCALE):
            s1 = s2 = 0.0
            for value in windowed:
                s0 = value + coeff * s1 - s2
                s2 = s1
                s1 = s0
            power = s1 * s1 + s2 * s2 - coeff * s1 * s2
            magnitude = math.sqrt(max(0.0, power)) / _ANALYSIS_SAMPLES
            out.append(min(1.0, magnitude / scale))
        return out


class NoiseGate:
    """Passes speech, blocks everything quieter.

    The server VAD alone is not enough on a laptop. It hears the room, the
    fans, the keyboard and whatever the echo canceller could not quite remove,
    transcribes the result into confident nonsense — "はい。", "Gjiliv." — and
    the assistant dutifully answers it. One of those answers is amusing; a
    conversation made entirely of them is unusable.

    So audio below a threshold is replaced with silence before it is ever sent.
    Replaced rather than dropped: the VAD wants an unbroken stream, and a gap
    in it confuses turn detection.

    Two details make this feel natural rather than clipped:

      hysteresis  it takes more level to open than to stay open, so a voice
                  hovering near the threshold does not chatter the gate
      hangover    once open it stays open briefly, so the quiet tail of a
                  sentence is not amputated mid-word
    """

    def __init__(self, threshold: float, hangover_ms: int = 700, chunk_ms: int = 20) -> None:
        self.threshold = threshold
        self.release = max(1, int(hangover_ms / max(1, chunk_ms)))
        self._open = False
        self._countdown = 0

    def step(self, level: float, threshold: float | None = None) -> bool:
        """True when this chunk should pass through."""
        opening = self.threshold if threshold is None else threshold
        # 60% of the opening level: enough hysteresis to stop chattering,
        # little enough that a trailing syllable still counts as speech.
        staying = opening * 0.6

        if level >= opening:
            self._open = True
            self._countdown = self.release
        elif self._open and level >= staying:
            self._countdown = self.release
        elif self._open:
            self._countdown -= 1
            if self._countdown <= 0:
                self._open = False
        return self._open

    def reset(self) -> None:
        self._open = False
        self._countdown = 0


def _pw_common(cfg: Config) -> list[str]:
    return ["--raw", "--rate", str(cfg.sample_rate), "--channels", str(cfg.channels), "--format", "s16"]


class Microphone:
    """Reads fixed-size PCM chunks and hands them to a callback."""

    def __init__(self, cfg: Config, on_chunk: Callable[[bytes, float, list[float]], None]) -> None:
        self.cfg = cfg
        self.on_chunk = on_chunk
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._bands = BandAnalyser(cfg.sample_rate)
        self.muted = False

    async def start(self) -> None:
        if self._proc is not None:
            return
        argv = [*_PDEATHSIG, "pw-record", *_pw_common(self.cfg)]
        if self.cfg.input_target:
            argv += ["--target", self.cfg.input_target]
        argv.append("-")

        log.debug("mic: %s", " ".join(argv))
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self._task = asyncio.create_task(self._pump(), name="mic-pump")

    async def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        want = self.cfg.chunk_bytes
        try:
            while True:
                chunk = await self._proc.stdout.readexactly(want)
                if self.muted:
                    continue
                self.on_chunk(chunk, rms_level(chunk), self._bands.push(chunk))
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("mic pump died")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()


class Speaker:
    """Writes PCM to pw-play, and can drop everything still queued."""

    def __init__(
        self, cfg: Config, on_level: Callable[[float, list[float]], None] | None = None
    ) -> None:
        self.cfg = cfg
        self.on_level = on_level
        self._bands = BandAnalyser(cfg.sample_rate)
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        # When the audio handed to pw-play will actually finish coming out of
        # the speakers. The model streams its answer far faster than real time,
        # so "the last chunk arrived" and "the room is quiet again" can be many
        # seconds apart — and in that gap the assistant hears its own voice.
        self._play_until = 0.0

    @property
    def playing(self) -> bool:
        return asyncio.get_running_loop().time() < self._play_until

    @property
    def remaining(self) -> float:
        """Seconds of audio still queued to play."""
        return max(0.0, self._play_until - asyncio.get_running_loop().time())

    async def _spawn(self) -> asyncio.subprocess.Process:
        argv = [*_PDEATHSIG, "pw-play", *_pw_common(self.cfg)]
        if self.cfg.output_target:
            argv += ["--target", self.cfg.output_target]
        argv.append("-")
        log.debug("speaker: %s", " ".join(argv))
        return await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )

    async def start(self) -> None:
        async with self._lock:
            if self._proc is None:
                self._proc = await self._spawn()

    async def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        async with self._lock:
            if self._proc is None or self._proc.returncode is not None:
                self._proc = await self._spawn()
            stdin = self._proc.stdin
            assert stdin
            try:
                stdin.write(pcm)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # pw-play went away (device switch, suspend). Next write respawns it.
                log.warning("speaker pipe broke, will respawn")
                self._proc = None
                return

            now = asyncio.get_running_loop().time()
            seconds = len(pcm) / (self.cfg.sample_rate * self.cfg.channels * 2)
            self._play_until = max(self._play_until, now) + seconds
        if self.on_level:
            self.on_level(rms_level(pcm), self._bands.push(pcm))

    async def flush_now(self) -> None:
        """Drop audio that is already queued, by replacing the player.

        Closing the pipe and waiting would let the buffer drain — which is the
        opposite of what barge-in needs — so the old player is killed outright.
        """
        async with self._lock:
            proc, self._proc = self._proc, None
            self._play_until = 0.0
            if proc and proc.returncode is None:
                proc.kill()
        if self.on_level:
            self.on_level(0.0, [0.0, 0.0, 0.0, 0.0])

    async def stop(self) -> None:
        async with self._lock:
            proc, self._proc = self._proc, None
            self._play_until = 0.0
        if proc and proc.returncode is None:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()


async def _loopback(cfg: Config, seconds: float) -> int:
    """Hear yourself through the whole path, and watch the level meter.

    Play music into the same sink while this runs: with echo cancellation
    working, the meter should stay near zero when you are silent.
    """
    speaker = Speaker(cfg)
    await speaker.start()

    peak = 0.0
    last_bands = [0.0, 0.0, 0.0, 0.0]
    pending: list[bytes] = []

    def on_chunk(chunk: bytes, level: float, bands: list[float]) -> None:
        nonlocal peak, last_bands
        peak = max(peak, level)
        last_bands = bands
        pending.append(chunk)

    mic = Microphone(cfg, on_chunk)
    await mic.start()
    print(f"Говори {seconds:.0f} секунд — услышишь себя обратно.")

    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        await asyncio.sleep(0.05)
        while pending:
            await speaker.write(pending.pop(0))
        bars = int(peak * 40)
        spectrum = " ".join(f"{b:.2f}" for b in last_bands)
        print(
            f"\rуровень |{'#' * bars}{'.' * (40 - bars)}| {peak:.3f}  полосы {spectrum}",
            end="",
            flush=True,
        )
        peak = 0.0

    print()
    await mic.stop()
    await speaker.stop()
    return 0


async def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Audio path check for omavoice")
    parser.add_argument("--loopback", action="store_true", help="echo the microphone back to the speakers")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--input", default=None, help="PipeWire source name (e.g. the echo-cancel source)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from . import config

    cfg = config.load()
    if args.input:
        cfg.input_target = args.input
    if not args.loopback:
        parser.error("nothing to do: pass --loopback")
    return await _loopback(cfg, args.seconds)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
