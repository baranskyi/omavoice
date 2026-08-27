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
import re
import shutil
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
        return Answer(spoken=raw.strip(), markdown="")

    if not isinstance(data, dict):
        return Answer(spoken=str(data))

    def _entries(key: str, second: str) -> list[dict]:
        out = []
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("label") and item.get(second):
                out.append({"label": str(item["label"]), second: str(item[second])})
        return out

    spoken = str(data.get("spoken") or "").strip()
    markdown = str(data.get("markdown") or "").strip()
    if not spoken:
        # A backend that filled only the panel still owes the voice something.
        spoken = re.sub(r"[#*`>\-]", " ", markdown).strip() or "Done."
    return Answer(spoken=spoken, markdown=markdown, links=_entries("links", "url"), files=_entries("files", "path"))


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


class Brain:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.backend = cfg.backend if cfg.backend in ("codex", "claude") else "codex"
        # One thread per backend, so flipping the switch mid-conversation does
        # not try to resume a codex thread inside claude.
        self._threads: dict[str, str] = {}
        self._proc: asyncio.subprocess.Process | None = None

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

    async def _run(self, argv: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
        log.debug("running %s", " ".join(argv[:6]))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=self.cfg.brain_timeout)
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
            "--sandbox", "read-only",
            "--cd", str(self.cfg.brain_cwd),
        ]
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

        code, stdout, stderr = await self._run(argv)

        # thread.started only appears on the first turn; resumed turns keep the id.
        for line in stdout.splitlines():
            if '"thread.started"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("thread_id"):
                self._threads["codex"] = str(event["thread_id"])
                break

        if out_file.exists():
            return _coerce(out_file.read_text())

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

    async def _ask_claude(self, query: str) -> Answer:
        # claude has no --output-schema, so the shape goes in the prompt and
        # _coerce cleans up whatever comes back.
        schema = ANSWER_SCHEMA.read_text()
        prompt = (
            "Answer the user's question using your access to this machine and the web.\n"
            "Reply with EXACTLY one JSON object matching this schema — no markdown "
            "fence, no commentary. Write `spoken` and `markdown` in the same "
            "language as the question:\n"
            f"{schema}\n\n"
            f"Question: {query}"
        )

        argv = ["claude", "-p", "--output-format", "json", "--permission-mode", "plan"]
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
                self._threads["claude"] = str(envelope["session_id"])
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
