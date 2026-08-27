"""Where everything lives, and what can be overridden from the environment.

Deliberately flat: the daemon has one config object, read once at startup, and
nothing writes back to it. Live changes (the backend switch) travel over the IPC
socket instead, so there is never a question of which copy is authoritative.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ANSWER_SCHEMA = PACKAGE_DIR.parent / "schemas" / "answer.json"


# The voices the Realtime API offers, and — the part that actually matters
# here — which grammatical gender each one has to speak in. Russian marks
# gender on past-tense verbs, so a female voice saying "посмотрел" is not a
# stylistic wobble: it sounds like a different person mid-sentence.
VOICES: tuple[tuple[str, str, str], ...] = (
    ("marin", "female", "Марин"),
    ("coral", "female", "Корал"),
    ("shimmer", "female", "Шиммер"),
    ("sage", "female", "Сейдж"),
    ("cedar", "male", "Седар"),
    ("ash", "male", "Эш"),
    ("ballad", "male", "Баллад"),
    ("echo", "male", "Эхо"),
    ("verse", "male", " Верс"),
    ("alloy", "male", "Эллой"),
)

VOICE_GENDER = {name: gender for name, gender, _ in VOICES}


def gender_of(voice: str) -> str:
    """Grammatical gender to speak in. Unknown voices default to feminine,
    matching the default voice rather than guessing."""
    return VOICE_GENDER.get(voice, "female")


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "omavoice"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- audio -------------------------------------------------------------
    # 24 kHz mono PCM16 is the only PCM rate the Realtime API accepts, and
    # pw-record resamples to it for us, so this is not a knob worth turning.
    sample_rate: int = 24000
    channels: int = 1
    # 20 ms of audio = 480 frames = 960 bytes. Small enough that barge-in feels
    # immediate, large enough that we are not doing syscalls all day.
    chunk_ms: int = 20
    # Both ends must go through the echo canceller, and this is the part that
    # is easy to get half right: recording from echo-cancel-source while
    # playing to the default sink leaves the canceller with no reference
    # signal, so it subtracts nothing and the assistant answers its own voice.
    # Record from its source, play into its sink. Both, or neither works.
    input_target: str = field(
        default_factory=lambda: os.environ.get("OMAVOICE_INPUT", "echo-cancel-source")
    )
    output_target: str = field(
        default_factory=lambda: os.environ.get("OMAVOICE_OUTPUT", "omavoice_playback")
    )

    # --- realtime ----------------------------------------------------------
    api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("OMAVOICE_MODEL", "gpt-realtime-2.1-mini"))
    voice: str = field(default_factory=lambda: os.environ.get("OMAVOICE_VOICE", "marin"))
    transcription_model: str = field(
        default_factory=lambda: os.environ.get("OMAVOICE_TRANSCRIBE", "gpt-4o-transcribe")
    )
    silence_ms: int = 500
    # Higher than the 0.5 default: a built-in laptop mic hears keyboards, fans
    # and the room, and every false positive is the assistant answering nobody.
    vad_threshold: float = field(
        default_factory=lambda: float(os.environ.get("OMAVOICE_VAD", "0.82"))
    )
    # Microphone level below which audio is treated as room noise and replaced
    # with silence. Normal speech at arm's length sits far above this; fans,
    # keyboards and residual echo sit below. Raise it if the assistant still
    # answers nobody, lower it if it misses you when you speak quietly.
    gate_level: float = field(
        default_factory=lambda: float(os.environ.get("OMAVOICE_GATE", "0.055"))
    )

    # --- brain -------------------------------------------------------------
    backend: str = field(default_factory=lambda: os.environ.get("OMAVOICE_BACKEND", "codex"))
    brain_timeout: float = 60.0
    brain_cwd: Path = field(default_factory=Path.home)

    # --- plumbing ----------------------------------------------------------
    socket_path: Path = field(default_factory=lambda: _runtime_dir() / "omavoice.sock")
    state_dir: Path = field(default_factory=_state_dir)
    debug: bool = field(default_factory=lambda: _env_flag("OMAVOICE_DEBUG", False))

    @property
    def chunk_bytes(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000) * self.channels * 2


def env_file() -> Path:
    """Where the API key lives: the daemon's own config, mode 600.

    Not the shell's config and not a project .env — the key must not be readable
    by every process the user starts, and it must survive reinstalling the
    plugin.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "omavoice" / "env"


def save_api_key(key: str) -> None:
    """Write the key into the env file, preserving whatever else is in it."""
    path = env_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                lines.append(f"OPENAI_API_KEY={key}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"OPENAI_API_KEY={key}")

    # Written 600 from the start rather than chmod'ed after: for the moment
    # between creating and tightening it, the key would be world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def load() -> Config:
    cfg = Config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg
