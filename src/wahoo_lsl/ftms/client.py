"""Python client for the FTMS control service (spec §9.1). Standard library only.

Each call is one localhost round trip (well under a millisecond); BLE latency is hidden by
the service. Not thread-safe: use one KickrClient per thread. Closing the client (or the
process exiting) makes the service spin the trainer down to 0 W.
"""
from __future__ import annotations

import json
import socket
from typing import Any, NamedTuple

from . import DEFAULT_HOST, DEFAULT_PORT


class KickrError(Exception):
    """The service answered ok:false."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class TargetState(NamedTuple):
    requested_w: int
    confirmed_w: int | None
    pending: bool
    t_lsl: float | None  # pylsl.local_clock() time of the last confirmation


class PowerReading(NamedTuple):
    watts: int | None
    t_lsl: float | None  # pylsl.local_clock() time the measurement arrived
    age_s: float | None


class Status(NamedTuple):
    link: str
    link_epoch: int
    target: TargetState
    power: PowerReading
    last_error: str | None
    last_error_t_lsl: float | None


class KickrClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout_s: float = 2.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rb")
        self._closed = False

    def set_target_power(self, watts: int) -> int:
        """Request a target; returns the accepted (possibly clamped) value immediately."""
        return int(self._request({"cmd": "set", "watts": watts})["target_w"])

    def read(self) -> Status:
        r = self._request({"cmd": "read"})
        t, pw = r["target"], r["power"]
        return Status(
            link=r["link"],
            link_epoch=r["link_epoch"],
            target=TargetState(t["requested_w"], t["confirmed_w"], t["pending"], t["t_lsl"]),
            power=PowerReading(pw["watts"], pw["t_lsl"], pw["age_s"]),
            last_error=r.get("last_error"),
            last_error_t_lsl=r.get("last_error_t_lsl"),
        )

    def read_target_power(self) -> TargetState:
        return self.read().target

    def read_power(self) -> PowerReading:
        return self.read().power

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._file.close()
        self._sock.close()

    def __enter__(self) -> "KickrClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, obj: dict[str, Any]) -> dict[str, Any]:
        self._sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        line = self._file.readline()
        if not line:
            raise ConnectionError("the FTMS service closed the connection")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise KickrError(reply.get("error", "unknown"), reply.get("detail", ""))
        return reply
