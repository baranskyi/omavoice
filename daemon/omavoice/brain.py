"""The brain: whatever actually answers the question.

The Realtime model is the voice and the ears. It knows how to hold a
conversation and nothing else — every question of fact goes through here, to a
local coding agent that can read this machine as well as the web.

Two backends, same contract. Both are asked to return JSON matching
schemas/answer.json, so the panel never has to parse prose:

    codex   `codex exec` on the ChatGPT subscription, sandboxed read-only.
            Native --output-schema, so the shape is enforced by the CLI.
    claude  `claude -p` with the schema pressed into the prompt. More
            integrations (skills, MCP), no schema enforcement, so we repair.

Conversation continuity is per-backend: the first ask of a session starts a
thread, later asks resume it, so "и что там во втором файле?" means something.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import ANSWER_SCHEMA, Config

log = logging.getLogger("omavoice.brain")

# Phrasings that only make sense as a literal command to run, not as a
# question. Voice adds its own transcription errors on top of however loosely
# a person phrases things, and the sandbox is read-only anyway — but a request
# whose whole content is "run this destructive thing" should not become a
# prompt at all. This is a guard against accident, not against an attacker:
# anyone at the keyboard can already run these directly.
_DESTRUCTIVE = re.compile(
    r"\b(rm\s+-[rf]|mkfs|dd\s+if=|shutdown|reboot|systemctl\s+(stop|disable)|"
    r"drop\s+(table|database)|git\s+push\s+--force|:\(\)\s*\{)",
    re.IGNORECASE,
)

_REFUSAL = ("I do not run commands by voice — I only read and report. "
            "Do that one yourself in a terminal.")

# How much a backend is allowed to say before we stop listening to it.
#
# This daemon runs for weeks; the agent it starts runs for a minute. That
# asymmetry is the whole problem: everything the short-lived process writes is
# held in the long-lived one, and then handed to the panel over the socket. A
# backend stuck in a retry loop, or one that decided the answer to a question
# was the contents of a log file, would otherwise choose how much memory the
# service uses and how much the panel is asked to draw.
#
# The numbers are set against what real work looks like. An answer is a few
# hundred bytes; codex's --json event stream is the only legitimately bulky
# thing here, and a full minute of it — the brain timeout — measures in tens of
# kilobytes. Four megabytes is not a long answer. It is a fault.
_MAX_STDOUT = 4 * 1024 * 1024
_MAX_STDERR = 256 * 1024
_MAX_ANSWER_FILE = 1024 * 1024
_MAX_SCHEMA_FILE = 64 * 1024

# And what survives into an Answer, which is what gets retained and broadcast.
_MAX_SPOKEN = 4000
_MAX_MARKDOWN = 64 * 1024
_MAX_ENTRIES = 24
_MAX_LABEL = 200
_MAX_VALUE = 2048
# Thread and session ids come back from the backend and go out again as
# command-line arguments on the next question, which is reason enough.
_MAX_THREAD_ID = 200

_CHUNK = 64 * 1024

# A single line of the agent's event stream. One `command_execution` event
# carries the command's whole output, so these are not small — but they are
# held only until the newline that ends them, and anything past this is
# dropped rather than accumulated.
_MAX_TRACE_LINE = 128 * 1024
# What survives into a line shown behind the waveform. It is meant to be
# half-read, not read.
_MAX_TRACE_TEXT = 180


def _clip(text: str, limit: int) -> str:
    """Cut a field to size.

    Silent on purpose. One oversized answer touches every field of every
    entry, and a warning apiece would put fifty lines in the journal — a
    fault that floods the log is the same fault we are bounding here, in a
    quieter place. `_coerce` says it once, for the answer as a whole.
    """
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


@dataclass
class Answer:
    """What the panel and the voice each get out of one question."""

    spoken: str
    markdown: str = ""
    links: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)

    @classmethod
    def error(cls, message: str) -> "Answer":
        return cls(spoken=message, markdown="")

    def as_ui_payload(self) -> dict:
        return {
            "type": "answer",
            "markdown": self.markdown,
            "links": self.links,
            "files": self.files,
        }


def _coerce(raw: str) -> Answer:
    """Turn whatever the agent said into an Answer, degrading rather than raising.

    A backend that ignored the schema still gave us prose worth speaking, so a
    parse failure becomes "speak the whole thing" instead of an error.
    """
    text = (raw or "").strip()
    if not text:
        return Answer.error("The agent returned nothing.")

    # claude -p often fences the JSON even when told not to.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        brace = text.find("{")
        if brace >= 0 and text.rstrip().endswith("}"):
            text = text[brace:]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Answer(spoken=_clip(raw.strip(), _MAX_SPOKEN), markdown="")

    if not isinstance(data, dict):
        return Answer(spoken=_clip(str(data), _MAX_SPOKEN))

    def _entries(key: str, second: str) -> list[dict]:
        raw_list = data.get(key)
        if not isinstance(raw_list, list):
            return []
        out = []
        # Both how many and how long. A list is a row of buttons in the
        # panel, and two dozen is already more than anyone reads; a label is
        # a few words and a path is a path.
        for item in raw_list[:_MAX_ENTRIES]:
            if isinstance(item, dict) and item.get("label") and item.get(second):
                out.append(
                    {
                        "label": _clip(str(item["label"]), _MAX_LABEL),
                        second: _clip(str(item[second]), _MAX_VALUE),
                    }
                )
        return out

    spoken = _clip(str(data.get("spoken") or "").strip(), _MAX_SPOKEN)
    markdown = _clip(str(data.get("markdown") or "").strip(), _MAX_MARKDOWN)
    if not spoken:
        # A backend that filled only the panel still owes the voice something.
        spoken = _clip(re.sub(r"[#*`>\-]", " ", markdown).strip() or "Done.", _MAX_SPOKEN)
    links = _entries("links", "url")
    files = _entries("files", "path")
    if len(text) > _MAX_MARKDOWN + _MAX_SPOKEN:
        log.warning("the agent returned %d bytes of answer — trimmed to fit", len(text))
    return Answer(spoken=spoken, markdown=markdown, links=links, files=files)


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Terminate, wait, and kill if it will not go.

    Asking politely and walking away is not stopping a process: an agent that
    ignores SIGTERM keeps running, and the caller has already forgotten about
    it. Nothing here raises — this runs on paths that are themselves cleaning
    up after a failure.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
        return
    except asyncio.TimeoutError:
        log.warning("agent ignored terminate, killing it")
    except asyncio.CancelledError:
        # Even while being cancelled, the child must not outlive us.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(proc.wait(), timeout=2)


# The name of the permission profile we hand codex. Ours, built fresh on
# every invocation and passed with -c, so the person's own ~/.codex/config.toml
# is never touched and their ChatGPT login keeps working.
_PROFILE = "omavoice"


def _codex_root() -> str:
    """The directory holding the codex binary.

    The profile is built by addition rather than subtraction — a `deny` beats a
    more specific `read`, so "everything except" cannot be expressed — and that
    means the sandbox starts with nothing and has to be told about the binary it
    is going to run. Left out, codex cannot exec itself: `bwrap: execvp ...: No
    such file or directory`, which reads as a broken agent rather than as a
    missing path.

    The containing directory and no more. This used to take the grandparent,
    which is the right answer for exactly one install layout — the nested
    `…/installs/codex/<version>/bin/codex` this machine happens to use — and a
    bad one everywhere else: `~/.local/bin/codex` would have granted the whole
    of `~/.local`, keyrings included, and `/usr/bin/codex` the whole of `/usr`.
    Granting only `…/bin` was tested against the nested layout and codex starts
    from it just as well.
    """
    found = shutil.which("codex")
    if not found:
        return ""
    return str(Path(found).resolve().parent)


def _codex_mcp_servers() -> list[str]:
    """The MCP servers this person has configured for codex, by name.

    There is no single switch for them. `mcp_servers={}` is accepted and
    silently ignored — codex drops unknown shapes rather than complaining — but
    naming each one and setting `enabled=false` does flip it to disabled. So
    they have to be enumerated, and the config file is a steadier place to read
    them from than the table `codex mcp list` prints.
    """
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    try:
        with (home / "config.toml").open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.debug("no codex config to read servers from: %s", exc)
        return []
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    return [name for name in servers if isinstance(name, str) and name]


def _toml_str(value: str) -> str:
    """One TOML basic string. A folder name is not a safe thing to paste."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_capped(path: Path, limit: int, what: str) -> str:
    """Read a file the agent wrote, refusing to read more than `limit`.

    Asking how big it is and then reading it are two facts about two
    different moments, and the agent is still running between them. So the
    ceiling is carried by the read itself: one byte past the limit is enough
    to know the file is wrong without holding the rest of it.

    Oversized is refused rather than trimmed. A truncated JSON document is
    not a smaller answer, it is a broken one, and `_coerce` would fall back
    to speaking the fragment aloud.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        log.warning("cannot read %s: %s", what, exc)
        return ""
    if len(data) > limit:
        log.warning("%s is over %d bytes — ignoring it", what, limit)
        return ""
    return data.decode(errors="replace")


class Brain:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.backend = cfg.backend if cfg.backend in ("codex", "claude") else "codex"
        # One thread per backend, so flipping the switch mid-conversation does
        # not try to resume a codex thread inside claude.
        self._threads: dict[str, str] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._on_trace: "Callable[[str], None] | None" = None

    # -- lifecycle ----------------------------------------------------------

    def set_backend(self, name: str) -> bool:
        if name not in ("codex", "claude"):
            return False
        if not shutil.which(name):
            log.warning("backend %s is not installed", name)
            return False
        self.backend = name
        return True

    def reset(self) -> None:
        """Forget conversation history. A new panel session starts clean."""
        self._threads.clear()

    def watch(self, on_trace: "Callable[[str], None] | None") -> None:
        """Be told what the agent is doing while it is doing it.

        There is a gap in this program between asking the agent something and
        hearing the answer, and for a long question it is twenty or thirty
        seconds of a panel that looks asleep. The agent is not asleep — it is
        narrating its plan and running commands, all of it already on stdout —
        and none of that ever reached anyone because the output was read to the
        end before being looked at.
        """
        self._on_trace = on_trace

    def _trace(self, text: str) -> None:
        if self._on_trace is None:
            return
        text = " ".join(text.split())
        if not text:
            return
        if len(text) > _MAX_TRACE_TEXT:
            text = text[:_MAX_TRACE_TEXT].rstrip() + " …"
        try:
            self._on_trace(text)
        except Exception:  # noqa: BLE001
            log.debug("trace sink failed", exc_info=True)

    def _codex_trace(self, line: str) -> None:
        """One line of `codex exec --json`, turned into one line worth seeing.

        Deliberately not everything: the token accounting and the thread ids
        say nothing to a person waiting. What does is the agent saying what it
        intends to do, and the commands it actually runs.
        """
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "agent_message" and event.get("type") == "item.completed":
            # Under --output-schema the agent talks to us in JSON, so the
            # message is `{"spoken": "I'll run ls…", "markdown": …}` rather
            # than a sentence. The sentence is in there; showing the envelope
            # instead would put punctuation on screen where the plan should be.
            text = str(item.get("text") or "")
            stripped = text.lstrip()
            if stripped.startswith("{"):
                try:
                    inner = json.loads(stripped)
                except json.JSONDecodeError:
                    inner = None
                if isinstance(inner, dict):
                    text = str(inner.get("spoken") or inner.get("markdown") or "")
            self._trace(text)
        elif kind == "command_execution":
            if event.get("type") == "item.started":
                self._trace("$ " + str(item.get("command") or ""))
            else:
                out = str(item.get("aggregated_output") or "").strip()
                first = out.splitlines()[0] if out else ""
                if first:
                    self._trace(first)
        elif kind == "reasoning":
            self._trace(str(item.get("text") or ""))

    def denial(self) -> str:
        """Why this backend may not be asked anything, or "" if it may.

        Both halves are the person's to decide and neither has a sensible
        default, so both are checked in one place — here — rather than being
        assumed anywhere that wants to ask a question. A backend that has not
        been permitted is not asked a smaller question; it is not asked.
        """
        if self.cfg.brain_cwd is None:
            return ("No folder has been chosen for me to work in yet. "
                    "Open the panel and pick one.")
        if self.backend not in self.cfg.consented:
            return (f"{self.backend} has not been allowed to answer by voice yet. "
                    "Open the panel and say yes.")
        return ""

    async def cancel(self) -> None:
        proc = self._proc
        if proc is not None:
            await _reap(proc)

    @property
    def busy(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # -- the one public call ------------------------------------------------

    async def ask(self, query: str) -> Answer:
        query = (query or "").strip()
        if not query:
            return Answer.error("I did not catch the question.")
        if _DESTRUCTIVE.search(query):
            log.warning("refusing destructive-sounding request: %s", query[:120])
            return Answer.error(_REFUSAL)

        if not shutil.which(self.backend):
            return Answer.error(f"The {self.backend} agent is not installed.")

        denial = self.denial()
        if denial:
            log.info("not asking %s: %s", self.backend, denial)
            return Answer.error(denial)

        try:
            if self.backend == "codex":
                return await self._ask_codex(query)
            return await self._ask_claude(query)
        except asyncio.TimeoutError:
            await self.cancel()
            return Answer.error("The agent is taking too long. Try a shorter question.")
        except Exception as exc:  # noqa: BLE001 - a dead brain must not kill the voice
            log.exception("brain failed")
            return Answer.error(f"The agent failed: {exc}")

    # -- backends -----------------------------------------------------------

    async def _drain(
        self,
        proc: asyncio.subprocess.Process,
        stream: asyncio.StreamReader,
        limit: int,
        what: str,
        on_line: "Callable[[str], None] | None" = None,
    ) -> bytes:
        """Read one pipe up to `limit` bytes, then end the process writing it.

        Reading in bounded chunks keeps our own memory in hand, but on its own
        it only moves the problem: a backend that keeps writing into a pipe
        nobody drains simply blocks, and the answer never comes. The limit is
        therefore a verdict, not a buffer size — past it the process is not
        producing an answer, and the way to stop paying for it is to end it.
        """
        buf = bytearray()
        # Only allocated when somebody is listening for lines. The whole point
        # of reading in blocks is that we are not obliged to look at them.
        pending = bytearray() if on_line is not None else None
        while len(buf) < limit:
            block = await stream.read(min(_CHUNK, limit - len(buf)))
            if not block:
                if pending:
                    self._offer(on_line, bytes(pending))
                return bytes(buf)
            buf += block
            if pending is not None:
                pending += block
                while True:
                    cut = pending.find(b"\n")
                    if cut < 0:
                        # A producer that never sends a newline must not be
                        # able to grow this without bound. The full bytes are
                        # still in `buf` under its own ceiling.
                        if len(pending) > _MAX_TRACE_LINE:
                            del pending[:]
                        break
                    line = bytes(pending[:cut])
                    del pending[: cut + 1]
                    self._offer(on_line, line)
        log.warning("agent wrote more than %d bytes to %s — stopping it", limit, what)
        await _reap(proc)
        return bytes(buf)

    @staticmethod
    def _offer(on_line, raw: bytes) -> None:
        """Hand one line to the watcher, and never let it break the answer.

        This runs on the path that is reading the agent's output. A watcher
        that raises here would abort the read, which would cost the person the
        answer they are waiting for in exchange for a decoration.
        """
        if not raw or len(raw) > _MAX_TRACE_LINE:
            return
        try:
            on_line(raw.decode(errors="replace"))
        except Exception:  # noqa: BLE001
            log.debug("trace watcher failed", exc_info=True)

    async def _gather(
        self,
        proc: asyncio.subprocess.Process,
        stdin: bytes | None,
        on_line: "Callable[[str], None] | None" = None,
    ) -> tuple[bytes, bytes]:
        """What `communicate()` does, with a ceiling on each pipe.

        Both pipes have to be read at once — a child that fills stderr while we
        are reading stdout deadlocks otherwise — and whichever one trips its
        limit ends the process, which gives the other one its EOF.
        """
        if proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                if stdin:
                    proc.stdin.write(stdin)
                    await proc.stdin.drain()
                proc.stdin.close()
        assert proc.stdout is not None and proc.stderr is not None
        out, err = await asyncio.gather(
            self._drain(proc, proc.stdout, _MAX_STDOUT, "stdout", on_line),
            self._drain(proc, proc.stderr, _MAX_STDERR, "stderr"),
        )
        await proc.wait()
        return out, err

    async def _run(
        self,
        argv: list[str],
        stdin: bytes | None = None,
        on_line: "Callable[[str], None] | None" = None,
    ) -> tuple[int, str, str]:
        log.debug("running %s", " ".join(argv[:6]))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Where the agent starts, which for claude is also what it takes as
            # its project. The daemon is started by systemd and inherits a
            # working directory nobody chose; leaving that in place would make
            # the scope an accident of how the service happened to be launched.
            cwd=str(self.cfg.brain_cwd) if self.cfg.brain_cwd else None,
        )
        self._proc = proc
        try:
            out, err = await asyncio.wait_for(
                self._gather(proc, stdin, on_line), timeout=self.cfg.brain_timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Both paths used to leave the agent running. `communicate()` being
            # cancelled does not touch the child, and the old `finally` dropped
            # the handle before anything had a chance to kill it — so a timed
            # out or interrupted question left a codex or claude process behind,
            # untracked, still reading the filesystem and still being billed.
            await _reap(proc)
            raise
        finally:
            if self._proc is proc:
                self._proc = None
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def _ask_codex(self, query: str) -> Answer:
        out_file = self.cfg.state_dir / "codex-last.json"
        out_file.unlink(missing_ok=True)

        # Argument order matters and is not symmetric: `resume` is a
        # subcommand, and it rejects --sandbox and --cd outright, so those have
        # to come before it while the reporting flags come after. Getting this
        # wrong fails only on the SECOND question of a conversation — the first
        # has no thread to resume — which is exactly the kind of bug that looks
        # like "the agent randomly stops working".
        before = [
            # Pinned rather than inherited from ~/.codex/config.toml: this is a
            # voice loop, and a person waiting for an answer out loud notices
            # every second. Measured on this machine: low ≈ 9 s for a simple
            # question, none ≈ 12 s (it compensates with more tool calls), and
            # minimal is rejected by the model outright.
            "-c", "model_reasoning_effort=low",
            "--cd", str(self.cfg.brain_cwd),
        ]

        limits = self._codex_limits()
        if limits:
            # Deliberately no --sandbox here, and this is the whole trick.
            #
            # Passing that flag explicitly discards `default_permissions`: the
            # profile is silently dropped and the agent reads the machine
            # again. It cost a round of testing to find, because everything
            # still looks right — codex even prints "sandbox: read-only" while
            # ignoring the thing that was supposed to bound it.
            #
            # Losing the flag costs nothing. The profile grants `read` and
            # nothing else, so writing is refused inside the folder as well as
            # outside it ("Read-only file system"), and the sandbox has no
            # network. It is the stronger of the two, not a substitute.
            before += limits
        else:
            # Permitted mode: exactly the command line this had before any of
            # the scoping existed. Writes still refused, reads unbounded.
            before += ["--sandbox", "read-only"]
        after = [
            "--skip-git-repo-check",
            "--json",
            "--output-schema", str(ANSWER_SCHEMA),
            "-o", str(out_file),
        ]

        argv = ["codex", "exec", *before]
        thread = self._threads.get("codex")
        if thread:
            argv += ["resume", thread]
        argv += [*after, query]

        code, stdout, stderr = await self._run(argv, on_line=self._codex_trace)

        # thread.started only appears on the first turn; resumed turns keep the id.
        for line in stdout.splitlines():
            if '"thread.started"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("thread_id"):
                self._threads["codex"] = _clip(str(event["thread_id"]), _MAX_THREAD_ID)
                break

        if out_file.exists():
            answer = _read_capped(out_file, _MAX_ANSWER_FILE, "codex-last.json")
            if answer:
                return _coerce(answer)

        # No last-message file: fall back to the event stream, then give up.
        for line in reversed(stdout.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                return _coerce(str(item["text"]))

        log.error("codex exited %s: %s", code, stderr[-400:])
        return Answer.error("Codex returned no answer.")

    def _codex_limits(self) -> list[str]:
        """The flags that hold codex to the chosen folder, or none at all.

        Nothing here is a variation on the sandbox: `--sandbox read-only`
        governs writing, and under it codex reads whatever it likes. What
        bounds reading is the permission profile, and it is built here rather
        than written into the person's config so that their own codex — their
        model, their login, their settings — is left exactly as they set it up.

        When the second permission has been given, this returns nothing. Not a
        looser profile, not a wider list: the same command line the agent had
        before any of this existed. A permitted mode that quietly differs from
        what it replaced is a mode nobody can reason about.
        """
        if self.backend in self.cfg.unrestricted or self.cfg.brain_cwd is None:
            return []

        root = _codex_root()
        readable = [_toml_str(":minimal") + ' = "read"']
        if root:
            readable.append(_toml_str(root) + ' = "read"')
        readable.append(_toml_str(str(self.cfg.brain_cwd)) + ' = "read"')

        flags = [
            "-c", f"default_permissions={_toml_str(_PROFILE)}",
            "-c", f"permissions.{_PROFILE}.filesystem={{{', '.join(readable)}}}",
            # The model's own search tool, which runs at OpenAI rather than in
            # the sandbox and is therefore untouched by anything above.
            "-c", 'web_search="disabled"',
        ]
        for name in _codex_mcp_servers():
            flags += ["-c", f"mcp_servers.{name}.enabled=false"]
        return flags

    async def _ask_claude(self, query: str) -> Answer:
        # claude has no --output-schema, so the shape goes in the prompt and
        # _coerce cleans up whatever comes back.
        schema = _read_capped(ANSWER_SCHEMA, _MAX_SCHEMA_FILE, "the answer schema")
        if not schema:
            return Answer.error("The answer schema is missing.")
        prompt = (
            "Answer the user's question using your access to this machine and the web.\n"
            "Reply with EXACTLY one JSON object matching this schema — no markdown "
            "fence, no commentary. Write `spoken` and `markdown` in the same "
            "language as the question:\n"
            f"{schema}\n\n"
            f"Question: {query}"
        )

        # Plan mode is what keeps claude reading rather than editing, and its
        # file tools are held to the working directory — a path outside it
        # comes back as a request for permission, which in a non-interactive
        # run is nobody's to grant. The explicit denials are belt and braces:
        # plan mode phrases its refusal as a question, and a question with no
        # one to answer it is a weaker thing to rely on than a refusal.
        #
        # Note what is *not* denied. The connectors, the MCP servers and the
        # shell are all left alone, because that is exactly what the consent
        # screen asked about and exactly what was granted. A permission that
        # quietly withholds half of what it promised is worse than no
        # permission: the person stops being able to predict what they agreed
        # to.
        #
        # Order matters here for a reason that has nothing to do with meaning:
        # --disallowedTools is variadic, so it keeps eating arguments until it
        # meets another flag. Left at the end it swallowed the question itself,
        # and claude refused the run for having no prompt in it — which reaches
        # the person as "the agent is broken". So it is followed by an ordinary
        # flag, deliberately, and the question stays a positional argument.
        if self.backend in self.cfg.unrestricted:
            argv = [
                "claude", "-p",
                "--disallowedTools", "Write,Edit,MultiEdit,NotebookEdit",
                "--output-format", "json",
                "--permission-mode", "plan",
            ]
        else:
            # `dontAsk` denies anything that would otherwise have asked, and
            # that is what turns the working directory into an edge — but only
            # together with the flag below, which is the whole lesson here.
            #
            # `dontAsk` denies what would ASK. It does not deny what the person
            # has already allowed. Claude Code accumulates permissions per
            # project in ~/.claude.json, so in a directory somebody has been
            # working in for weeks there is nothing left to ask about, and the
            # mode denies nothing at all: a read of /tmp outside the workspace
            # came back with the file's contents and an empty
            # `permission_denials`. In a directory with no history the same
            # command was refused. The boundary was the person's own config,
            # not this argv.
            #
            # `--setting-sources ""` loads none of those files, so the run
            # starts with no accumulated permissions and the mode has something
            # to refuse. With it, on the very directory that failed before,
            # both the Read and the shell fallback come back denied — taken
            # from the envelope's `permission_denials`, not from asking the
            # model what it was allowed to do, which is how this was got wrong
            # the first time.
            #
            # It costs the shell: with nothing pre-allowed, Bash is refused
            # too. That is the price of the edge, and it is the right way
            # round — an unconfined shell makes any file scoping decorative.
            argv = [
                "claude", "-p",
                "--disallowedTools",
                "Write,Edit,MultiEdit,NotebookEdit,WebFetch,WebSearch,WebBrowser",
                "--strict-mcp-config",
                "--setting-sources", "",
                "--output-format", "json",
                "--permission-mode", "dontAsk",
            ]
        thread = self._threads.get("claude")
        if thread:
            argv += ["--resume", thread]

        code, stdout, stderr = await self._run(argv + [prompt])
        if code != 0 and not stdout.strip():
            log.error("claude exited %s: %s", code, stderr[-400:])
            return Answer.error("Claude returned no answer.")

        # `--output-format json` wraps the reply in an envelope carrying the
        # session id we need for the next turn.
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            return _coerce(stdout)

        if isinstance(envelope, dict):
            if envelope.get("session_id"):
                self._threads["claude"] = _clip(str(envelope["session_id"]), _MAX_THREAD_ID)
            return _coerce(str(envelope.get("result") or stdout))
        return _coerce(stdout)


async def _main() -> int:
    """`python -m omavoice.brain "вопрос"` — the brain on its own, no audio."""
    import argparse

    parser = argparse.ArgumentParser(description="Ask the local agent one question")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--backend", choices=("codex", "claude"), default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from . import config

    cfg = config.load()
    if args.backend:
        cfg.backend = args.backend
    brain = Brain(cfg)

    answer = await brain.ask(" ".join(args.query))
    print("--- spoken ---")
    print(answer.spoken)
    if answer.markdown:
        print("\n--- markdown ---")
        print(answer.markdown)
    for link in answer.links:
        print(f"\n[link] {link['label']} -> {link['url']}")
    for entry in answer.files:
        print(f"[file] {entry['label']} -> {entry['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
