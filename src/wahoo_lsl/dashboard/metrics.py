"""Pure CPS metric derivation and shared 5-minute sweep histories (no I/O, no Tk)."""
from __future__ import annotations

import math
from typing import Any, NamedTuple

WINDOW_S = 300.0
ZERO_AFTER_S = 3.0
DEFAULT_CIRCUMFERENCE_M = 2.105  # 700x25c
WHEEL_REV_WRAP = 1 << 32
CRANK_REV_WRAP = 1 << 16
EVENT_T_WRAP = 1 << 16
WHEEL_TICKS_PER_S = 2048
CRANK_TICKS_PER_S = 1024


class CpsMetrics(NamedTuple):
    t_lsl: float
    power_w: int | None
    speed_kmh: float | None
    cadence_rpm: float | None


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
        if d_revs and d_ticks:
            self._rate = d_revs * self._ticks_per_s / d_ticks
            self._last, self._last_change_t = (revs, event_t), t
        elif t - self._last_change_t >= ZERO_AFTER_S:
            self._rate = 0.0
        return self._rate


class CpsDeriver:
    """Turns consecutive CPS log samples into power, speed (km/h) and cadence (rpm)."""

    def __init__(self, circumference_m: float = DEFAULT_CIRCUMFERENCE_M) -> None:
        if isinstance(circumference_m, bool) or not (math.isfinite(circumference_m) and circumference_m > 0):
            raise ValueError(f"circumference_m must be a positive finite number, got {circumference_m!r}")
        self._circumference_m = float(circumference_m)
        self.reset()

    def reset(self) -> None:
        self._wheel = _RevRate(WHEEL_REV_WRAP, WHEEL_TICKS_PER_S)
        self._crank = _RevRate(CRANK_REV_WRAP, CRANK_TICKS_PER_S)

    def update(self, sample: dict) -> CpsMetrics | None:
        t, fields = sample.get("t_lsl"), sample.get("fields")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not isinstance(fields, dict):
            return None
        wheel = self._wheel.update(t, fields.get("wheel_revs"), fields.get("wheel_event_t"))
        crank = self._crank.update(t, fields.get("crank_revs"), fields.get("crank_event_t"))
        return CpsMetrics(
            float(t),
            _int_or_none(fields.get("power_w")),
            None if wheel is None else wheel * self._circumference_m * 3.6,
            None if crank is None else crank * 60.0,
        )


class SweepClock:
    """The shared 5-minute cycle: every History on one clock clears together."""

    def __init__(self, t0: float, window_s: float = WINDOW_S) -> None:
        self.cycle_start = t0
        self.window_s = window_s
        self.cycle = 0

    def advance(self, t: float) -> bool:
        """Move to the cycle containing t; True if the cycle changed."""
        if t < self.cycle_start + self.window_s:
            return False
        n = math.floor((t - self.cycle_start) / self.window_s)
        self.cycle_start += n * self.window_s
        self.cycle += n
        return True


class History:
    """Points for one graph in the current cycle, as (seconds since cycle start, value)."""

    def __init__(self, clock: SweepClock) -> None:
        self._clock = clock
        self._cycle = clock.cycle
        self._points: list[tuple[float, float | None]] = []

    def add(self, t: float, value: float | None) -> None:
        self._clock.advance(t)
        self._sync()
        if t < self._clock.cycle_start:
            return  # late point from a cycle that has already been cleared
        self._points.append((t - self._clock.cycle_start, value))

    def points(self) -> list[tuple[float, float | None]]:
        self._sync()
        return list(self._points)

    def _sync(self) -> None:
        if self._cycle != self._clock.cycle:
            self._points.clear()
            self._cycle = self._clock.cycle
