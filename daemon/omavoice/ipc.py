"""The channel between the daemon and the QML panel.

A Unix socket carrying newline-delimited JSON, the same shape quickshell.spotify
uses: the UI sends commands, the daemon pushes state. Anything can connect —
the panel, the bar widget on each monitor, omavoice-ctl — and every
connection gets the full broadcast, so a second monitor's widget is never
showing stale state.

The daemon must never block on a slow reader, so a client whose queue backs up
is dropped rather than allowed to stall the audio loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger("omavoice.ipc")

CommandHandler = Callable[[dict], Awaitable[dict | None]]

# Deep enough to ride out a repaint, shallow enough that a wedged client is
# noticed within a second rather than growing without bound.
_QUEUE_DEPTH = 64


class _Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self.alive = True


class Server:
    def __init__(self, path: Path, on_command: CommandHandler) -> None:
        self.path = path
        self.on_command = on_command
        self._server: asyncio.Server | None = None
        self._clients: set[_Client] = set()
        # Replayed to every new connection so a panel that opens mid-answer
        # comes up showing what is actually happening.
        self._latest: dict[str, dict] = {}
        # The last stretch of the waterfall, replayed to a panel that opens
        # mid-conversation — coming back from the background to an empty log
        # would hide exactly the work you left it doing.
        self._recent_events: deque[dict] = deque(maxlen=14)

    async def start(self) -> None:
        # A stale socket from a killed daemon would make bind fail; there is no
        # one on the other end of it by definition.
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)
        log.info("listening on %s", self.path)

    async def stop(self) -> None:
        # Clients first. wait_closed() waits for every connection handler to
        # finish, and each one is parked on readline() — so closing the server
        # before its clients means waiting forever, and systemd eventually
        # SIGKILLs us with the socket file still on disk.
        for client in list(self._clients):
            client.alive = False
            with contextlib.suppress(Exception):
                client.writer.close()
        self._clients.clear()

        if self._server:
            self._server.close()
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            self._server = None

        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def forget(self, *kinds: str) -> None:
        """Drop replayed messages of these kinds.

        A panel that opens a new session should not be handed the previous
        conversation's answer just because a late-joining client would want it.
        """
        for kind in kinds:
            self._latest.pop(kind, None)
        if "event" in kinds:
            self._recent_events.clear()

    # -- outbound -----------------------------------------------------------

    def broadcast(self, message: dict) -> None:
        """Push a message to every connected UI. Never blocks, never raises."""
        kind = message.get("type")
        # Levels are a firehose and are meaningless once stale, so they are the
        # one message type not worth replaying to a late joiner.
        # Levels are meaningless once stale. Events are a stream, but a short
        # tail of them is worth keeping so a reopened panel has context.
        if kind == "event":
            self._recent_events.append(message)
        elif kind and kind != "level":
            self._latest[kind] = message

        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        for client in list(self._clients):
            if not client.alive:
                continue
            try:
                client.queue.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("dropping a client that stopped reading")
                client.alive = False
                with contextlib.suppress(Exception):
                    client.writer.close()

    # -- connections --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        self._clients.add(client)
        log.debug("client connected (%d total)", len(self._clients))

        for message in list(self._latest.values()) + list(self._recent_events):
            with contextlib.suppress(asyncio.QueueFull):
                client.queue.put_nowait((json.dumps(message, ensure_ascii=False) + "\n").encode())

        pump = asyncio.create_task(self._pump(client), name="ipc-pump")
        try:
            while client.alive:
                line = await reader.readline()
                if not line:
                    break
                await self._dispatch(line, client)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("client loop failed")
        finally:
            client.alive = False
            pump.cancel()
            self._clients.discard(client)
            with contextlib.suppress(Exception):
                writer.close()
            log.debug("client gone (%d left)", len(self._clients))

    async def _dispatch(self, line: bytes, client: _Client) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.warning("ignoring malformed line: %r", line[:80])
            return
        if not isinstance(message, dict):
            return

        reply = await self.on_command(message)
        if reply is None:
            return
        # A reply carries the request's id so a caller can correlate it, the
        # same convention the spotify plugin's backend uses.
        if message.get("id") is not None:
            reply.setdefault("id", message["id"])
        with contextlib.suppress(asyncio.QueueFull):
            client.queue.put_nowait((json.dumps(reply, ensure_ascii=False) + "\n").encode())

    async def _pump(self, client: _Client) -> None:
        try:
            while client.alive:
                payload = await client.queue.get()
                client.writer.write(payload)
                await client.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            client.alive = False
        except Exception:  # noqa: BLE001
            log.exception("ipc pump failed")
            client.alive = False


async def request(path: Path, message: dict, *, collect: str | None = None, timeout: float = 90.0) -> dict | None:
    """One-shot client, for omavoice-ctl.

    Sends a command and waits either for its correlated reply, or — when
    `collect` names a message type — for the next push of that type.
    """
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        message.setdefault("id", 1)
        writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await writer.drain()

        async def _wait() -> dict | None:
            while True:
                line = await reader.readline()
                if not line:
                    return None
                try:
                    reply = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(reply, dict):
                    continue
                if collect:
                    if reply.get("type") == collect:
                        return reply
                    continue
                if reply.get("id") == message["id"]:
                    return reply

        return await asyncio.wait_for(_wait(), timeout=timeout)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
