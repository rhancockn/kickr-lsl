"""Target-power ramp for slewed FTMS targets. Pure: no I/O, no clocks."""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_SLEW_W_PER_S = 1000.0


@dataclass(frozen=True)
class Ramp:
    """Linear move from start_w to target_w at rate_w_per_s, starting at t0.

    The controller sends updates every interval_s (measured from its previous update, not from t0)
    and a final one at end_t; this class only describes the line.
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
