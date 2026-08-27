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
import contextlib
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


def clipped_samples(pcm: bytes, ceiling: int = 32000) -> int:
    """How many samples in this chunk are pinned near full scale.

    Loud and clipped look the same in an RMS meter — both read high — but only
    one of them is destroying the waveform, and a clipped waveform transcribes
    into confident nonsense no threshold can rescue. Counting the pinned
    samples separates the two in one number.
    """
    return sum(1 for s in _samples_of(pcm) if s >= ceiling or s <= -ceiling)


def rms_full_scale(pcm: bytes) -> float:
    """RMS of a chunk against the real ceiling of PCM16, 0..1.

    Kept separate from `rms_level` on purpose. That one is normalised against
    `_FULL_SCALE`, a number chosen so the waveform on screen looks lively — it
    reaches 1.0 at a level that is merely loud, not clipped. Reading it as
    headroom cost half a day of wrong diagnoses. Anything reasoning about
    actual signal strength wants this one.
    """
    samples = _samples_of(pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


def rms_level(pcm: bytes) -> float:
    """Loudness of a PCM16 chunk on the display scale, 0..1 — for the waveform.

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


class AutoGain:
    """Brings a quiet microphone up to a level speech recognition can read.

    Measured, not assumed. The same sentence, recorded on this machine and fed
    to the API untouched, came back as one word; amplified eight times, as the
    whole question. Nothing about it was distorted — there simply was not
    enough signal for the model to work with:

        as captured   'Wut'              'the weather.'      'はい。'
        x8            'What is a man?'   'What is the...'    'I want to know.'

    A microphone built into a display sits an arm's length further away than
    the one in a laptop lid, and its output is quieter by roughly that much.
    Which one is in use changes when a cable is plugged in, so a fixed gain
    would be wrong again the moment the desk changes. This tracks how loud
    speech actually is and scales it towards a target.

    Deliberately not done with the system volume control: that is shared with
    every other program on the machine, and turning it up for the assistant
    turned it up for dictation too, where it clipped.
    """

    # Where speech should land. The reference recording that transcribes
    # perfectly sits around here, and the model tolerates a hot signal far
    # better than a faint one — amplifying past the point of some clipping
    # still recovered more words than leaving it quiet.
    TARGET = 0.25
    # Never quieter than it arrived, and never so loud that a burst of noise
    # becomes a shout.
    MIN_GAIN = 1.0
    MAX_GAIN = 16.0

    def __init__(self, chunk_ms: int = 20) -> None:
        self._speech_peak = 0.0
        self.gain = 1.0
        # Roughly a second and a half of speech to settle, and slow release so
        # the level does not pump between words.
        self._attack = 0.05
        self._release = 0.002

    def observe(self, level: float, speaking: bool) -> None:
        """Fold one chunk into the estimate. Only speech counts.

        `level` must be full-scale RMS (`rms_full_scale`), not the display
        scale — the two differ by 5.5x, and mixing them silently produces a
        gain of one.
        """
        if not speaking:
            return
        if level > self._speech_peak:
            self._speech_peak += (level - self._speech_peak) * self._attack
        else:
            self._speech_peak += (level - self._speech_peak) * self._release
        if self._speech_peak > 1e-4:
            wanted = self.TARGET / self._speech_peak
            self.gain = max(self.MIN_GAIN, min(self.MAX_GAIN, wanted))

    def apply(self, pcm: bytes) -> bytes:
        """Scale a chunk, clamping rather than wrapping at the edges."""
        if self.gain <= 1.001:
            return pcm
        samples = _samples_of(pcm)
        gain = self.gain
        out = array.array(
            "h", (max(-32768, min(32767, int(s * gain))) for s in samples)
        )
        return out.tobytes()

    def reset(self) -> None:
        self._speech_peak = 0.0
        self.gain = 1.0


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

    The threshold itself is measured rather than configured. A fixed number is
    only ever right for the microphone it was picked on: a laptop mic at
    talking distance and a display mic across the desk differ by more than an
    order of magnitude, and the echo canceller's noise suppression moves the
    floor again. Tuned for one, the same constant either answers the fan or
    swallows half a sentence — which is exactly what happened here.

    So the gate tracks the room's noise floor continuously and opens at a
    multiple of it. The floor falls quickly and rises slowly: speech should
    drag it up barely at all, while plugging in a different microphone should
    be forgotten within seconds. `OMAVOICE_GATE` still accepts a number for
    anyone who would rather pin it.
    """

    # Speech sits far above a room's floor — 20 dB is a whisper's worth of
    # margin. Opening at eight times the floor clears fans, keyboards and
    # residual echo while leaving a quiet voice comfortable room.
    OPEN_OVER_FLOOR = 8.0
    # Below this the input is silent enough that the ratio stops meaning
    # anything: without it, a digitally-clean source drives the threshold to
    # zero and every bit of noise counts as speech.
    FLOOR_MIN = 0.0004
    # And above this something is wrong — a loud room, a hot input — and a
    # ratio would lock the person out of their own microphone.
    OPEN_MAX = 0.05

    def __init__(self, threshold: float | None, hangover_ms: int = 700, chunk_ms: int = 20) -> None:
        # None means "measure it"; a number means the user pinned it.
        self.threshold = threshold
        self.release = max(1, int(hangover_ms / max(1, chunk_ms)))
        self._open = False
        self._countdown = 0
        self._floor = self.FLOOR_MIN

    @property
    def adaptive(self) -> bool:
        return self.threshold is None

    @property
    def noise_floor(self) -> float:
        return self._floor

    @property
    def opening_level(self) -> float:
        """The level speech has to reach right now to open the gate."""
        if self.threshold is not None:
            return self.threshold
        return min(self.OPEN_MAX, max(self.FLOOR_MIN, self._floor) * self.OPEN_OVER_FLOOR)

    def observe(self, level: float) -> None:
        """Fold one chunk into the noise-floor estimate.

        Asymmetric on purpose, in two ways.

        Downward it follows almost immediately, so a quieter microphone is
        adopted within a second or two. Upward it crawls, so a passing noise
        does not drag the threshold along with it.

        And upward only while the gate is shut. Nothing heard during speech may
        raise the floor: a slow climb is invisible over one sentence but adds
        up over a monologue, and the gate ends up cutting off the very person
        it opened for. Falling is always allowed — that can only make the gate
        more willing to listen.
        """
        if level < self._floor:
            self._floor += (level - self._floor) * 0.25
        elif not self._open:
            self._floor += (level - self._floor) * 0.0008
        self._floor = max(1e-5, self._floor)

    def step(self, level: float, threshold: float | None = None) -> bool:
        """True when this chunk should pass through."""
        if self.adaptive and threshold is None:
            self.observe(level)
        opening = self.opening_level if threshold is None else threshold
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
    """Reads fixed-size PCM chunks and hands them to a callback.

    Keeps `pw-record` alive rather than assuming it stays that way. A capture
    device can vanish under it mid-session — unplugging a dock, or a Bluetooth
    headset whose microphone node only exists while the card is in its headset
    profile and disappears when it flips back. Worse, a target that is not
    there *yet* makes `pw-record` exit at once: opening a headset microphone is
    itself what asks WirePlumber to switch the profile, so the first attempt can
    lose a race with the switch it triggered.

    That used to end the pump and nothing else. The session stayed open, the
    panel said "listening", the meter sat still, and the assistant was deaf with
    no line anywhere saying so — indistinguishable from a microphone that simply
    heard nothing. So the process is restarted, and every one of these events is
    on the record.
    """

    # Long enough to let a profile switch settle, short enough that the pause is
    # a hiccup rather than a gap in the conversation.
    RETRY_SECONDS = 0.6
    # A stream can fail without stopping. A Bluetooth headset whose HFP
    # transport is erroring — "SCO packet for unknown connection handle" in the
    # kernel log — leaves pw-record alive and delivering a tenth of real time,
    # which is silence to anyone listening and perfect health to a watchdog that
    # only checks for death.
    #
    # Measured as a gap between chunks rather than a rate over a window. A
    # window has to start somewhere, and one that spans a restart blames the new
    # process for the old one's silence — which is how a working microphone came
    # to be declared starving. Chunks arrive every 20 ms; two seconds without
    # one is a stall by any reading.
    SILENCE_TIMEOUT = 2.0
    # Starting is not the same as stalling, and deserves more room. A capture
    # opened right after the graph was rearranged — a Bluetooth profile switch,
    # a device released a moment ago — can take seconds to produce its first
    # frame, and killing it at two was how a perfectly good microphone kept
    # being declared dead.
    START_TIMEOUT = 6.0
    # Two different situations, two different patiences. A device that has been
    # working and then stumbles deserves several attempts — a dock waking up, a
    # profile switching. A device that has not produced a single chunk since the
    # session opened is not warming up, it is absent, and every retry is another
    # three seconds of a person talking to something that cannot hear.
    MAX_RETRIES = 8
    COLD_RETRIES = 2

    def __init__(self, cfg: Config, on_chunk: Callable[[bytes, float, list[float]], None]) -> None:
        self.cfg = cfg
        self.on_chunk = on_chunk
        # Set per session by the daemon; empty means the system default.
        self.target = cfg.input_target
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._bands = BandAnalyser(cfg.sample_rate)
        self.muted = False
        self._stopping = False
        # Chunks delivered since the last start, so "it died immediately" and
        # "it ran for a while and then died" are different lines in the log.
        self.chunks = 0
        # Where to go when the chosen device never produces anything. Deaf is
        # the worst possible end state: a working microphone of the wrong kind
        # still lets someone finish the sentence they started.
        self.fallback_target = ""
        # Told about faults worth putting on screen, not only in the log.
        self.on_fault: Callable[[str], None] | None = None
        # Every chunk ever delivered, so throughput can be measured against the
        # clock rather than inferred from the process still being alive.
        self.chunks_total = 0

    async def start(self) -> None:
        # `done()` matters as much as `is not None`. The pump task ends by
        # itself when it gives up on a device, and a finished task left in this
        # field turned every later start into a silent no-op: a session with no
        # capture at all, no log line, and a panel saying "listening".
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        # Per session, not per daemon: "has this device ever worked" has to mean
        # "since this conversation opened", or a microphone that worked an hour
        # ago buys an absent one the patience meant for a stumble.
        self.chunks_total = 0
        self._task = asyncio.create_task(self._run(), name="mic-pump")

    async def _spawn(self) -> asyncio.subprocess.Process | None:
        argv = [*_PDEATHSIG, "pw-record", *_pw_common(self.cfg)]
        if self.target:
            argv += ["--target", self.target]
        argv.append("-")
        log.debug("mic: %s", " ".join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                # Kept, not discarded. When pw-record refuses a target it says
                # so on stderr and exits — and throwing that away is how "the
                # microphone produced nothing" stayed a mystery through several
                # rounds of guessing.
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log.error("could not start pw-record: %s", exc)
            return None
        asyncio.create_task(self._drain_stderr(proc), name="mic-stderr")
        return proc

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            data = await proc.stderr.read()
        except (asyncio.CancelledError, ValueError):
            return
        text = data.decode(errors="replace").strip()
        if text:
            log.warning("pw-record said: %s", text[:300])

    async def _run(self) -> None:
        try:
            await self._loop()
        finally:
            # Whatever ended this — giving up, cancellation — the field must not
            # outlive the task that owns it.
            self._task = None

    async def _loop(self) -> None:
        attempts = 0
        while not self._stopping:
            proc = await self._spawn()
            if proc is None:
                return
            self._proc = proc
            delivered = await self._read(proc)
            self.chunks += delivered

            if self._stopping:
                return
            attempts = 0 if delivered else attempts + 1
            log.warning(
                "microphone stream ended after %d chunks (%d this session, "
                "pw-record exit=%s, target=%r) — restarting, attempt %d",
                delivered,
                self.chunks_total,
                proc.returncode,
                self.target or "system default",
                attempts,
            )
            # "Has it worked" means a second of audio, not a chunk. A device
            # that dribbles a handful of frames and stops is absent, and
            # counting those as evidence of health buys it the patience meant
            # for a device that was genuinely running.
            worked = self.chunks_total >= 1000 // max(1, self.cfg.chunk_ms)
            limit = self.MAX_RETRIES if worked else self.COLD_RETRIES
            if attempts >= limit:
                if self.fallback_target and self.target != self.fallback_target:
                    log.error(
                        "microphone %r never produced audio — falling back to %r",
                        self.target or "system default",
                        self.fallback_target,
                    )
                    self._fault(f"microphone unavailable, switched to {self.fallback_target}")
                    self.target = self.fallback_target
                    attempts = 0
                    continue
                log.error(
                    "microphone %r produced nothing in %d attempts — giving up. "
                    "The session is deaf; pick another microphone in settings.",
                    self.target or "system default",
                    attempts,
                )
                self._fault("no working microphone — pick one in settings")
                return
            # Backing off rather than hammering. Reopening a Bluetooth headset's
            # HFP transport is itself what destabilises it — the kernel starts
            # logging "SCO packet for unknown connection handle" — so a tight
            # retry loop turns a stumble into a wedged audio graph.
            await asyncio.sleep(self.RETRY_SECONDS * (2 ** min(attempts - 1, 4)))

    def _fault(self, message: str) -> None:
        if self.on_fault is None:
            return
        try:
            self.on_fault(message)
        except Exception:  # noqa: BLE001
            log.exception("microphone fault handler failed")

    async def _read(self, proc: asyncio.subprocess.Process) -> int:
        """Pump one process to exhaustion. Returns how many chunks it gave."""
        assert proc.stdout
        want = self.cfg.chunk_bytes
        delivered = 0
        try:
            while True:
                chunk = await asyncio.wait_for(
                    proc.stdout.readexactly(want),
                    timeout=self.START_TIMEOUT if delivered == 0 else self.SILENCE_TIMEOUT,
                )
                delivered += 1
                self.chunks_total += 1
                if delivered == 1:
                    log.info(
                        "microphone streaming: %s", self.target or "system default"
                    )
                if self.muted:
                    continue
                self.on_chunk(chunk, rms_level(chunk), self._bands.push(chunk))
        except asyncio.TimeoutError:
            log.warning(
                "microphone %r gave nothing for %.0fs after %d chunks — restarting",
                self.target or "system default",
                self.START_TIMEOUT if delivered == 0 else self.SILENCE_TIMEOUT,
                delivered,
            )
            if proc.returncode is None:
                proc.kill()
            return delivered
        except asyncio.IncompleteReadError:
            return delivered
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("mic pump died")
            return delivered

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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
        # Set per session by the daemon; empty means the system default.
        self.target = cfg.output_target
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
        if self.target:
            argv += ["--target", self.target]
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
