"""The only module that reads clocks.

t_lsl  = pylsl.local_clock(): liblsl monotonic steady clock, seconds. Sole timing reference.
t_wall = time.time_ns(): system realtime clock (UTC). Human reference only; may jump.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pylsl


def now_lsl() -> float:
    return pylsl.local_clock()


def now_wall_ns() -> int:
    return time.time_ns()


def wall_iso(ns: int) -> str:
    """Render a time.time_ns() value as ISO-8601 UTC, truncated to microseconds."""
    seconds, remainder = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=remainder // 1000)
    return dt.isoformat(timespec="microseconds")
