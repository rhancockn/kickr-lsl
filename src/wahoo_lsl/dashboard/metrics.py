"""Pure CPS metric derivation and shared 5-minute sweep histories (no I/O, no Tk)."""
from __future__ import annotations

import math
from typing import Any, NamedTuple

from ..derive import DEFAULT_CIRCUMFERENCE_M
from ..derive import CpsDeriver as _CoreDeriver

WINDOW_S = 300.0


class CpsMetrics(NamedTuple):
    t_lsl: float
    power_w: int | None
    speed_kmh: float | None
    cadence_rpm: float | None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class CpsDeriver:
    """Turns consecutive CPS log samples into power plus the shared speed/cadence derivation."""

    def __init__(self, circumference_m: float = DEFAULT_CIRCUMFERENCE_M) -> None:
        self._core = _CoreDeriver(circumference_m)

    def reset(self) -> None:
        self._core.reset()

    def update(self, sample: dict) -> CpsMetrics | None:
        t, fields = sample.get("t_lsl"), sample.get("fields")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not isinstance(fields, dict):
            return None
        derived = self._core.update(t, fields)
        return CpsMetrics(float(t), _int_or_none(fields.get("power_w")), derived["speed_kmh"], derived["cadence_rpm"])


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
