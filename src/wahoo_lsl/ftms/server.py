"""JSON-lines TCP endpoint (spec §8). One client at a time.

Loopback clients are always accepted. With a non-empty `allowed` list the server listens on the
LAN and also accepts clients whose address matches a listed IP or DNS name (see allow.py);
others get a `not_allowed` error and are closed without disturbing the active session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Protocol

from .allow import CachedResolver, Resolver, is_allowed, resolve_host
from .controller import LinkDown
from .slew import MAX_SLEW_W_PER_S

log = logging.getLogger(__name__)

MAX_LINE_BYTES = 4096
CLOSE_TIMEOUT_S = 5.0
REFUSAL_WARN_INTERVAL_S = 60.0


class ControllerApi(Protocol):
    def request_target(self, watts: int) -> tuple[int, bool]: ...

    def snapshot(self) -> dict[str, Any]: ...

    def set_slew_rate(self, w_per_s: float) -> float: ...

    def reset_slew(self) -> None: ...

    async def spin_down(self, reason: str) -> bool: ...


def peer_ip(writer: asyncio.StreamWriter) -> str:
    peername = writer.get_extra_info("peername")
    return peername[0] if peername else ""


def error_reply(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}


class ControlServer:
    def __init__(
        self,
        controller: ControllerApi,
        host: str,
        port: int,
        *,
        allowed: Sequence[str] = (),
        resolve: Resolver = resolve_host,
    ) -> None:
        self._controller = controller
        self._allowed = tuple(allowed)
        self._resolve = CachedResolver(resolve)
        self._refusal_warned: dict[str, float] = {}
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
        extra = f" (local + allowed: {', '.join(self._allowed)})" if self._allowed else ""
        log.info("Accepting client commands on %s:%d%s", self._host, self.port, extra)

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
        if self._allowed:
            ip = peer_ip(writer)
            if not await is_allowed(ip, self._allowed, self._resolve):
                self._log_refusal(ip)
                detail = f"this machine ({ip}) is not in ftms.allowed_hosts"
                with suppress(ConnectionError):
                    await _send(writer, error_reply("not_allowed", detail))
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                return
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
        spun_down = False  # set by an explicit disconnect so the finally path does not repeat it
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
                reply = await self._dispatch(line, writer)
                spun_down = reply.get("spun_down") is not None
                await _send(writer, reply)
                if spun_down:
                    break
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
            if not self._closing and not spun_down:
                self._controller.reset_slew()  # the rate belongs to the client that set it
                await self._controller.spin_down("client disconnected")
            with suppress(ConnectionError):
                await writer.wait_closed()

    def _log_refusal(self, ip: str) -> None:
        now = time.monotonic()
        quiet = now - self._refusal_warned.get(ip, -REFUSAL_WARN_INTERVAL_S) < REFUSAL_WARN_INTERVAL_S
        if not quiet:
            self._refusal_warned[ip] = now
        log.log(logging.DEBUG if quiet else logging.WARNING, "Refusing %s: not in allowed_hosts", ip or "unknown peer")

    async def _dispatch(self, line: bytes, writer: asyncio.StreamWriter) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_reply("bad_request", "request is not valid JSON")
        if not isinstance(request, dict):
            return error_reply("bad_request", "request must be a JSON object")
        cmd = request.get("cmd")
        if cmd == "read":
            return {"ok": True, **self._controller.snapshot()}
        if cmd == "connect":
            snap = self._controller.snapshot()
            return {"ok": True, "peer": peer_ip(writer), "link": snap["link"], "link_epoch": snap["link_epoch"]}
        if cmd == "disconnect":
            self._controller.reset_slew()
            return {"ok": True, "spun_down": await self._controller.spin_down("client disconnect")}
        if cmd == "set":
            watts = request.get("watts")
            if not isinstance(watts, int) or isinstance(watts, bool) or watts < 0:
                return error_reply("invalid_watts", f"watts must be a non-negative integer, got {watts!r}")
            try:
                accepted, clamped = self._controller.request_target(watts)
            except LinkDown as e:
                return error_reply("link_down", str(e))
            return {"ok": True, "target_w": accepted, "clamped": clamped}
        if cmd == "slew":
            rate = request.get("w_per_s")
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not 0 <= rate <= MAX_SLEW_W_PER_S  # also rejects NaN/inf; no float() so huge ints cannot overflow
            ):
                return error_reply(
                    "invalid_slew", f"w_per_s must be a number 0..{MAX_SLEW_W_PER_S:g}, got {rate!r}"
                )
            return {"ok": True, "slew_w_per_s": self._controller.set_slew_rate(rate)}
        return error_reply("bad_request", f"unknown cmd {cmd!r}; expected 'set', 'slew', 'read', 'connect' or 'disconnect'")


async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(json.dumps(obj).encode("utf-8") + b"\n")
    await writer.drain()
