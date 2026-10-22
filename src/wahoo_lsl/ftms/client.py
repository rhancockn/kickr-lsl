"""Python client for the FTMS control service (spec §9.1). Standard library only.

Each call is one localhost round trip (well under a millisecond); BLE latency is hidden by
the service. Use one KickrClient per process (the service accepts a single client); it is
not thread-safe. Any failure mid-call (timeout, interrupt, malformed reply) closes the client:
later calls raise ConnectionError without sending. Closing the client (or the process
exiting) makes the service spin the trainer down to 0 W.
The slew rate set with set_slew_rate also resets to the service default when the client disconnects.
"""
from __future__ import annotations

import json
import math
import numbers
import operator
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
    # The last two fields have defaults: constructing with the old 4 fields and attribute access keep
    # working; unpacking into 4 names does not.
    requested_w: int
    confirmed_w: int | None
    pending: bool
    t_lsl: float | None  # pylsl.local_clock() time of the last confirmation
    setpoint_w: int | None = None  # current ramp value (= requested_w when not ramping); None from older services
    ramp_end_t_lsl: float | None = None  # when the running ramp reaches requested_w; None when not ramping


class SlewState(NamedTuple):
    w_per_s: float  # 0 = targets are sent directly
    update_interval_s: float


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
    slew: SlewState | None = None  # None from services without slew



def _whole_watts(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("watts must be a number, not bool")
    try:
        return operator.index(value)  # int and integer types such as numpy.int64
    except TypeError:
        pass
    if not isinstance(value, numbers.Real):
        raise TypeError(f"watts must be a real number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"watts must be finite, got {value!r}")
    return round(value)


def _rate(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("w_per_s must be a number, not bool")
    if not isinstance(value, numbers.Real):
        raise TypeError(f"w_per_s must be a real number, got {type(value).__name__}")
    rate = float(value)
    if not math.isfinite(rate):
        raise ValueError(f"w_per_s must be finite, got {value!r}")
    return rate

class KickrClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout_s: float = 2.0) -> None:
        self._closed = True  # until fully constructed
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._file = self._sock.makefile("rb")
        except BaseException:
            self._sock.close()
            raise
        self._closed = False

    def set_target_power(self, watts: float) -> int:
        """Request a target; returns the accepted (rounded, possibly clamped) value immediately.

        Any real number is accepted (int, float, numpy scalars) and rounded to whole watts.
        bool, non-numbers (TypeError) and NaN/infinity (ValueError) are rejected without sending.
        """
        return self._request({"cmd": "set", "watts": _whole_watts(watts)}, lambda r: int(r["target_w"]))

    def set_slew_rate(self, w_per_s: float) -> float:
        """Set this connection's slew rate in W/s (0 = targets go straight to the trainer).

        Returns the accepted rate. Resets to the service default when the client disconnects.
        bool, non-numbers (TypeError) and NaN/infinity (ValueError) are rejected without sending;
        the service rejects rates outside 0..1000 with KickrError("invalid_slew").
        """
        return self._request({"cmd": "slew", "w_per_s": _rate(w_per_s)}, lambda r: float(r["slew_w_per_s"]))

    def read(self) -> Status:
        return self._request({"cmd": "read"}, _parse_status)

    def read_target_power(self) -> TargetState:
        return self.read().target

    def read_power(self) -> PowerReading:
        return self.read().power

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.close()
        finally:
            self._sock.close()

    def __enter__(self) -> "KickrClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, obj: dict[str, Any], parse: Any) -> Any:
        if self._closed:
            raise ConnectionError("client is closed")
        try:
            self._sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")
            line = self._file.readline()
            if not line:
                raise ConnectionError("the FTMS service closed the connection")
            reply = json.loads(line)
            if not reply.get("ok"):
                raise KickrError(reply.get("error", "unknown"), reply.get("detail", ""))
            return parse(reply)
        except KickrError:
            raise  # a well-formed error reply; the connection stays in sync
        except BaseException:
            self.close()  # unknown stream state: never reuse it (the service spins down)
            raise


def _parse_status(r: dict[str, Any]) -> Status:
    t, pw, sl = r["target"], r["power"], r.get("slew")
    return Status(
        link=r["link"],
        link_epoch=r["link_epoch"],
        target=TargetState(
            t["requested_w"], t["confirmed_w"], t["pending"], t["t_lsl"], t.get("setpoint_w"), t.get("ramp_end_t_lsl")
        ),
        power=PowerReading(pw["watts"], pw["t_lsl"], pw["age_s"]),
        last_error=r.get("last_error"),
        last_error_t_lsl=r.get("last_error_t_lsl"),
        slew=None if sl is None else SlewState(sl["w_per_s"], sl["update_interval_s"]),
    )
