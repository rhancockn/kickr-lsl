# KICKR Test Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Tkinter test dashboard that drives FTMS target power through `KickrClient`, shows the read-back target and CPS power/speed/cadence (tailed from the logger's JSONL), plus a `wahoo-dashboard` wrapper that runs service, logger and dashboard as one session.

**Architecture:** New package `src/wahoo_lsl/dashboard/`. Pure logic (`metrics.py`, graph helpers) is unit-tested without Tk. Two daemon threads (`PowerWorker`, `LogFollower`) post `(kind, payload)` tuples to one `queue.Queue` that the Tk loop drains every 100 ms. `launcher.py` supervises three child processes with ordered start-up and shutdown.

**Tech Stack:** Python ≥3.10 stdlib (tkinter, threading, subprocess, socket, sysconfig), `pylsl.local_clock()`; pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-kickr-dashboard-design.md`

## Global Constraints

- **Do not modify any existing file under `src/` or any existing test file.** The only edits to existing files are one new line in `[project.scripts]` of `pyproject.toml` (Task 5) and a new README section (Task 6).
- No new dependencies. Allowed imports: stdlib, `pylsl`, and from this project only `wahoo_lsl.ftms` (`DEFAULT_HOST`, `DEFAULT_PORT`), `wahoo_lsl.ftms.client` (`KickrClient`, `KickrError`, `Status` and its parts), `wahoo_lsl.ftms.config` (`load_ftms_config`), `wahoo_lsl.config` (`ConfigError`).
- Python floor 3.10: every module starts with `from __future__ import annotations`; no 3.11+-only APIs.
- Environment: conda env `wahoo`. Test command: `conda run -n wahoo python -m pytest -q -W error`. The full suite (314 existing tests + new) must stay green; `-W error` means unclosed files/sockets/processes fail tests — always close them.
- Commit after each task; each message ends with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Style: match the existing package — module docstring, type hints, sparse comments.
- Constants (verbatim from spec): sweep window **300 s**; speed/cadence → 0 after **3 s** without new revolutions; default circumference **2.105 m**; log poll **0.25 s**; power poll **0.5 s**; reconnect retry **1 s**; client timeout **1 s**; UI pump **100 ms**; redraw **1 s**; stale after **15 s**; graph line breaks on x gaps **> 5 s**; launcher port wait **15 s**, CPS early-exit window **2 s**, shutdown grace **20 s**, kill after **5 s** more.

## Review Focus

1. **Logger restart / reconnect mid-session** — counters reset, so a naïve delta gives an absurd speed spike. Expect `cps_file` and `cps_event` to reset the deriver (Task 4, `test_new_log_file_or_cps_event_resets_deriver`).
2. **Dashboard opened while an old log already exists** — nothing from the old file is plotted, even when it ends mid-line (Task 2, `test_preexisting_file_starts_at_eof`, `test_preexisting_partial_line_is_discarded`).
3. **Service held by another client (`busy`)** — shown, retried, never hangs (Task 3, `test_busy_reply_drops_connection_and_retries`).
4. **Slider moved while the service is down** — that value is never sent later (Task 3, `test_pending_value_cleared_on_connect`).
5. **Second Ctrl-C during a slow shutdown** — remaining grace periods are skipped (Task 5, `test_hurry_skips_grace`).

## File Structure

```
src/wahoo_lsl/dashboard/__init__.py   package docstring only
src/wahoo_lsl/dashboard/metrics.py    CpsMetrics, CpsDeriver, SweepClock, History (pure)
src/wahoo_lsl/dashboard/logtail.py    LogFollower
src/wahoo_lsl/dashboard/power.py      PowerWorker
src/wahoo_lsl/dashboard/graph.py      segments(), nice_ceiling(), Graph (Tk canvas)
src/wahoo_lsl/dashboard/app.py        Dashboard window + main()
src/wahoo_lsl/dashboard/launcher.py   wahoo-dashboard wrapper
tests/test_dashboard_metrics.py
tests/test_dashboard_logtail.py
tests/test_dashboard_power.py
tests/test_dashboard_graph.py
tests/test_dashboard_app.py
tests/test_dashboard_launcher.py
tests/dashboard_fake_child.py         stand-in child process for launcher tests
```

Event kinds posted to the UI queue (shared vocabulary for Tasks 2–4):

| kind | payload | from |
|---|---|---|
| `cps_sample` | dict (a `"type":"sample"` record) | LogFollower |
| `cps_event` | dict (`"type":"event"`, kind `connected`/`disconnected`) | LogFollower |
| `cps_file` | str (file name) | LogFollower |
| `cps_status` | str | LogFollower |
| `cps_bad` | int (total bad lines so far) | LogFollower |
| `service_connected` | None | PowerWorker |
| `service_down` | str | PowerWorker |
| `status` | `wahoo_lsl.ftms.client.Status` | PowerWorker |
| `set_ok` | int (watts accepted) | PowerWorker |
| `set_error` | str | PowerWorker |
| `service_error` | str | PowerWorker |

---

### Task 1: Package and pure metrics

**Files:**
- Create: `src/wahoo_lsl/dashboard/__init__.py`
- Create: `src/wahoo_lsl/dashboard/metrics.py`
- Test: `tests/test_dashboard_metrics.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CpsMetrics(t_lsl, power_w, speed_kmh, cadence_rpm)`; `CpsDeriver(circumference_m=2.105)` with `update(sample: dict) -> CpsMetrics | None` and `reset()`; `SweepClock(t0, window_s=300.0)` with attributes `cycle_start`, `cycle`, `window_s` and `advance(t) -> bool`; `History(clock)` with `add(t, value)` and `points() -> list[tuple[float, float | None]]`; constants `WINDOW_S`, `ZERO_AFTER_S`, `DEFAULT_CIRCUMFERENCE_M`.

- [ ] **Step 1: Write the failing tests** — `tests/test_dashboard_metrics.py`:

```python
import math

import pytest

from wahoo_lsl.dashboard.metrics import (
    DEFAULT_CIRCUMFERENCE_M,
    CpsDeriver,
    CpsMetrics,
    History,
    SweepClock,
)


def sample(t, power=None, wheel=None, crank=None):
    fields = {}
    if power is not None:
        fields["power_w"] = power
    if wheel is not None:
        fields["wheel_revs"], fields["wheel_event_t"] = wheel
    if crank is not None:
        fields["crank_revs"], fields["crank_event_t"] = crank
    return {"type": "sample", "t_lsl": t, "fields": fields}


def test_first_sample_primes_only():
    d = CpsDeriver()
    assert d.update(sample(1.0, power=150, wheel=(10, 0), crank=(5, 0))) == CpsMetrics(1.0, 150, None, None)


def test_speed_and_cadence_from_deltas():
    d = CpsDeriver()
    d.update(sample(1.0, wheel=(10, 0), crank=(5, 0)))
    m = d.update(sample(2.0, power=200, wheel=(11, 2048), crank=(6, 1024)))
    assert m.power_w == 200
    assert m.speed_kmh == pytest.approx(DEFAULT_CIRCUMFERENCE_M * 3.6)  # 1 rev/s
    assert m.cadence_rpm == pytest.approx(60.0)


def test_custom_circumference():
    d = CpsDeriver(circumference_m=2.0)
    d.update(sample(1.0, wheel=(0, 0)))
    assert d.update(sample(2.0, wheel=(2, 2048))).speed_kmh == pytest.approx(2 * 2.0 * 3.6)


def test_counter_and_event_time_wrap():
    d = CpsDeriver()
    d.update(sample(1.0, wheel=(2**32 - 1, 65000), crank=(65535, 65000)))
    m = d.update(sample(2.0, wheel=(1, 1000), crank=(0, 488)))
    # wheel: 2 revs in (1000 - 65000) % 65536 = 1536 ticks of 1/2048 s
    assert m.speed_kmh == pytest.approx(2 * 2048 / 1536 * DEFAULT_CIRCUMFERENCE_M * 3.6)
    # crank: 1 rev in (488 - 65000) % 65536 = 1024 ticks of 1/1024 s
    assert m.cadence_rpm == pytest.approx(60.0)


def test_no_new_revolution_holds_then_zeroes_after_3s():
    d = CpsDeriver()
    d.update(sample(0.0, crank=(0, 0)))
    assert d.update(sample(1.0, crank=(1, 1024))).cadence_rpm == pytest.approx(60.0)
    assert d.update(sample(2.0, crank=(1, 1024))).cadence_rpm == pytest.approx(60.0)  # held
    assert d.update(sample(3.9, crank=(1, 1024))).cadence_rpm == pytest.approx(60.0)  # 2.9 s: held
    assert d.update(sample(4.0, crank=(1, 1024))).cadence_rpm == 0.0  # 3 s since the last change


def test_stationary_from_start_reads_zero_after_3s():
    d = CpsDeriver()
    d.update(sample(0.0, wheel=(0, 0)))
    assert d.update(sample(2.0, wheel=(0, 0))).speed_kmh is None
    assert d.update(sample(3.0, wheel=(0, 0))).speed_kmh == 0.0


def test_revs_without_event_time_change_is_not_an_event():
    d = CpsDeriver()
    d.update(sample(0.0, crank=(0, 100)))
    assert d.update(sample(1.0, crank=(3, 100))).cadence_rpm is None


def test_missing_fields_give_none_and_reprime():
    d = CpsDeriver()
    d.update(sample(0.0, wheel=(0, 0)))
    m = d.update(sample(1.0, power=100))
    assert (m.power_w, m.speed_kmh, m.cadence_rpm) == (100, None, None)
    assert d.update(sample(2.0, wheel=(5, 2048))).speed_kmh is None  # re-primed, not a 5-rev delta


@pytest.mark.parametrize(
    "bad",
    [{"type": "sample"}, {"t_lsl": "x", "fields": {}}, {"t_lsl": 1.0, "fields": []}, {"t_lsl": True, "fields": {}}],
)
def test_malformed_sample_returns_none(bad):
    assert CpsDeriver().update(bad) is None


def test_non_int_fields_are_ignored():
    d = CpsDeriver()
    m = d.update({"t_lsl": 1.0, "fields": {"power_w": True, "wheel_revs": 1.5, "wheel_event_t": 0}})
    assert (m.power_w, m.speed_kmh) == (None, None)


def test_reset_forgets_previous_counters():
    d = CpsDeriver()
    d.update(sample(0.0, wheel=(1000, 0)))
    d.reset()
    assert d.update(sample(1.0, wheel=(0, 2048))).speed_kmh is None


@pytest.mark.parametrize("c", [0, -1, math.nan, math.inf, True])
def test_invalid_circumference_rejected(c):
    with pytest.raises(ValueError):
        CpsDeriver(circumference_m=c)


def test_history_points_are_relative_to_cycle_start():
    h = History(SweepClock(100.0))
    h.add(100.0, 1)
    h.add(150.5, None)
    h.add(399.0, 3)
    assert h.points() == [(0.0, 1), (50.5, None), (299.0, 3)]


def test_history_clears_when_cycle_ends_and_skips_whole_cycles():
    clock = SweepClock(0.0)
    h = History(clock)
    h.add(10.0, 1)
    h.add(910.0, 2)  # cycles 1 and 2 had no data at all
    assert clock.cycle_start == 900.0 and clock.cycle == 3
    assert h.points() == [(10.0, 2)]


def test_history_drops_points_before_cycle_start():
    clock = SweepClock(0.0)
    h = History(clock)
    h.add(301.0, 1)
    h.add(299.0, 2)  # a late point from the previous cycle
    assert h.points() == [(1.0, 1)]


def test_shared_clock_clears_all_histories_together():
    clock = SweepClock(0.0)
    a, b = History(clock), History(clock)
    a.add(5.0, 1)
    b.add(6.0, 2)
    assert clock.advance(300.0) is True
    assert a.points() == [] and b.points() == []
    assert clock.advance(301.0) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_metrics.py`
Expected: collection error `ModuleNotFoundError: No module named 'wahoo_lsl.dashboard'`.

- [ ] **Step 3: Implement**

`src/wahoo_lsl/dashboard/__init__.py`:

```python
"""Test dashboard for the FTMS control API and the CPS logger (spec: 2026-10-21-kickr-dashboard-design)."""
```

`src/wahoo_lsl/dashboard/metrics.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_metrics.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/dashboard/__init__.py src/wahoo_lsl/dashboard/metrics.py tests/test_dashboard_metrics.py
git commit -m "feat(dashboard): CPS speed/cadence derivation and 5-minute sweep history

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: LogFollower

**Files:**
- Create: `src/wahoo_lsl/dashboard/logtail.py`
- Test: `tests/test_dashboard_logtail.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `LogFollower(log_dir, post, pattern="cps_*.jsonl", poll_s=0.25)` with `start()`, `stop()`, `poll_once()`, attribute `bad_lines: int`. `post` is called with `(kind, payload)` tuples: `cps_sample`, `cps_event`, `cps_file`, `cps_status`, `cps_bad` (see table above).

- [ ] **Step 1: Write the failing tests** — `tests/test_dashboard_logtail.py`:

```python
import json
import os
import time

import pytest

from wahoo_lsl.dashboard.logtail import LogFollower


def rec(seq, t=1.0):
    return json.dumps({"type": "sample", "seq": seq, "t_lsl": t, "fields": {"power_w": 100}}) + "\n"


class Collect(list):
    def __call__(self, item):
        self.append(item)

    def kinds(self, kind):
        return [p for k, p in self if k == kind]

    def seqs(self):
        return [r["seq"] for r in self.kinds("cps_sample")]


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(posts, log_dir=None, **kw):
        f = LogFollower(tmp_path if log_dir is None else log_dir, posts, **kw)
        made.append(f)
        return f

    yield factory
    for f in made:
        f.stop()


def test_waiting_posted_once_when_no_log(tmp_path, make):
    posts = Collect()
    f = make(posts, log_dir=tmp_path / "logs")
    f.poll_once()
    f.poll_once()
    assert posts == [("cps_status", f"waiting for CPS log in {tmp_path / 'logs'}")]


def test_new_file_read_from_start(tmp_path, make):
    posts = Collect()
    f = make(posts)
    (tmp_path / "other.jsonl").write_text(rec(9))
    (tmp_path / "cps_1.jsonl").write_text(rec(0) + rec(1))
    f.poll_once()
    assert posts.kinds("cps_file") == ["cps_1.jsonl"]
    assert posts.seqs() == [0, 1]


def test_preexisting_file_starts_at_eof(tmp_path, make):
    p = tmp_path / "cps_1.jsonl"
    p.write_text(rec(0))
    posts = Collect()
    f = make(posts)
    f.poll_once()
    assert posts.kinds("cps_file") == ["cps_1.jsonl"]
    assert posts.seqs() == []
    with p.open("a") as fh:
        fh.write(rec(1))
    f.poll_once()
    assert posts.seqs() == [1]


def test_preexisting_partial_line_is_discarded(tmp_path, make):
    p = tmp_path / "cps_1.jsonl"
    p.write_text(rec(0) + '{"type": "sample", "seq": 1')
    posts = Collect()
    f = make(posts)
    f.poll_once()
    with p.open("a") as fh:
        fh.write(', "t_lsl": 1.0, "fields": {}}\n' + rec(2))
    f.poll_once()
    assert posts.seqs() == [2]
    assert f.bad_lines == 0


def test_partial_line_waits_for_newline(tmp_path, make):
    posts = Collect()
    f = make(posts)
    p = tmp_path / "cps_1.jsonl"
    line = rec(0)
    p.write_text(line[:10])
    f.poll_once()
    assert posts.seqs() == []
    with p.open("a") as fh:
        fh.write(line[10:])
    f.poll_once()
    assert posts.seqs() == [0]


def test_switches_to_newer_file_and_reads_it_from_start(tmp_path, make):
    now = time.time()
    old = tmp_path / "cps_1.jsonl"
    old.write_text(rec(0))
    os.utime(old, (now - 100, now - 100))
    posts = Collect()
    f = make(posts)
    f.poll_once()
    new = tmp_path / "cps_2.jsonl"
    new.write_text(rec(5))
    os.utime(new, (now, now))
    f.poll_once()
    assert posts.kinds("cps_file") == ["cps_1.jsonl", "cps_2.jsonl"]
    assert posts.seqs() == [5]


def test_bad_lines_counted_and_posted(tmp_path, make):
    posts = Collect()
    f = make(posts)
    (tmp_path / "cps_1.jsonl").write_bytes(b"not json\n[1]\n\xc3(\n\n" + rec(0).encode())
    f.poll_once()
    assert f.bad_lines == 3
    assert posts.kinds("cps_bad") == [3]
    assert posts.seqs() == [0]


def test_only_connection_events_are_posted(tmp_path, make):
    posts = Collect()
    f = make(posts)
    events = [
        {"type": "event", "kind": "clock_sync", "detail": {}},
        {"type": "event", "kind": "connected", "detail": {"name": "KICKR"}},
        {"type": "event", "kind": "disconnected", "detail": {}},
    ]
    (tmp_path / "cps_1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    f.poll_once()
    assert [e["kind"] for e in posts.kinds("cps_event")] == ["connected", "disconnected"]


def test_thread_follows_and_stops(tmp_path, make):
    posts = Collect()
    f = make(posts, poll_s=0.01)
    f.start()
    (tmp_path / "cps_1.jsonl").write_text(rec(0))
    deadline = time.monotonic() + 2.0
    while not posts.seqs():
        assert time.monotonic() < deadline, "follower thread never posted the sample"
        time.sleep(0.01)
    f.stop()
    assert not f._thread.is_alive()
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_logtail.py`
Expected: `ModuleNotFoundError: No module named 'wahoo_lsl.dashboard.logtail'`.

- [ ] **Step 3: Implement** — `src/wahoo_lsl/dashboard/logtail.py`:

```python
"""Follow the newest CPS JSONL log and post its samples to the dashboard."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import IO, Any, Callable

Post = Callable[[tuple[str, Any]], None]
CONNECTION_EVENTS = ("connected", "disconnected")


class LogFollower:
    """Polls log_dir for the newest file matching pattern and posts complete lines.

    A file that already existed when the follower was created is read from its end, so an
    old session is never plotted; files that appear later are read from the start.
    """

    def __init__(self, log_dir: str | Path, post: Post, pattern: str = "cps_*.jsonl", poll_s: float = 0.25) -> None:
        self._dir = Path(log_dir)
        self._post = post
        self._pattern = pattern
        self._poll_s = poll_s
        self._preexisting = set(self._candidates())
        self._path: Path | None = None
        self._file: IO[bytes] | None = None
        self._buf = b""
        self._discard_partial = False
        self._waiting_posted = False
        self.bad_lines = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cps-log-follower", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self._close()

    def poll_once(self) -> None:
        newest = self._newest()
        if newest is None:
            if self._path is None and not self._waiting_posted:
                self._post(("cps_status", f"waiting for CPS log in {self._dir}"))
                self._waiting_posted = True
            return
        if newest != self._path:
            self._open(newest)
        self._read()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except OSError as e:
                self._post(("cps_status", f"log read error: {e}"))
                self._close()
            self._stop.wait(self._poll_s)

    def _candidates(self) -> list[Path]:
        try:
            return [p for p in self._dir.glob(self._pattern) if p.is_file()]
        except OSError:
            return []

    def _newest(self) -> Path | None:
        best, best_key = None, None
        for p in self._candidates():
            try:
                key = (p.stat().st_mtime_ns, p.name)
            except OSError:
                continue  # removed between glob and stat
            if best_key is None or key > best_key:
                best, best_key = p, key
        return best

    def _open(self, path: Path) -> None:
        self._close()
        f = open(path, "rb")
        self._discard_partial = False
        if path in self._preexisting:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                self._discard_partial = f.read(1) != b"\n"
        self._file, self._path, self._buf = f, path, b""
        self._waiting_posted = False
        self._post(("cps_file", path.name))

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file, self._path, self._buf = None, None, b""

    def _read(self) -> None:
        assert self._file is not None
        chunk = self._file.read()
        if not chunk:
            return
        *lines, self._buf = (self._buf + chunk).split(b"\n")
        if self._discard_partial and lines:
            lines = lines[1:]  # the tail of a line that was mid-write when we attached
            self._discard_partial = False
        before = self.bad_lines
        for line in lines:
            self._handle(line)
        if self.bad_lines != before:
            self._post(("cps_bad", self.bad_lines))

    def _handle(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            self.bad_lines += 1
            return
        if not isinstance(record, dict):
            self.bad_lines += 1
            return
        if record.get("type") == "sample":
            self._post(("cps_sample", record))
        elif record.get("type") == "event" and record.get("kind") in CONNECTION_EVENTS:
            self._post(("cps_event", record))
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_logtail.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/dashboard/logtail.py tests/test_dashboard_logtail.py
git commit -m "feat(dashboard): follow the newest CPS log file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: PowerWorker

**Files:**
- Create: `src/wahoo_lsl/dashboard/power.py`
- Test: `tests/test_dashboard_power.py`

**Interfaces:**
- Consumes: `wahoo_lsl.ftms.client.KickrClient(host, port, timeout_s)` (constructor connects; raises `OSError` if unreachable), `.set_target_power(watts) -> int`, `.read() -> Status`, `.close()`; `KickrError` with `.code`, `.detail`. Existing test helper `tests/ftms_stub.py` (`StubService(reply)`, `.port`, `.requests`, `.disconnects`, `snapshot(...)`).
- Produces: `PowerWorker(post, host=DEFAULT_HOST, port=DEFAULT_PORT, poll_s=0.5, retry_s=1.0, timeout_s=1.0, client_factory=KickrClient)` with `start()`, `request(watts: int)`, `stop(join_s=5.0)`. Posts `service_connected`, `service_down`, `status`, `set_ok`, `set_error`, `service_error`.

- [ ] **Step 1: Write the failing tests** — `tests/test_dashboard_power.py`:

```python
import queue
import socket
import threading
import time

from ftms_stub import StubService, snapshot

from wahoo_lsl.dashboard.power import PowerWorker
from wahoo_lsl.ftms.client import PowerReading, Status, TargetState

STATUS = Status("ready", 1, TargetState(0, 0, False, None), PowerReading(None, None, None), None, None)


class Events:
    def __init__(self):
        self.q = queue.Queue()
        self.seen = []

    def __call__(self, item):
        self.q.put(item)

    def wait(self, kind, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {kind!r} event; saw {[k for k, _ in self.seen]}")
            try:
                k, p = self.q.get(timeout=remaining)
            except queue.Empty:
                continue
            self.seen.append((k, p))
            if k == kind:
                return p

    def kinds(self):
        while True:
            try:
                self.seen.append(self.q.get_nowait())
            except queue.Empty:
                return [k for k, _ in self.seen]


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def default_reply(request):
    if request["cmd"] == "set":
        return {"ok": True, "target_w": request["watts"], "clamped": False}
    return snapshot(requested=150, confirmed=150)


def sets(svc):
    return [r["watts"] for r in svc.requests if r["cmd"] == "set"]


def test_connects_and_posts_status():
    ev = Events()
    with StubService(default_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            assert ev.wait("service_connected") is None
            status = ev.wait("status")
            assert isinstance(status, Status) and status.target.requested_w == 150
        finally:
            w.stop()


def test_request_sends_set_and_posts_set_ok():
    ev = Events()
    with StubService(default_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            w.request(180)
            assert ev.wait("set_ok") == 180
            assert sets(svc)[0] == 180
        finally:
            w.stop()


def test_rapid_requests_coalesce_to_latest():
    ev = Events()
    gate, entered = threading.Event(), threading.Event()

    def reply(request):
        if request["cmd"] == "read" and not gate.is_set():
            entered.set()
            gate.wait(3.0)
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            assert entered.wait(3.0)  # the worker is blocked inside read()
            for watts in (100, 150, 200):
                w.request(watts)
            gate.set()
            assert ev.wait("set_ok") == 200
            time.sleep(0.1)
            assert sets(svc) == [200]
        finally:
            gate.set()
            w.stop()


def test_failed_set_is_dropped_not_retried():
    ev = Events()

    def reply(request):
        if request["cmd"] == "set":
            return {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            w.request(120)
            assert ev.wait("set_error") == "link_down: BLE link is reconnecting"
            for _ in range(3):
                ev.wait("status")
            assert sets(svc) == [120]
        finally:
            w.stop()
        assert sets(svc) == [120, 0]  # only the shutdown 0 W follows


def test_reconnect_after_connection_loss_posts_connected_again():
    ev = Events()
    reads = {"n": 0}

    def reply(request):
        if request["cmd"] == "read":
            reads["n"] += 1
            if reads["n"] == 1:
                return None  # the stub closes the connection
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02, retry_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            assert ev.wait("service_down").startswith("connection lost")
            ev.wait("service_connected")
            ev.wait("status")
        finally:
            w.stop()


def test_unreachable_posts_service_down_once():
    ev = Events()
    w = PowerWorker(ev, port=closed_port(), retry_s=0.01)
    w.start()
    try:
        assert "unreachable" in ev.wait("service_down")
        time.sleep(0.2)
        assert ev.kinds().count("service_down") == 1
    finally:
        w.stop()


class FakeClient:
    def __init__(self):
        self.sets = []
        self.closed = False

    def set_target_power(self, watts):
        self.sets.append(watts)
        return watts

    def read(self):
        return STATUS

    def close(self):
        self.closed = True


def test_pending_value_cleared_on_connect():
    ev = Events()
    client, allow = FakeClient(), threading.Event()

    def factory(host, port, timeout_s):
        if not allow.is_set():
            raise ConnectionRefusedError("refused")
        return client

    w = PowerWorker(ev, poll_s=0.01, retry_s=0.01, client_factory=factory)
    w.start()
    try:
        ev.wait("service_down")
        w.request(300)  # chosen while the service is down
        allow.set()
        ev.wait("service_connected")
        ev.wait("status")
        ev.wait("status")
        assert client.sets == []
    finally:
        w.stop()
    assert client.sets == [0] and client.closed


def test_busy_reply_drops_connection_and_retries():
    ev = Events()
    with StubService(lambda r: {"ok": False, "error": "busy", "detail": "another client is connected"}) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02, retry_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            assert ev.wait("service_error") == "busy: another client is connected"
            assert "busy" in ev.wait("service_down")
            ev.wait("service_connected")  # retried
        finally:
            w.stop()


def test_stop_sends_zero_and_closes():
    ev = Events()
    with StubService(default_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        ev.wait("status")
        w.stop()
        assert not w._thread.is_alive()
        assert svc.requests[-1] == {"cmd": "set", "watts": 0}
        deadline = time.monotonic() + 2.0
        while svc.disconnects < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_stop_while_unreachable_is_prompt():
    w = PowerWorker(Events(), port=closed_port(), retry_s=10.0)
    w.start()
    time.sleep(0.05)
    started = time.monotonic()
    w.stop()
    assert time.monotonic() - started < 2.0
    assert not w._thread.is_alive()
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_power.py`
Expected: `ModuleNotFoundError: No module named 'wahoo_lsl.dashboard.power'`.

- [ ] **Step 3: Implement** — `src/wahoo_lsl/dashboard/power.py`:

```python
"""Background thread that owns the dashboard's KickrClient; the Tk thread never blocks on it."""
from __future__ import annotations

import threading
from typing import Any, Callable

from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.client import KickrClient, KickrError

Post = Callable[[tuple[str, Any]], None]


def _describe(e: BaseException) -> str:
    return str(e) or type(e).__name__


class PowerWorker:
    """Sends the latest requested target and polls read(), posting results as (kind, payload).

    A value requested while disconnected, or a set that fails, is dropped and never replayed.
    """

    def __init__(
        self,
        post: Post,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        poll_s: float = 0.5,
        retry_s: float = 1.0,
        timeout_s: float = 1.0,
        client_factory: Callable[..., Any] = KickrClient,
    ) -> None:
        self._post = post
        self._host, self._port = host, port
        self._poll_s, self._retry_s, self._timeout_s = poll_s, retry_s, timeout_s
        self._factory = client_factory
        self._lock = threading.Lock()
        self._pending: int | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._client: Any = None
        self._down_msg: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ftms-power-worker", daemon=True)
        self._thread.start()

    def request(self, watts: int) -> None:
        with self._lock:
            self._pending = int(watts)
        self._wake.set()

    def stop(self, join_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(join_s)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._client is None and not self._connect():
                    self._sleep(self._retry_s)
                    continue
                self._send_pending()
                if self._client is not None:
                    self._read()
                self._sleep(self._poll_s)
        finally:
            self._shutdown()

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _connect(self) -> bool:
        try:
            self._client = self._factory(self._host, self._port, self._timeout_s)
        except OSError as e:
            self._down(f"service unreachable ({_describe(e)})")
            return False
        with self._lock:
            self._pending = None  # never replay a value chosen while disconnected
        self._down_msg = None
        self._post(("service_connected", None))
        return True

    def _down(self, msg: str) -> None:
        if msg != self._down_msg:  # each distinct message once, not every retry
            self._down_msg = msg
            self._post(("service_down", msg))

    def _drop(self, msg: str) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None
        self._down(msg)

    def _send_pending(self) -> None:
        with self._lock:
            watts, self._pending = self._pending, None
        if watts is None:
            return
        try:
            self._post(("set_ok", self._client.set_target_power(watts)))
        except KickrError as e:
            self._post(("set_error", f"{e.code}: {e.detail}"))
            if e.code == "busy":
                self._drop("service busy: another client is connected")
        except Exception as e:
            self._post(("set_error", f"set {watts} W failed: {_describe(e)}"))
            self._drop(f"connection lost ({_describe(e)})")

    def _read(self) -> None:
        try:
            status = self._client.read()
        except KickrError as e:
            self._post(("service_error", f"{e.code}: {e.detail}"))
            if e.code == "busy":
                self._drop("service busy: another client is connected")
            return
        except Exception as e:
            self._drop(f"connection lost ({_describe(e)})")
            return
        self._post(("status", status))

    def _shutdown(self) -> None:
        if self._client is None:
            return
        try:
            self._client.set_target_power(0)
        except Exception:
            pass  # closing the client makes the service spin down anyway
        try:
            self._client.close()
        except OSError:
            pass
        self._client = None
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_power.py`
Expected: all pass. Run it 3 times; it is thread-timing sensitive and must be stable.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/dashboard/power.py tests/test_dashboard_power.py
git commit -m "feat(dashboard): background power worker over KickrClient

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Graph and dashboard window

**Files:**
- Create: `src/wahoo_lsl/dashboard/graph.py`
- Create: `src/wahoo_lsl/dashboard/app.py`
- Test: `tests/test_dashboard_graph.py`
- Test: `tests/test_dashboard_app.py`

**Interfaces:**
- Consumes: Task 1 (`CpsDeriver`, `SweepClock`, `History`, `DEFAULT_CIRCUMFERENCE_M`), Task 2 (`LogFollower(log_dir, post)` with `start()`/`stop()`), Task 3 (`PowerWorker(post, host, port)` with `start()`/`request()`/`stop()`), `Status`/`TargetState`/`PowerReading` from `wahoo_lsl.ftms.client`.
- Produces: `graph.segments(points, max_gap_s=5.0)`, `graph.nice_ceiling(value, floor)`, `graph.Graph(parent, title, unit, y_floor)` with `.canvas` and `.draw(points)` (data items tagged `"data"`). `app.Dashboard(root, events, worker, follower, *, max_watts=1000, circumference_m=2.105, clock=pylsl.local_clock, monotonic=time.monotonic)`; `app.main(argv=None) -> int`; `python -m wahoo_lsl.dashboard.app` accepts `--host --port --log-dir --max-watts --circumference-m` (Task 5 builds this command line).

- [ ] **Step 1: Write the failing tests**

`tests/test_dashboard_graph.py`:

```python
import pytest

from wahoo_lsl.dashboard.graph import Graph, nice_ceiling, segments


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_segments_break_on_none_and_gaps():
    pts = [(0, 1), (1, 2), (2, None), (3, 4), (4, 5), (10, 6), (11, 7)]
    assert segments(pts) == [[(0, 1.0), (1, 2.0)], [(3, 4.0), (4, 5.0)], [(10, 6.0), (11, 7.0)]]


def test_segments_edge_cases():
    assert segments([]) == []
    assert segments([(0, None)]) == []
    assert segments([(0, 3)]) == [[(0, 3.0)]]
    assert segments([(0, 1), (5, 2)]) == [[(0, 1.0), (5, 2.0)]]  # a gap of exactly 5 s is not a break


@pytest.mark.parametrize(
    "value, floor, expected",
    [(0, 100, 100.0), (101, 100, 200.0), (200, 100, 200.0), (201, 100, 500.0),
     (999, 10, 1000.0), (1000, 10, 1000.0), (7.3, 10, 10.0), (0, 0, 1.0), (0.3, 0, 0.5)],
)
def test_nice_ceiling(value, floor, expected):
    assert nice_ceiling(value, floor) == pytest.approx(expected)


def test_graph_draws_one_item_per_run_and_redraws_cleanly(tk_root):
    g = Graph(tk_root, "Power", "W", 100)
    g.canvas.pack()
    tk_root.update()
    g.draw([(0, 100), (1, 110), (2, None), (3, 120), (20, 90)])
    assert len(g.canvas.find_withtag("data")) == 3  # one line and two isolated points
    texts = [g.canvas.itemcget(i, "text") for i in g.canvas.find_all() if g.canvas.type(i) == "text"]
    assert "200" in texts  # y axis max = nice_ceiling(120, 100)
    g.draw([])
    assert g.canvas.find_withtag("data") == ()
```

`tests/test_dashboard_app.py`:

```python
import queue
from types import SimpleNamespace

import pytest

from wahoo_lsl.dashboard import app as app_mod
from wahoo_lsl.dashboard.app import DASH, STALE_S, Dashboard
from wahoo_lsl.ftms.client import PowerReading, Status, TargetState


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


class FakeWorker:
    def __init__(self):
        self.requests = []
        self.stopped = 0

    def request(self, watts):
        self.requests.append(watts)

    def stop(self):
        self.stopped += 1


class FakeFollower:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def dash(tk_root):
    events, lsl, mono = queue.Queue(), Clock(1000.0), Clock(50.0)
    worker, follower = FakeWorker(), FakeFollower()
    d = Dashboard(tk_root, events, worker, follower, clock=lsl, monotonic=mono)
    ns = SimpleNamespace(app=d, events=events, lsl=lsl, mono=mono, root=tk_root, worker=worker, follower=follower)
    yield ns
    d.close()


def pump(ns, *items):
    for item in items:
        ns.events.put(item)
    ns.app._pump()
    ns.root.update()


def sample(t, power, wheel, crank):
    return ("cps_sample", {"type": "sample", "t_lsl": t, "fields": {
        "power_w": power, "wheel_revs": wheel[0], "wheel_event_t": wheel[1],
        "crank_revs": crank[0], "crank_event_t": crank[1]}})


def test_slider_change_requests_power_once_per_value(dash):
    dash.app.slider.set(150)
    dash.root.update()
    dash.app.slider.set(150)
    dash.root.update()
    assert dash.worker.requests == [150]


def test_zero_button_sends_zero(dash):
    dash.app.slider.set(200)
    dash.root.update()
    dash.app.zero_button.invoke()
    dash.root.update()
    assert dash.app.slider.get() == 0
    assert dash.worker.requests == [200, 0]


def test_service_connected_resets_slider_to_zero(dash):
    dash.app.slider.set(200)
    dash.root.update()
    pump(dash, ("service_connected", None))
    assert dash.app.slider.get() == 0
    assert dash.worker.requests[-1] == 0
    assert "service: connected" in dash.app.status_text.get()


def test_status_updates_target_panel_and_history(dash):
    status = Status("ready", 2, TargetState(200, 180, True, 5.0), PowerReading(190, 5.0, 0.1), "spin-down failed", 6.0)
    pump(dash, ("status", status))
    assert dash.app.target_text.get() == "requested 200 W · confirmed 180 W (pending)"
    assert dash.app.link_text.get() == "link ready (epoch 2)"
    assert dash.app.error_text.get() == "spin-down failed"
    assert dash.app.histories["target"].points() == [(0.0, 180)]


def test_service_down_blanks_target_panel(dash):
    pump(dash, ("status", Status("ready", 1, TargetState(100, 100, False, 1.0), PowerReading(None, None, None), None, None)))
    pump(dash, ("service_down", "service unreachable (refused)"))
    assert dash.app.target_text.get() == f"requested {DASH} · confirmed {DASH}"
    assert "service unreachable" in dash.app.status_text.get()


def test_set_error_shown_and_cleared_by_set_ok(dash):
    pump(dash, ("set_error", "link_down: x"))
    assert dash.app.set_error_text.get() == "link_down: x"
    pump(dash, ("set_ok", 100))
    assert dash.app.set_error_text.get() == ""


def test_cps_samples_update_values_and_histories(dash):
    pump(dash, sample(1000.0, 150, (10, 0), (5, 0)), sample(1001.0, 160, (11, 2048), (6, 1024)))
    assert dash.app.cps_values["power"].get() == "160"
    assert dash.app.cps_values["speed"].get() == "7.6"
    assert dash.app.cps_values["cadence"].get() == "60"
    assert dash.app.histories["power"].points() == [(0.0, 150), (1.0, 160)]
    speed = dash.app.histories["speed"].points()
    assert speed[0] == (0.0, None) and speed[1][1] == pytest.approx(2.105 * 3.6)


def test_values_go_stale_without_samples(dash):
    pump(dash, sample(1000.0, 150, (10, 0), (5, 0)))
    dash.mono.t += STALE_S + 0.1
    pump(dash)
    assert all(v.get() == DASH for v in dash.app.cps_values.values())
    assert "stale" in dash.app.cps_state.get()


@pytest.mark.parametrize("reset", [("cps_file", "cps_2.jsonl"), ("cps_event", {"type": "event", "kind": "connected"})])
def test_new_log_file_or_cps_event_resets_deriver(dash, reset):
    pump(dash, sample(1000.0, 150, (1000, 0), (500, 0)), reset, sample(1001.0, 150, (0, 2048), (0, 1024)))
    assert dash.app.cps_values["speed"].get() == DASH  # re-primed: no bogus wrap-around speed
    assert dash.app.cps_values["cadence"].get() == DASH


def test_status_bar_shows_log_file_and_bad_lines(dash):
    pump(dash, ("cps_file", "cps_2.jsonl"), ("cps_bad", 3))
    text = dash.app.status_text.get()
    assert "cps_2.jsonl" in text and "bad lines: 3" in text


def test_redraw_draws_and_sweep_clears_after_window(dash):
    pump(dash, ("status", Status("ready", 1, TargetState(100, 100, False, 1.0), PowerReading(None, None, None), None, None)))
    dash.app._redraw()
    assert len(dash.app.graphs["target"].canvas.find_withtag("data")) == 1
    dash.lsl.t = 1300.0
    dash.app._redraw()
    assert dash.app.histories["target"].points() == []


def test_close_stops_worker_and_follower_once(dash):
    dash.app.close()
    dash.app.close()
    assert (dash.worker.stopped, dash.follower.stopped) == (1, 1)


def test_request_close_closes_on_next_pump(dash):
    dash.app.request_close()
    dash.app._pump()
    assert dash.worker.stopped == 1


def test_main_without_display_exits_2(monkeypatch, capsys):
    def no_display():
        raise app_mod.tk.TclError("no display name")

    monkeypatch.setattr(app_mod.tk, "Tk", no_display)
    assert app_mod.main([]) == 2
    assert "cannot open a window" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--max-watts", "0"], ["--circumference-m", "-1"]])
def test_main_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as info:
        app_mod.main(argv)
    assert info.value.code == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_graph.py tests/test_dashboard_app.py`
Expected: `ModuleNotFoundError` for `wahoo_lsl.dashboard.graph` / `.app`.

- [ ] **Step 3: Implement**

`src/wahoo_lsl/dashboard/graph.py`:

```python
"""Tk canvas line chart with a fixed 0-300 s sweep axis."""
from __future__ import annotations

import math
import tkinter as tk
from typing import Sequence

MAX_GAP_S = 5.0
SMALL_FONT = "TkSmallCaptionFont"

Point = tuple[float, "float | None"]


def segments(points: Sequence[Point], max_gap_s: float = MAX_GAP_S) -> list[list[tuple[float, float]]]:
    """Split points into drawable runs, breaking at None values and at x gaps > max_gap_s."""
    runs: list[list[tuple[float, float]]] = []
    run: list[tuple[float, float]] = []
    for x, y in points:
        if y is None or (run and x - run[-1][0] > max_gap_s):
            if run:
                runs.append(run)
            run = []
        if y is not None:
            run.append((x, float(y)))
    if run:
        runs.append(run)
    return runs


def nice_ceiling(value: float, floor: float) -> float:
    """Smallest 1/2/5 x 10^k that is >= max(value, floor)."""
    v = max(value, floor)
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        c = m * 10.0**exp
        if c >= v:
            return c
    return 10.0 ** (exp + 1)


class Graph:
    LEFT, RIGHT, TOP, BOTTOM = 44, 10, 18, 18

    def __init__(self, parent: tk.Misc, title: str, unit: str, y_floor: float,
                 window_s: float = 300.0, width: int = 640, height: int = 130, color: str = "#1f77b4") -> None:
        self.title, self.unit, self.y_floor, self.window_s, self.color = title, unit, y_floor, window_s, color
        self.canvas = tk.Canvas(parent, width=width, height=height, bg="white", highlightthickness=0)

    def draw(self, points: Sequence[Point]) -> None:
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() if c.winfo_width() > 1 else int(c.cget("width"))
        h = c.winfo_height() if c.winfo_height() > 1 else int(c.cget("height"))
        x0, x1, top, bottom = self.LEFT, w - self.RIGHT, self.TOP, h - self.BOTTOM
        ymax = nice_ceiling(max((y for _, y in points if y is not None), default=0.0), self.y_floor)

        def sx(x: float) -> float:
            return x0 + (x1 - x0) * x / self.window_s

        def sy(y: float) -> float:
            return bottom - (bottom - top) * max(y, 0.0) / ymax

        c.create_text(x0, 2, text=f"{self.title} ({self.unit})", anchor="nw", font=SMALL_FONT)
        c.create_rectangle(x0, top, x1, bottom, outline="#999")
        for tick in range(0, int(self.window_s) + 1, 60):
            c.create_line(sx(tick), bottom, sx(tick), bottom + 3, fill="#999")
            c.create_text(sx(tick), bottom + 3, text=f"{tick // 60}m", anchor="n", font=SMALL_FONT)
        for frac in (0.0, 0.5, 1.0):
            c.create_text(x0 - 4, sy(ymax * frac), text=f"{ymax * frac:g}", anchor="e", font=SMALL_FONT)
        for run in segments(points):
            coords = [v for x, y in run for v in (sx(x), sy(y))]
            if len(run) == 1:
                cx, cy = coords
                c.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5, fill=self.color, outline="", tags="data")
            else:
                c.create_line(*coords, fill=self.color, width=1.5, tags="data")
```

`src/wahoo_lsl/dashboard/app.py`:

```python
"""KICKR test dashboard window: target power slider, FTMS read-back and CPS metrics."""
from __future__ import annotations

import argparse
import queue
import signal
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Any, Callable, Sequence

import pylsl

from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.client import Status

from .graph import Graph
from .logtail import LogFollower
from .metrics import DEFAULT_CIRCUMFERENCE_M, CpsDeriver, History, SweepClock
from .power import PowerWorker

PUMP_MS = 100
REDRAW_MS = 1000
STALE_S = 15.0  # longer than the logger's flush batches (10 samples at ~1 Hz)
DASH = "—"
CPS_PANELS = (  # key, title, unit, y floor, value format
    ("power", "Power", "W", 100, "{:.0f}"),
    ("speed", "Speed", "km/h", 10, "{:.1f}"),
    ("cadence", "Cadence", "rpm", 60, "{:.0f}"),
)


class Dashboard:
    def __init__(
        self,
        root: tk.Tk,
        events: queue.Queue,
        worker: Any,
        follower: Any,
        *,
        max_watts: int = 1000,
        circumference_m: float = DEFAULT_CIRCUMFERENCE_M,
        clock: Callable[[], float] = pylsl.local_clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root, self.events, self.worker, self.follower = root, events, worker, follower
        self._clock, self._monotonic = clock, monotonic
        self._deriver = CpsDeriver(circumference_m)
        self._sweep = SweepClock(clock())
        self.histories = {key: History(self._sweep) for key in ("target", "power", "speed", "cadence")}
        self._last_sent: int | None = None
        self._cps_arrival: float | None = None
        self._service, self._log_file, self._bad = "starting", DASH, 0
        self._closing = False
        self._close_requested = False
        self._build(max_watts)
        self._refresh_status()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._pump_id = root.after(PUMP_MS, self._pump)
        self._redraw_id = root.after(REDRAW_MS, self._redraw)

    def _build(self, max_watts: int) -> None:
        root = self.root
        root.title("KICKR test dashboard")
        big = tkfont.nametofont("TkDefaultFont").copy()
        big.configure(size=16, weight="bold")

        ctl = tk.Frame(root, padx=8, pady=6)
        ctl.pack(fill="x")
        tk.Label(ctl, text="Target power (W)").pack(side="left")
        self.slider = tk.Scale(ctl, from_=0, to=max_watts, orient="horizontal", resolution=1, length=420,
                               command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True, padx=6)
        self.zero_button = tk.Button(ctl, text="0 W", command=lambda: self._set_target(0))
        self.zero_button.pack(side="left")

        self.target_text = tk.StringVar(value=f"requested {DASH} · confirmed {DASH}")
        self.link_text = tk.StringVar(value=f"link {DASH}")
        self.error_text = tk.StringVar(value="")
        self.set_error_text = tk.StringVar(value="")
        tgt = tk.LabelFrame(root, text="FTMS target (read back)", padx=6, pady=4)
        tgt.pack(fill="x", padx=8, pady=4)
        tk.Label(tgt, textvariable=self.target_text, font=big, anchor="w").pack(fill="x")
        tk.Label(tgt, textvariable=self.link_text, anchor="w").pack(fill="x")
        tk.Label(tgt, textvariable=self.error_text, fg="#b00", anchor="w").pack(fill="x")
        tk.Label(tgt, textvariable=self.set_error_text, fg="#b00", anchor="w").pack(fill="x")
        self.graphs = {"target": Graph(tgt, "Confirmed target", "W", 100)}
        self.graphs["target"].canvas.pack(fill="x", expand=True)

        cps = tk.LabelFrame(root, text="CPS (from log)", padx=6, pady=4)
        cps.pack(fill="both", expand=True, padx=8, pady=4)
        self.cps_state = tk.StringVar(value="CPS: starting")
        tk.Label(cps, textvariable=self.cps_state, anchor="w").pack(fill="x")
        self.cps_values: dict[str, tk.StringVar] = {}
        for key, title, unit, floor, _ in CPS_PANELS:
            row = tk.Frame(cps)
            row.pack(fill="x", pady=2)
            box = tk.Frame(row, width=130)
            box.pack(side="left", fill="y")
            box.pack_propagate(False)
            tk.Label(box, text=f"{title} ({unit})").pack(anchor="w")
            self.cps_values[key] = tk.StringVar(value=DASH)
            tk.Label(box, textvariable=self.cps_values[key], font=big).pack(anchor="w")
            self.graphs[key] = Graph(row, title, unit, floor)
            self.graphs[key].canvas.pack(side="left", fill="x", expand=True)

        self.status_text = tk.StringVar()
        tk.Label(root, textvariable=self.status_text, anchor="w", relief="sunken", padx=4).pack(fill="x", side="bottom")

    # --- target control -------------------------------------------------

    def _on_slider(self, value: str) -> None:
        self._send(int(float(value)))

    def _set_target(self, watts: int) -> None:
        self.slider.set(watts)
        self._send(watts)

    def _send(self, watts: int) -> None:
        if watts != self._last_sent:
            self._last_sent = watts
            self.worker.request(watts)

    # --- events ---------------------------------------------------------

    def handle(self, kind: str, payload: Any) -> None:
        if kind == "status":
            self._on_status(payload)
        elif kind == "service_connected":
            self._service = "connected"
            self._last_sent = None
            self._set_target(0)  # the service spun down when any previous connection dropped
        elif kind == "service_down":
            self._service = payload
            self.target_text.set(f"requested {DASH} · confirmed {DASH}")
            self.link_text.set(f"link {DASH}")
        elif kind == "set_ok":
            self.set_error_text.set("")
        elif kind == "set_error":
            self.set_error_text.set(payload)
        elif kind == "service_error":
            self.error_text.set(payload)
        elif kind == "cps_sample":
            self._on_sample(payload)
        elif kind == "cps_event":
            self._deriver.reset()  # the sensor (re)connected: its counters may have restarted
            self.cps_state.set(f"CPS: {payload.get('kind')}")
        elif kind == "cps_file":
            self._deriver.reset()
            self._log_file = payload
        elif kind == "cps_status":
            self.cps_state.set(f"CPS: {payload}")
        elif kind == "cps_bad":
            self._bad = payload
        self._refresh_status()

    def _on_status(self, s: Status) -> None:
        t = s.target
        confirmed = DASH if t.confirmed_w is None else str(t.confirmed_w)
        pending = " (pending)" if t.pending else ""
        self.target_text.set(f"requested {t.requested_w} W · confirmed {confirmed} W{pending}")
        self.link_text.set(f"link {s.link} (epoch {s.link_epoch})")
        self.error_text.set(s.last_error or "")
        self.histories["target"].add(self._clock(), t.confirmed_w)

    def _on_sample(self, record: dict) -> None:
        m = self._deriver.update(record)
        if m is None:
            return
        self._cps_arrival = self._monotonic()
        values = {"power": m.power_w, "speed": m.speed_kmh, "cadence": m.cadence_rpm}
        for key, _, _, _, fmt in CPS_PANELS:
            value = values[key]
            self.histories[key].add(m.t_lsl, value)
            self.cps_values[key].set(DASH if value is None else fmt.format(value))
        self.cps_state.set("CPS: live")

    def _refresh_status(self) -> None:
        self.status_text.set(f"service: {self._service} · log: {self._log_file} · bad lines: {self._bad}")

    # --- timers and lifecycle --------------------------------------------

    def _pump(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            self.handle(kind, payload)
        if self._cps_arrival is not None and self._monotonic() - self._cps_arrival > STALE_S:
            for var in self.cps_values.values():
                var.set(DASH)
            self.cps_state.set(f"CPS: stale (no samples for {STALE_S:.0f} s)")
            self._cps_arrival = None
        if self._close_requested:
            self.close()
            return
        self._pump_id = self.root.after(PUMP_MS, self._pump)

    def _redraw(self) -> None:
        if self._closing:
            return
        self._sweep.advance(self._clock())
        for key, graph in self.graphs.items():
            graph.draw(self.histories[key].points())
        self._redraw_id = self.root.after(REDRAW_MS, self._redraw)

    def request_close(self) -> None:
        """Safe from a signal handler: the next pump closes the window."""
        self._close_requested = True

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for after_id in (self._pump_id, self._redraw_id):
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self.worker.stop()
        self.follower.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo_lsl.dashboard.app", description="KICKR FTMS/CPS test dashboard")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"FTMS service host (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"FTMS service port (default {DEFAULT_PORT})")
    p.add_argument("--log-dir", type=Path, default=Path("logs"), help="CPS log folder (default logs)")
    p.add_argument("--max-watts", type=int, default=1000, help="slider maximum in W (default 1000)")
    p.add_argument("--circumference-m", type=float, default=DEFAULT_CIRCUMFERENCE_M,
                   help=f"wheel circumference for speed (default {DEFAULT_CIRCUMFERENCE_M})")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_watts <= 0:
        parser.error("--max-watts must be positive")
    if not args.circumference_m > 0:
        parser.error("--circumference-m must be positive")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"wahoo-dashboard: cannot open a window: {e}", file=sys.stderr)
        return 2
    events: queue.Queue = queue.Queue()
    worker = PowerWorker(events.put, args.host, args.port)
    follower = LogFollower(args.log_dir, events.put)
    app = Dashboard(root, events, worker, follower, max_watts=args.max_watts, circumference_m=args.circumference_m)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: app.request_close())
    worker.start()
    follower.start()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_graph.py tests/test_dashboard_app.py`
Expected: all pass (Tk tests skip only if no display is available — on the macOS dev machine they must run, not skip; check with `-rs`).

Then a manual smoke run against nothing: `conda run -n wahoo python -m wahoo_lsl.dashboard.app --log-dir /tmp/none` — the window opens, the status bar shows "service unreachable …", the CPS line shows "waiting for CPS log in /tmp/none"; closing the window exits 0. Note the result in the report (you cannot click; it is enough to start it in the background, wait 3 s, send SIGINT, and confirm exit code 0).

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/dashboard/graph.py src/wahoo_lsl/dashboard/app.py tests/test_dashboard_graph.py tests/test_dashboard_app.py
git commit -m "feat(dashboard): Tk window with target slider, read-back and CPS graphs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Launcher (`wahoo-dashboard`)

**Files:**
- Create: `src/wahoo_lsl/dashboard/launcher.py`
- Create: `tests/dashboard_fake_child.py`
- Test: `tests/test_dashboard_launcher.py`
- Modify: `pyproject.toml` (`[project.scripts]` only)

**Interfaces:**
- Consumes: `wahoo_lsl.ftms.config.load_ftms_config(path) -> FtmsConfig` (`.ftms.host`, `.ftms.port`; raises `wahoo_lsl.config.ConfigError`); the dashboard command line from Task 4.
- Produces: `launcher.main(argv=None) -> int`; `find_script(name, scripts_dir=None) -> Path | None`; `build_commands(args, host, port, scripts_dir=None) -> dict[str, list[str]]` (keys `ftms`, `cps`, `dash`); `LauncherError`; `Timings`; `Supervisor(commands, host, port, out, stop, hurry, timings).run() -> int`; module flag `WIN`.

- [ ] **Step 1: Write the fake child and the failing tests**

`tests/dashboard_fake_child.py`:

```python
"""Stand-in child for launcher tests: python dashboard_fake_child.py LABEL ORDER_FILE [MODE ...]

Modes: none (idle until interrupted), "listen PORT", "exit CODE DELAY_S", "ignore" (ignores
interrupts; only terminate() stops it). On interrupt: append LABEL to ORDER_FILE, exit 0.
"""
import signal
import socket
import sys
import time

label, order_file, mode = sys.argv[1], sys.argv[2], sys.argv[3:]
stopping = False


def on_interrupt(signum, frame):
    global stopping
    stopping = True


handler = signal.SIG_IGN if mode[:1] == ["ignore"] else on_interrupt
signal.signal(signal.SIGINT, handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, handler)

print(f"{label} started", flush=True)
server = None
if mode[:1] == ["listen"]:
    server = socket.socket()
    server.bind(("127.0.0.1", int(mode[1])))
    server.listen(8)
if mode[:1] == ["exit"]:
    time.sleep(float(mode[2]))
    sys.exit(int(mode[1]))
while not stopping:
    time.sleep(0.02)
if server is not None:
    server.close()
with open(order_file, "a") as f:
    f.write(label + "\n")
print(f"{label} stopping", flush=True)
```

`tests/test_dashboard_launcher.py`:

```python
import io
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wahoo_lsl.dashboard import launcher
from wahoo_lsl.dashboard.launcher import LauncherError, Supervisor, Timings, build_commands, find_script

FAKE = str(Path(__file__).with_name("dashboard_fake_child.py"))
FAST = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake(label, order, *mode):
    return [sys.executable, FAKE, label, str(order), *map(str, mode)]


def start(commands, port, timings=FAST):
    ns = SimpleNamespace(out=io.StringIO(), stop=threading.Event(), hurry=threading.Event(), code=None)
    sup = Supervisor(commands, "127.0.0.1", port, ns.out, ns.stop, ns.hurry, timings)

    def run():
        ns.code = sup.run()

    ns.thread = threading.Thread(target=run)
    ns.thread.start()
    return ns


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def finish(ns, timeout=30.0):
    ns.thread.join(timeout)
    assert not ns.thread.is_alive(), "supervisor did not finish"
    return ns.code


def order(path):
    return path.read_text().split() if path.exists() else []


def happy(tmp_path, port, dash_mode=()):
    o = tmp_path / "order"
    return o, {"ftms": fake("ftms", o, "listen", port), "cps": fake("cps", o), "dash": fake("dash", o, *dash_mode)}


def test_stop_shuts_down_dash_then_ftms_then_cps(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    ns = start(cmds, port)
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 0
    assert order(o) == ["dash", "ftms", "cps"]
    out = ns.out.getvalue()
    assert "[ftms] ftms started" in out and "[cps] cps started" in out and "[ftms] ftms stopping" in out


def test_dashboard_exit_shuts_down_the_rest(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("exit", 0, 0.2))
    assert finish(start(cmds, port)) == 0
    assert order(o) == ["ftms", "cps"]


def test_first_nonzero_exit_code_is_returned(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("exit", 4, 0.2))
    assert finish(start(cmds, port)) == 4
    assert order(o) == ["ftms", "cps"]


def test_ftms_exit_during_startup_returns_its_code(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o, "exit", 3, 0), "cps": fake("cps", o), "dash": fake("dash", o)}
    ns = start(cmds, port)
    assert finish(ns) == 3
    assert "[cps]" not in ns.out.getvalue()


def test_port_timeout_returns_2_and_stops_ftms(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o), "cps": fake("cps", o), "dash": fake("dash", o)}
    ns = start(cmds, port, Timings(port_wait_s=0.5, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05))
    assert finish(ns) == 2
    assert order(o) == ["ftms"]
    assert "did not open" in ns.out.getvalue() and "[cps]" not in ns.out.getvalue()


def test_cps_early_exit_returns_its_code(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o, "listen", port), "cps": fake("cps", o, "exit", 5, 0), "dash": fake("dash", o)}
    ns = start(cmds, port)
    assert finish(ns) == 5
    assert order(o) == ["ftms"]
    assert "[dash]" not in ns.out.getvalue()


def test_stop_during_port_wait_starts_nothing_else(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o), "cps": fake("cps", o), "dash": fake("dash", o)}
    ns = start(cmds, port, Timings(port_wait_s=30.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05))
    wait_for(lambda: "[ftms] ftms started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 0
    assert order(o) == ["ftms"]


def test_child_ignoring_interrupt_is_terminated(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("ignore",))
    ns = start(cmds, port, Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=0.3, kill_after_s=2.0, poll_s=0.05))
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 1  # terminated, not a clean exit
    assert order(o) == ["ftms", "cps"]


def test_hurry_skips_grace(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("ignore",))
    ns = start(cmds, port, Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=60.0, kill_after_s=2.0, poll_s=0.05))
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    started = time.monotonic()
    ns.stop.set()
    time.sleep(0.3)
    ns.hurry.set()
    assert finish(ns, timeout=15.0) == 1
    assert time.monotonic() - started < 15.0


def test_find_script(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    (tmp_path / "wahoo-ftms").write_text("")
    assert find_script("wahoo-ftms", tmp_path) == tmp_path / "wahoo-ftms"
    assert find_script("wahoo-lsl", tmp_path) is None


def test_find_script_prefers_exe_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", True)
    (tmp_path / "wahoo-ftms").write_text("")
    (tmp_path / "wahoo-ftms.exe").write_text("")
    assert find_script("wahoo-ftms", tmp_path) == tmp_path / "wahoo-ftms.exe"


def test_find_script_defaults_to_this_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name: str(tmp_path) if name == "scripts" else None)
    (tmp_path / "wahoo-lsl").write_text("")
    assert find_script("wahoo-lsl") == tmp_path / "wahoo-lsl"


def test_build_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    for name in ("wahoo-ftms", "wahoo-lsl"):
        (tmp_path / name).write_text("")
    args = launcher.build_parser().parse_args(
        ["--ftms-config", "f.json", "--cps-config", "c.json", "--log-dir", "L", "--debug",
         "--max-watts", "600", "--circumference-m", "2.0"])
    cmds = build_commands(args, "127.0.0.1", 47999, tmp_path)
    assert cmds["ftms"] == [str(tmp_path / "wahoo-ftms"), "serve", "-c", "f.json", "--debug"]
    assert cmds["cps"] == [str(tmp_path / "wahoo-lsl"), "run", "-c", "c.json", "--debug"]
    assert cmds["dash"] == [sys.executable, "-m", "wahoo_lsl.dashboard.app", "--host", "127.0.0.1",
                            "--port", "47999", "--log-dir", "L", "--max-watts", "600", "--circumference-m", "2.0"]


def test_build_commands_reports_missing_scripts(tmp_path):
    args = launcher.build_parser().parse_args([])
    with pytest.raises(LauncherError, match="wahoo-ftms, wahoo-lsl"):
        build_commands(args, "127.0.0.1", 47610, tmp_path)


def test_main_config_error_exits_2(tmp_path, capsys):
    assert launcher.main(["--ftms-config", str(tmp_path / "missing.json")]) == 2
    assert "cannot read config" in capsys.readouterr().err


def test_main_missing_scripts_exits_2(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "ftms.json"
    cfg.write_text('{"device": {"name": "KICKR"}}')
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name: str(empty))
    assert launcher.main(["--ftms-config", str(cfg)]) == 2
    assert "pip install -e ." in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_launcher.py`
Expected: `ModuleNotFoundError: No module named 'wahoo_lsl.dashboard.launcher'`.

- [ ] **Step 3: Implement** — `src/wahoo_lsl/dashboard/launcher.py`:

```python
"""wahoo-dashboard: run the FTMS service, the CPS logger and the dashboard as one session.

Start-up: FTMS (wait for its port) -> CPS logger -> dashboard. Any child exiting, the window
closing, or Ctrl-C shuts the rest down in the order dashboard -> FTMS -> CPS logger.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Sequence

from wahoo_lsl.config import ConfigError
from wahoo_lsl.ftms.config import load_ftms_config

from .metrics import DEFAULT_CIRCUMFERENCE_M

WIN = sys.platform == "win32"
SHUTDOWN_ORDER = ("dash", "ftms", "cps")


class LauncherError(Exception):
    pass


@dataclass(frozen=True)
class Timings:
    port_wait_s: float = 15.0
    cps_early_exit_s: float = 2.0
    grace_s: float = 20.0
    kill_after_s: float = 5.0
    poll_s: float = 0.2


def find_script(name: str, scripts_dir: str | Path | None = None) -> Path | None:
    """An installed console script in this environment's scripts folder (never a PATH search)."""
    d = Path(scripts_dir if scripts_dir is not None else sysconfig.get_path("scripts"))
    for candidate in (f"{name}.exe", name) if WIN else (name,):
        path = d / candidate
        if path.is_file():
            return path
    return None


def build_commands(args: argparse.Namespace, host: str, port: int,
                   scripts_dir: str | Path | None = None) -> dict[str, list[str]]:
    ftms, cps = find_script("wahoo-ftms", scripts_dir), find_script("wahoo-lsl", scripts_dir)
    missing = [name for name, path in (("wahoo-ftms", ftms), ("wahoo-lsl", cps)) if path is None]
    if missing:
        where = scripts_dir if scripts_dir is not None else sysconfig.get_path("scripts")
        raise LauncherError(f"{', '.join(missing)} not found in {where}; run `pip install -e .` in this environment")
    debug = ["--debug"] if args.debug else []
    return {
        "ftms": [str(ftms), "serve", "-c", str(args.ftms_config), *debug],
        "cps": [str(cps), "run", "-c", str(args.cps_config), *debug],
        "dash": [sys.executable, "-m", "wahoo_lsl.dashboard.app", "--host", host, "--port", str(port),
                 "--log-dir", str(args.log_dir), "--max-watts", str(args.max_watts),
                 "--circumference-m", str(args.circumference_m)],
    }


class Child:
    """One child process; its merged output is relayed line by line with a [label] prefix."""

    def __init__(self, label: str, argv: Sequence[str], out: IO[str], out_lock: threading.Lock) -> None:
        self.label = label
        # POSIX: own session, so a terminal Ctrl-C reaches only the wrapper (ordered shutdown).
        # Windows: own process group, so CTRL_BREAK_EVENT can target it.
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
        self.proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"}, **group,
        )
        self._relay = threading.Thread(target=self._pump, args=(out, out_lock), name=f"relay-{label}", daemon=True)
        self._relay.start()

    def _pump(self, out: IO[str], lock: threading.Lock) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with lock:
                out.write(f"[{self.label}] {line.rstrip(chr(13) + chr(10))}\n")
                out.flush()

    def poll(self) -> int | None:
        return self.proc.poll()

    def stop(self, grace_s: float, kill_after_s: float, hurry: threading.Event) -> int:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.CTRL_BREAK_EVENT if WIN else signal.SIGINT)
            except OSError:
                pass  # already gone
            deadline = time.monotonic() + grace_s
            while self.proc.poll() is None and time.monotonic() < deadline and not hurry.is_set():
                time.sleep(0.05)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(kill_after_s)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self._relay.join(2.0)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        return self.proc.returncode


class Supervisor:
    def __init__(self, commands: dict[str, list[str]], host: str, port: int, out: IO[str],
                 stop: threading.Event, hurry: threading.Event, timings: Timings = Timings()) -> None:
        self.commands, self.host, self.port, self.out = commands, host, port, out
        self.stop_event, self.hurry, self.t = stop, hurry, timings
        self._lock = threading.Lock()
        self.children: dict[str, Child] = {}
        self.code = 0

    def run(self) -> int:
        try:
            self._start_and_watch()
        finally:
            for label in SHUTDOWN_ORDER:
                child = self.children.get(label)
                if child is not None:
                    grace = 0.0 if self.hurry.is_set() else self.t.grace_s
                    self._note(child.stop(grace, self.t.kill_after_s, self.hurry))
        return self.code

    def _say(self, msg: str) -> None:
        with self._lock:
            self.out.write(f"[wrapper] {msg}\n")
            self.out.flush()

    def _note(self, returncode: int | None) -> None:
        if self.code == 0 and returncode:
            self.code = returncode if returncode > 0 else 1  # killed by a signal -> 1

    def _start(self, label: str) -> Child:
        child = self.children[label] = Child(label, self.commands[label], self.out, self._lock)
        return child

    def _start_and_watch(self) -> None:
        ftms = self._start("ftms")
        ready = self._wait_for_port(ftms)
        if ready == "exited":
            self._say(f"FTMS service exited with code {ftms.poll()} during start-up")
            self._note(ftms.poll())
            return
        if ready == "timeout":
            self._say(f"FTMS service did not open {self.host}:{self.port} within {self.t.port_wait_s:g} s")
            self._note(2)
            return
        if ready == "stopped":
            return
        cps = self._start("cps")
        deadline = time.monotonic() + self.t.cps_early_exit_s
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if cps.poll() is not None:
                self._say(f"CPS logger exited with code {cps.poll()} during start-up")
                self._note(cps.poll())
                return
            self.stop_event.wait(self.t.poll_s)
        if self.stop_event.is_set():
            return
        self._start("dash")
        while not self.stop_event.is_set():
            for label, child in self.children.items():
                returncode = child.poll()
                if returncode is not None:
                    self._say(f"{label} exited with code {returncode}; shutting down")
                    self._note(returncode)
                    return
            self.stop_event.wait(self.t.poll_s)
        self._say("interrupted; shutting down")

    def _wait_for_port(self, ftms: Child) -> str:
        deadline = time.monotonic() + self.t.port_wait_s
        while True:
            if ftms.poll() is not None:
                return "exited"
            if self.stop_event.is_set():
                return "stopped"
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return "ready"
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return "timeout"
            self.stop_event.wait(self.t.poll_s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo-dashboard",
                                description="Run the FTMS service, the CPS logger and the test dashboard together")
    p.add_argument("--ftms-config", type=Path, default=Path("ftms.json"), help="FTMS config (default ftms.json)")
    p.add_argument("--cps-config", type=Path, default=Path("config.json"), help="CPS logger config (default config.json)")
    p.add_argument("--log-dir", type=Path, default=Path("logs"), help="CPS log folder the dashboard follows (default logs)")
    p.add_argument("--debug", action="store_true", help="pass --debug to both services")
    p.add_argument("--max-watts", type=int, default=1000, help="dashboard slider maximum in W (default 1000)")
    p.add_argument("--circumference-m", type=float, default=DEFAULT_CIRCUMFERENCE_M,
                   help=f"wheel circumference for speed (default {DEFAULT_CIRCUMFERENCE_M})")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_ftms_config(args.ftms_config).ftms
        commands = build_commands(args, settings.host, settings.port)
    except (ConfigError, LauncherError) as e:
        print(f"wahoo-dashboard: {e}", file=sys.stderr)
        return 2
    stop, hurry = threading.Event(), threading.Event()

    def on_signal(signum: int, frame: object) -> None:
        (hurry if stop.is_set() else stop).set()  # a second interrupt skips the grace periods

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, on_signal)
    return Supervisor(commands, settings.host, settings.port, sys.stdout, stop, hurry).run()


if __name__ == "__main__":
    raise SystemExit(main())
```

`pyproject.toml` — add one line to `[project.scripts]`, after `wahoo-ftms`:

```toml
wahoo-dashboard = "wahoo_lsl.dashboard.launcher:main"
```

Then reinstall so the script exists: `conda run -n wahoo pip install -e . -q`.

- [ ] **Step 4: Run to verify pass**

Run: `conda run -n wahoo python -m pytest -q -W error tests/test_dashboard_launcher.py` (3 times — process timing must be stable), then the full suite `conda run -n wahoo python -m pytest -q -W error`.
Expected: all pass. Also `conda run -n wahoo wahoo-dashboard --help` prints usage.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/dashboard/launcher.py tests/dashboard_fake_child.py tests/test_dashboard_launcher.py pyproject.toml
git commit -m "feat(dashboard): wahoo-dashboard launcher with ordered start-up and shutdown

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: README

**Files:**
- Modify: `README.md` (append a new section at the end; change nothing else)

**Interfaces:**
- Consumes: the commands and behaviour from Tasks 4–5.

- [ ] **Step 1: Append this section to `README.md`**

````markdown
## Test dashboard (`wahoo-dashboard`)

A small Tk window for testing the FTMS control API and the CPS logger together. It uses
only the public pieces: `KickrClient` for setting and reading the target, and the CPS
JSONL log files for power, speed and cadence.

```bash
pip install -e .            # once, so the wahoo-dashboard command exists
wahoo-dashboard             # uses ftms.json, config.json and logs/ in the current folder
wahoo-dashboard --ftms-config my-ftms.json --cps-config my-cps.json --log-dir logs --debug
```

`wahoo-dashboard` starts `wahoo-ftms serve`, waits for its port, starts `wahoo-lsl run`,
then opens the dashboard. Output from all three is shown with `[ftms]`, `[cps]` and
`[dash]` prefixes. Closing the window, pressing Ctrl-C, or any of the three exiting stops
the rest in the order dashboard → FTMS service → CPS logger (press Ctrl-C again to skip
the waits). The exit code is the first non-zero exit among the three, or 2 for a
config or start-up problem.

To open only the window against a service and logger that are already running:

```bash
python -m wahoo_lsl.dashboard.app --log-dir logs [--port 47610] [--max-watts 1000] [--circumference-m 2.105]
```

- **Slider:** sends the target as you drag (the service applies at most 2 changes per
  second, latest wins). "0 W" drops it straight to zero. When the dashboard (re)connects
  the slider resets to 0, matching the service's spin-down.
- **Target panel:** requested and confirmed target read back from the service, link state
  and last error, with a graph of the confirmed target.
- **CPS panels:** power, speed and cadence with graphs. Speed and cadence are calculated
  from the wheel and crank revolution counters; speed assumes a 2.105 m wheel unless you
  pass `--circumference-m`.
- **Graphs:** fill left to right over 5 minutes, then all clear and start again.
- **Lag:** the logger writes to disk every `log.flush_every_n` samples, so CPS values can
  arrive several seconds late (points still land at the time they were measured). Set
  `"flush_every_n": 1` in the logger config for a livelier display.
````

- [ ] **Step 2: Verify and commit**

Run: `conda run -n wahoo python -m pytest -q -W error` (unchanged, all pass).

```bash
git add README.md
git commit -m "docs: README section for the test dashboard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
