"""omavoice-ctl — a terminal handle on the running daemon.

Everything here is a thin wrapper over one IPC command, so the daemon stays the
only place that knows how any of this works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import config, ipc


async def _send(message: dict, *, collect: str | None = None, timeout: float = 90.0) -> int:
    cfg = config.load()
    if not cfg.socket_path.exists():
        print(
            "Демон не запущен. Запусти: systemctl --user start omavoice",
            file=sys.stderr,
        )
        return 1
    try:
        reply = await ipc.request(cfg.socket_path, message, collect=collect, timeout=timeout)
    except (ConnectionRefusedError, FileNotFoundError):
        print("Демон не отвечает — сокет есть, но никто не слушает.", file=sys.stderr)
        return 1
    except asyncio.TimeoutError:
        print("Демон не ответил вовремя.", file=sys.stderr)
        return 1

    if reply is None:
        print("Демон закрыл соединение без ответа.", file=sys.stderr)
        return 1

    if reply.get("ok") is False:
        print(reply.get("error") or "Команда не выполнена.", file=sys.stderr)
        return 1
    return _render(reply)


def _render(reply: dict) -> int:
    if reply.get("spoken"):
        print(reply["spoken"])
        if reply.get("markdown"):
            print()
            print(reply["markdown"])
        for link in reply.get("links") or []:
            print(f"\n[ссылка] {link['label']} -> {link['url']}")
        for entry in reply.get("files") or []:
            print(f"[файл] {entry['label']} -> {entry['path']}")
        return 0

    printable = {k: v for k, v in reply.items() if k not in ("id", "ok")}
    if printable:
        print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="omavoice-ctl")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what the daemon is doing right now")
    sub.add_parser("start", help="open a voice session")
    sub.add_parser("stop", help="close the voice session")
    sub.add_parser("cancel", help="stop the answer in flight, keep the session")
    sub.add_parser("reset", help="forget this conversation and start a fresh one")
    sub.add_parser("background", help="keep the session working with the panel closed")
    sub.add_parser("foreground", help="resume listening")

    ask = sub.add_parser("ask", help="ask the local agent in text, no microphone")
    ask.add_argument("query", nargs="+")

    backend = sub.add_parser("backend", help="switch the local agent")
    backend.add_argument("name", choices=("codex", "claude"))

    say = sub.add_parser("say", help="make the assistant speak a line (echo testing)")
    say.add_argument("text", nargs="+")

    voice = sub.add_parser("voice", help="pick the voice (and with it, the grammatical gender)")
    voice.add_argument("name", nargs="?", help="omit to list what is available")

    sub.add_parser("access", help="the folder in use and which agents are allowed")

    workspace = sub.add_parser("workspace", help="the folder the agent works in")
    workspace.add_argument("folder", help="an existing directory")

    allow = sub.add_parser("allow", help="let an agent answer by voice")
    allow.add_argument("name", choices=("codex", "claude"))

    revoke = sub.add_parser("revoke", help="withdraw an agent's permission")
    revoke.add_argument("name", choices=("codex", "claude"))

    unrestrict = sub.add_parser(
        "unrestrict", help="let an agent use everything it can, not only the folder"
    )
    unrestrict.add_argument("name", choices=("codex", "claude"))

    restrict = sub.add_parser("restrict", help="hold an agent to the chosen folder again")
    restrict.add_argument("name", choices=("codex", "claude"))

    args = parser.parse_args()

    if args.command == "ask":
        return asyncio.run(_send({"cmd": "ask", "query": " ".join(args.query)}, timeout=180))
    if args.command == "backend":
        return asyncio.run(_send({"cmd": "backend", "value": args.name}))
    if args.command == "say":
        return asyncio.run(_send({"cmd": "say", "text": " ".join(args.text)}))
    if args.command == "workspace":
        return asyncio.run(_send({"cmd": "workspace", "value": args.folder}))
    if args.command == "allow":
        return asyncio.run(_send({"cmd": "consent", "backend": args.name, "granted": True}))
    if args.command == "revoke":
        return asyncio.run(_send({"cmd": "consent", "backend": args.name, "granted": False}))
    if args.command == "unrestrict":
        return asyncio.run(_send({"cmd": "unrestrict", "backend": args.name, "granted": True}))
    if args.command == "restrict":
        return asyncio.run(_send({"cmd": "unrestrict", "backend": args.name, "granted": False}))
    if args.command == "voice":
        if not args.name:
            from .config import VOICES

            for name, gender, label in VOICES:
                mark = "женский" if gender == "female" else "мужской"
                print(f"  {name:<9} {mark:<8} {label}")
            return 0
        return asyncio.run(_send({"cmd": "voice", "value": args.name}))
    return asyncio.run(_send({"cmd": args.command}))


if __name__ == "__main__":
    raise SystemExit(main())
