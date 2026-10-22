"""Derived CPS metrics shared by every output: log, LSL, and later outlets.

Pure: no I/O, no clocks. Speed and cadence come from successive cumulative
revolution counters and their last-event times (CPS spec), so every consumer
of a Sample sees the same values.
"""
from __future__ import annotations

import math
from typing import Any

DEFAULT_CIRCUMFERENCE_M = 2.105  # 700x25c
ZERO_AFTER_S = 3.0  # no new revolution for this long -> 0
WHEEL_REV_WRAP = 1 << 32
CRANK_REV_WRAP = 1 << 16
EVENT_T_WRAP = 1 << 16
WHEEL_TICKS_PER_S = 2048
CRANK_TICKS_PER_S = 1024


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _RevRate:
    """Revolutions per second from a cumulative counter and its last-event time."""

    def __init__(self, rev_wrap: int, ticks_per_s: int) -> None:
        self._rev_wrap = rev_wrap
        self._ticks_per_s = ticks_per_s
        self._last: tuple[int, int] | None = None
        self._last_change_t = 0.0
        self._rate: float | None = None

    def update(self, t: float, revs: Any, event_t: Any) -> float | None:
        revs, event_t = _int_or_none(revs), _int_or_none(event_t)
        if revs is None or event_t is None:
            self._last, self._rate = None, None
            return None
        if self._last is None:
            self._last, self._last_change_t = (revs, event_t), t
            return self._rate
        d_revs = (revs - self._last[0]) % self._rev_wrap
        d_ticks = (event_t - self._last[1]) % EVENT_T_WRAP
        if d_revs and d_ticks and t - self._last_change_t > EVENT_T_WRAP / self._ticks_per_s:
            # Event time has wrapped while idle: delta ticks are ambiguous, so only re-prime.
            self._last, self._last_change_t = (revs, event_t), t
        elif d_revs and d_ticks:
            self._rate = d_revs * self._ticks_per_s / d_ticks
            self._last, self._last_change_t = (revs, event_t), t
        elif t - self._last_change_t >= ZERO_AFTER_S:
            self._rate = 0.0
        return self._rate


class CpsDeriver:
    """Speed (km/h) and cadence (rpm) from consecutive parsed CP Measurement fields."""

    def __init__(self, circumference_m: float = DEFAULT_CIRCUMFERENCE_M) -> None:
        if isinstance(circumference_m, bool) or not (math.isfinite(circumference_m) and circumference_m > 0):
            raise ValueError(f"circumference_m must be a positive finite number, got {circumference_m!r}")
        self._circumference_m = float(circumference_m)
        self.reset()

    @property
    def circumference_m(self) -> float:
        return self._circumference_m

    def reset(self) -> None:
        self._wheel = _RevRate(WHEEL_REV_WRAP, WHEEL_TICKS_PER_S)
        self._crank = _RevRate(CRANK_REV_WRAP, CRANK_TICKS_PER_S)

    def update(self, t_lsl: float, fields: dict) -> dict[str, float | None]:
        wheel = self._wheel.update(t_lsl, fields.get("wheel_revs"), fields.get("wheel_event_t"))
        crank = self._crank.update(t_lsl, fields.get("crank_revs"), fields.get("crank_event_t"))
        return {
            "speed_kmh": None if wheel is None else wheel * self._circumference_m * 3.6,
            "cadence_rpm": None if crank is None else crank * 60.0,
        }
