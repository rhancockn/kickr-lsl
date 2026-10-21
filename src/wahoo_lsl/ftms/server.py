"""Localhost JSON-lines TCP endpoint (spec §8). One client at a time."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Protocol

from .controller import LinkDown

log = logging.getLogger(__name__)

MAX_LINE_BYTES = 4096
CLOSE_TIMEOUT_S = 5.0


class ControllerApi(Protocol):
    def request_target(self, watts: int) -> tuple[int, bool]: ...

    def snapshot(self) -> dict[str, Any]: ...

    async def spin_down(self, reason: str) -> bool: ...


def error_reply(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}


class ControlServer:
    def __init__(self, controller: ControllerApi, host: str, port: int) -> None:
        self._controller = controller
        self._host = host
        self._port = port
        self._server: asyncio.base_events.Server | None = None
        self._active: asyncio.StreamWriter | None = None
        self._closing = False

    async def bind(self) -> None:
        """Bind the port now (fails fast with OSError) but accept no clients yet."""
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, limit=MAX_LINE_BYTES, start_serving=False
        )

    async def start_serving(self) -> None:
        assert self._server is not None, "bind() first"
        await self._server.start_serving()
        log.info("Accepting client commands on %s:%d", self._host, self.port)

    @property
    def port(self) -> int:
        assert self._server is not None, "bind() first"
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        """Stop accepting, drop the active client (no handler spin-down: shutdown does it)."""
        self._closing = True
        if self._server is None:
            return
        self._server.close()
        if self._active is not None:
            self._active.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("TCP server did not close within %.1fs; continuing", CLOSE_TIMEOUT_S)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self._active is not None:
            log.warning("Rejecting second client %s: busy", peer)
            with suppress(ConnectionError):
                await _send(writer, error_reply("busy", "another client is connected"))
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            return
        self._active = writer
        log.info("Client connected: %s", peer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # line longer than the stream limit
                    await _send(writer, error_reply("bad_request", f"request line longer than {MAX_LINE_BYTES} bytes"))
                    break
                if not line:
                    break  # EOF
                if not line.strip():
                    continue
                await _send(writer, self._dispatch(line))
        except ConnectionError as e:
            log.info("Client %s connection error: %s", peer, e)
        except Exception:
            log.exception("Client handler error")
        finally:
            self._active = None
            writer.close()
            log.info("Client disconnected: %s", peer)
            # Spin down before awaiting wait_closed: Controller.spin_down zeroes requested_w
            # before its first await, so a reconnecting client's later set always wins.
            if not self._closing:
                await self._controller.spin_down("client disconnected")
            with suppress(ConnectionError):
                await writer.wait_closed()

    def _dispatch(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_reply("bad_request", "request is not valid JSON")
        if not isinstance(request, dict):
            return error_reply("bad_request", "request must be a JSON object")
        cmd = request.get("cmd")
        if cmd == "read":
            return {"ok": True, **self._controller.snapshot()}
        if cmd == "set":
            watts = request.get("watts")
            if not isinstance(watts, int) or isinstance(watts, bool) or watts < 0:
                return error_reply("invalid_watts", f"watts must be a non-negative integer, got {watts!r}")
            try:
                accepted, clamped = self._controller.request_target(watts)
            except LinkDown as e:
                return error_reply("link_down", str(e))
            return {"ok": True, "target_w": accepted, "clamped": clamped}
        return error_reply("bad_request", f"unknown cmd {cmd!r}; expected 'set' or 'read'")


async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(json.dumps(obj).encode("utf-8") + b"\n")
    await writer.drain()
