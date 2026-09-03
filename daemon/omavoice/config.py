"""Where everything lives, and what can be overridden from the environment.

Deliberately flat: the daemon has one config object, read once at startup, and
nothing writes back to it. Live changes (the backend switch) travel over the IPC
socket instead, so there is never a question of which copy is authoritative.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("omavoice.config")

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


def _env_path(name: str) -> Path | None:
    """A directory named in the environment, if it is really there.

    A workspace that does not exist is worse than none: the agent would start
    in whatever the daemon's own working directory happens to be, which is the
    unbounded case this setting exists to prevent.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_gate() -> float | None:
    """OMAVOICE_GATE: a number pins the threshold, "auto" measures it."""
    raw = os.environ.get("OMAVOICE_GATE", "auto").strip().lower()
    if raw in ("", "auto"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


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
    # Empty means "decide per session" — see devices.py, which follows whatever
    # the system is using and switches the echo canceller in or out with it.
    # Naming a device here pins it, and then both ends must be named: recording
    # from echo-cancel-source while playing to the default sink leaves the
    # canceller with no reference signal, so it subtracts nothing and the
    # assistant answers its own voice. Both, or neither.
    input_target: str = field(default_factory=lambda: os.environ.get("OMAVOICE_INPUT", ""))
    output_target: str = field(default_factory=lambda: os.environ.get("OMAVOICE_OUTPUT", ""))

    # --- realtime ----------------------------------------------------------
    # Read from a file of its own rather than from the environment, and never
    # put back. An environment variable is not a secret on a shared UID: it is
    # inherited by every child, and /proc/<pid>/environ keeps the copy the
    # process started with for anything running as the same user to read —
    # including the agent this daemon spawns on every question. The key file
    # is mode 600 and is opened only here.
    api_key: str = field(default_factory=lambda: read_api_key())
    model: str = field(default_factory=lambda: os.environ.get("OMAVOICE_MODEL", "gpt-realtime-2.1-mini"))
    voice: str = field(default_factory=lambda: os.environ.get("OMAVOICE_VOICE", "marin"))
    transcription_model: str = field(
        default_factory=lambda: os.environ.get("OMAVOICE_TRANSCRIBE", "gpt-4o-transcribe")
    )
    # How long a pause has to last before the turn is considered finished.
    # 500 ms is what the API suggests and it is too eager for a person
    # composing a question out loud: "what is the weather in Malaga... in
    # Spain" was being cut after the fourth word and sent as a fragment.
    silence_ms: int = field(
        default_factory=lambda: int(os.environ.get("OMAVOICE_SILENCE_MS", "1100"))
    )
    # The API default. It used to be 0.82, on the theory that a laptop mic hears
    # keyboards and fans and every false positive is the assistant answering
    # nobody — but rejecting noise is the gate's job now, and it measures the
    # room rather than guessing at it. Left strict, this reads a pause between
    # words as the end of a sentence and commits half a question.
    vad_threshold: float = field(
        default_factory=lambda: float(os.environ.get("OMAVOICE_VAD", "0.5"))
    )
    # Microphone level below which audio is treated as room noise and replaced
    # with silence. "auto" — the default — measures the room's noise floor and
    # follows it, because a fixed number is only ever right for the microphone
    # it was picked on: a laptop mic at talking distance and a display mic
    # across the desk are an order of magnitude apart. Set a number to pin it.
    gate_level: float | None = field(default_factory=lambda: _env_gate())

    # --- brain -------------------------------------------------------------
    backend: str = field(default_factory=lambda: os.environ.get("OMAVOICE_BACKEND", "codex"))
    brain_timeout: float = 60.0
    # The folder this assistant works in — and, deliberately, the only one.
    #
    # It used to be the home directory, which was the wrong default in a way
    # that is easy to miss: the agent is the same one you type to, so it can
    # read whatever you can, and reaching it by voice makes a sentence spoken
    # near the machine enough to start that. A room can be overheard; a
    # transcription can be wrong. Naming a folder does not make either of those
    # go away, but it decides in advance how far a mistake can reach.
    #
    # None means nobody has chosen yet, and the agent is not asked anything at
    # all until they have. Not a default, because there is no folder this
    # program can pick that would be right — the person is the only one who
    # knows which of theirs is the safe one.
    brain_cwd: Path | None = field(default_factory=lambda: _env_path("OMAVOICE_WORKSPACE"))
    # Which backends have been told, in as many words, that they may use
    # everything they can already do — their tools, their MCP servers, their
    # connectors — on behalf of a voice. Empty means nobody has said so yet.
    #
    # Per-backend rather than one switch, because they are not the same
    # program and do not reach the same things: granting codex says nothing
    # about what claude has been connected to.
    consented: set[str] = field(default_factory=set)
    # And which of those have been let off the leash as well: allowed to use
    # everything they can normally do — their connectors, their MCP servers,
    # the web, and the rest of the machine — rather than only the folder.
    #
    # A second question rather than a bigger first one. The two are not the
    # same decision: "this agent may answer me" is about trusting the agent,
    # and "it may go anywhere" is about what a misheard sentence can reach.
    # Empty by default, so a fresh install is bounded until someone says
    # otherwise in as many words.
    unrestricted: set[str] = field(default_factory=set)

    # --- plumbing ----------------------------------------------------------
    socket_path: Path = field(default_factory=lambda: _runtime_dir() / "omavoice.sock")
    state_dir: Path = field(default_factory=_state_dir)
    # Three taps on the audio path, each writing raw PCM16 at `sample_rate`.
    # Together they answer "where did the sentence go" by elimination, which is
    # the only way it was ever going to be answered: every theory that skipped
    # this step turned out to be wrong.
    #
    #   OMAVOICE_MIC_DUMP    what pw-record delivered, before the gate
    #   OMAVOICE_DUMP        what was handed to the API, after the gate
    #   OMAVOICE_VOICE_DUMP  the assistant's own speech, straight off the API —
    #                        pristine, and therefore the reference material for
    #                        `python -m omavoice.probe`
    mic_dump: str = field(default_factory=lambda: os.environ.get("OMAVOICE_MIC_DUMP", ""))
    dump_path: str = field(default_factory=lambda: os.environ.get("OMAVOICE_DUMP", ""))
    voice_dump: str = field(default_factory=lambda: os.environ.get("OMAVOICE_VOICE_DUMP", ""))
    debug: bool = field(default_factory=lambda: _env_flag("OMAVOICE_DEBUG", False))

    @property
    def chunk_bytes(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000) * self.channels * 2


# --- files that outlive the process ------------------------------------------
#
# A pathname is a claim about the world at the moment it is resolved, and
# nothing keeps it true afterwards. The daemon runs on every login and rewrites
# the key file whenever someone saves one from the settings window; between
# deciding "the key is at ~/.config/omavoice/key" and truncating that name,
# anything running as this user can have made it a symlink to something else.
# So the directories are opened once, everything below works relative to those
# descriptors, the last component is never followed, and what is found there
# has to be a plain file of ours before a byte is read or written.
#
# The reads are bounded and the writes are all-or-nothing, for the same reason:
# a file that grew is not a file worth reading whole, and a half-written
# credential is worse than no credential at all.

# An OpenAI key is under two hundred characters; the longest project key seen
# so far is 164. Four kilobytes is twenty times that and still one small read.
KEY_MAX_BYTES = 4096
# Settings only, a handful of KEY=VALUE lines. 64 KiB is far more than anything
# setup.sh or a person editing by hand would ever put there.
ENV_MAX_BYTES = 64 * 1024


class UnsafePath(OSError):
    """What is at that name is not the plain file of ours we expected."""


def config_dir() -> Path:
    """The daemon's own config directory, holding the key and the settings.

    Not the shell's config and not a project .env — the key must not be
    readable by every process the user starts, and it must survive reinstalling
    the plugin.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "omavoice"


def env_file() -> Path:
    """Settings, which systemd does load into the daemon's environment."""
    return config_dir() / "env"


def key_file() -> Path:
    """Where the credential lives, alone.

    Deliberately not the env file: systemd loads that one into the daemon's
    environment, which is exactly what must not happen to a key.
    """
    return config_dir() / "key"


_dir_fds: dict[str, int] = {}


def _dir_fd(path: Path) -> int:
    """Open a directory once and keep it for the life of the process.

    Naming it is unavoidable exactly here, and only here. Every operation after
    this one is relative to the descriptor, so the name can be moved or
    replaced afterwards without any of it landing somewhere else.
    """
    cached = _dir_fds.get(str(path))
    if cached is not None:
        return cached
    path.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC because this daemon spawns codex and claude on every question,
    # and a handle on the directory holding the key is not theirs to inherit.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise UnsafePath(errno.EPERM, f"{path} belongs to another user")
    except BaseException:
        os.close(fd)
        raise
    _dir_fds[str(path)] = fd
    return fd


def config_dir_fd() -> int:
    return _dir_fd(config_dir())


def state_dir_fd() -> int:
    return _dir_fd(_state_dir())


def _checked_fd(dir_fd: int, name: str, *, flags: int, mode: int = 0o600) -> int:
    """Open one name inside an already-held directory, refusing surprises.

    O_NOFOLLOW covers the symlink; the fstat covers everything O_NOFOLLOW does
    not, which is most of it. O_NONBLOCK is not an optimisation — without it,
    opening a fifo planted at this name would simply hang until someone fed it.
    """
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, mode, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(errno.EINVAL, f"{name} is not a regular file")
        if st.st_uid != os.getuid():
            raise UnsafePath(errno.EPERM, f"{name} belongs to uid {st.st_uid}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_private(dir_fd: int, name: str, limit: int) -> str | None:
    """At most `limit` bytes of a plain file of ours, or None.

    None for every way this can go wrong — absent, a symlink, a fifo, someone
    else's, too big. A daemon that will not start because its settings file
    looks odd is a worse outcome than one that starts and says so.
    """
    try:
        fd = _checked_fd(dir_fd, name, flags=os.O_RDONLY)
    except FileNotFoundError:
        return None
    except UnsafePath as exc:
        log.warning("refusing to read %s: %s", name, exc.strerror)
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            log.warning("refusing to read %s: it is a symlink", name)
        else:
            log.warning("could not read %s: %s", name, exc)
        return None
    # One byte past the ceiling, so "exactly at the limit" and "far beyond it"
    # are told apart without trusting a size the file could have changed since.
    with os.fdopen(fd, "rb") as handle:
        blob = handle.read(limit + 1)
    if len(blob) > limit:
        log.warning("refusing to read %s: larger than %d bytes", name, limit)
        return None
    return blob.decode("utf-8", "replace")


def _mode_for(dir_fd: int, name: str, mode: int) -> int:
    """The mode the replacement should carry, and a look at what it replaces.

    Never wider than asked for and never wider than what is there now, so a
    rewrite cannot loosen a key file. Owner-read is the floor: a credential we
    can no longer read back is a credential lost.
    """
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return mode
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath(errno.EINVAL, f"{name} is not a regular file")
    if st.st_uid != os.getuid():
        raise UnsafePath(errno.EPERM, f"{name} belongs to uid {st.st_uid}")
    return (stat.S_IMODE(st.st_mode) & mode) | 0o400


def write_private(dir_fd: int, name: str, text: str, *, mode: int = 0o600) -> None:
    """Put new contents there, or leave the old ones exactly as they were.

    Written to a temporary file beside it and renamed over the top, because the
    alternative — truncate, then write — has a window in which the file holds
    neither the old value nor the new one. A power cut in that window used to
    mean an empty key file and an assistant that had forgotten its credential.
    """
    mode = _mode_for(dir_fd, name, mode)
    tmp = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    # O_EXCL, so this cannot be talked into writing through a name someone else
    # put there first; 600 from the moment it exists rather than chmod'ed after.
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=dir_fd,
    )
    try:
        handle = os.fdopen(fd, "w")
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    try:
        # Exact, whatever the umask happens to be.
        os.fchmod(handle.fileno(), mode)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        handle.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    # The rename has to reach the disk too, or a crash can leave the directory
    # still pointing at the old inode with the new one already unlinked.
    with contextlib.suppress(OSError):
        os.fsync(dir_fd)


def open_append(path: str | Path):
    """A plain file of ours, opened to append to, without following a symlink.

    For the audio taps. They are not secrets, but they carry the microphone
    verbatim, and the path comes from the environment — which means it can name
    something already there.
    """
    path = Path(path)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = _checked_fd(
            dir_fd,
            path.name,
            flags=os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            mode=0o600,
        )
    finally:
        os.close(dir_fd)
    return os.fdopen(fd, "ab", buffering=0)


def _config_fd() -> int | None:
    try:
        return config_dir_fd()
    except OSError as exc:
        log.warning("cannot use %s: %s", config_dir(), exc)
        return None


def read_api_key() -> str:
    """The key, from its file, with the older locations still honoured.

    Three of them, in order of how much they should be trusted:
      1. the key file, which nothing else reads;
      2. OPENAI_API_KEY in the environment, which is how it used to arrive and
         how someone running the daemon by hand may still pass it;
      3. the env file, where setup.sh used to put it — read directly here so an
         existing installation keeps working without the key going through
         systemd into the environment.
    """
    fd = _config_fd()
    if fd is not None:
        key = (read_private(fd, "key", KEY_MAX_BYTES) or "").strip()
        if key:
            return key

    from_env = os.environ.get("OPENAI_API_KEY", "").strip()
    if from_env:
        return from_env

    if fd is not None:
        for line in (read_private(fd, "env", ENV_MAX_BYTES) or "").splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\'\"")

    # Said out loud, and with the location, because the alternative is a daemon
    # that starts and then fails at connect time with something about a 401.
    log.warning("no API key yet — save one from the settings window, or put it in %s", key_file())
    return ""


def save_api_key(key: str) -> None:
    """Write the key to its own file, and take it out of the env file.

    Moving it is part of saving it: an installation that predates the split
    still has the key in the env file, which systemd loads into the daemon's
    environment. Leaving a copy there would mean the credential is protected
    only until someone reads /proc.
    """
    fd = config_dir_fd()
    write_private(fd, "key", key + "\n", mode=0o600)
    _strip_key_from_env_file(fd)


def _strip_key_from_env_file(dir_fd: int) -> None:
    """Remove any OPENAI_API_KEY line from the settings file."""
    text = read_private(dir_fd, "env", ENV_MAX_BYTES)
    if text is None:
        return
    lines = text.splitlines()
    kept = [line for line in lines if not line.strip().startswith("OPENAI_API_KEY=")]
    if len(kept) == len(lines):
        return
    write_private(dir_fd, "env", "\n".join(kept).rstrip("\n") + "\n", mode=0o600)


def load() -> Config:
    cfg = Config()
    try:
        # Opened here so the descriptor is held from startup, before anything
        # has had a chance to move the directory out from under us.
        state_dir_fd()
    except OSError as exc:
        log.warning("cannot use %s: %s — settings will not be remembered", cfg.state_dir, exc)
    return cfg
