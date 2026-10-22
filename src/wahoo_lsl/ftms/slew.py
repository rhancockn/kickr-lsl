"""Target-power ramp for slewed FTMS targets (spec: FTMS target slew). Pure: no I/O, no clocks."""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_SLEW_W_PER_S = 1000.0
_GRID_EPS = 1e-9  # a time on (or a rounding error below) an update grid point counts as that point


@dataclass(frozen=True)
class Ramp:
    """Linear move from start_w to target_w at rate_w_per_s, starting at t0.

    Updates are due at t0 + k * interval_s (k >= 1) and finally at end_t.
    """

    start_w: float
    target_w: int
    rate_w_per_s: float
    t0: float
    interval_s: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.rate_w_per_s) and self.rate_w_per_s > 0):
            raise ValueError(f"rate_w_per_s must be a finite number > 0, got {self.rate_w_per_s!r}")
        if not (math.isfinite(self.interval_s) and self.interval_s > 0):
            raise ValueError(f"interval_s must be a finite number > 0, got {self.interval_s!r}")

    @property
    def end_t(self) -> float:
        return self.t0 + abs(self.target_w - self.start_w) / self.rate_w_per_s

    def value_at(self, t: float) -> float:
        if t >= self.end_t:
            return float(self.target_w)
        if t <= self.t0:
            return float(self.start_w)
        step = self.rate_w_per_s * (t - self.t0)
        return self.start_w + step if self.target_w > self.start_w else self.start_w - step

    def next_update_at(self, after_t: float) -> float:
        """The first update time strictly after ``after_t``, capped at end_t."""
        k = max(1, math.floor((after_t - self.t0) / self.interval_s + _GRID_EPS) + 1)
        return min(self.t0 + k * self.interval_s, self.end_t)
