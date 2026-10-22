# CPS LSL Outlet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `wahoo-lsl run` broadcasts CPS data (configured measurements + derived speed/cadence) as a regular LSL stream, with derivations computed once and shared by the JSONL log and LSL.

**Architecture:** The dispatcher derives speed/cadence once per sample (`derive.CpsDeriver`) and attaches them to `Sample.derived`. A new `LslSink` runs in the existing pipeline on its own thread; `SinkRunner` drains its queue in batches and the sink pushes each batch as one `push_chunk`. The outlet is created on the `features` event (identity from the `connected` event, which now carries Device Information Service fields).

**Tech Stack:** Python ≥ 3.10, `pylsl` 1.18 (existing dependency), `bleak` (existing), pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-cps-lsl-outlet-design.md`

## Global Constraints

- Branch `feat/lsl` (already created off `main`; spec committed as ec022dd). Work in the repo root; do not create worktrees.
- Run tests with: `conda run -n wahoo python -m pytest -q -W error` (all tests must pass, no warnings).
- No new dependencies. `pylsl` is only imported in `clock.py`, `lsl.py`, `sinks/lsl_sink.py` and tests.
- Never add, modify or delete the user's untracked `config.json`, `config.json~`, `ftms.json`, or anything in `logs/`.
- Never run `wahoo-lsl run`, `wahoo-lsl list-features`, `wahoo-lsl scan`, `wahoo-ftms` or `wahoo-dashboard` (they use real Bluetooth hardware). Tests use fakes.
- Commit after each task; every commit message ends with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `lsl.enabled` defaults to `true`; `lsl.nominal_srate` defaults to `1.0` (0 = irregular); `derive.wheel_circumference_m` defaults to `2.105`.
- Stream `source_id` = stream `name` = `<MODEL>-CPS-<id6>`; type `CPS`; format `double64`.
- An LSL failure must never stop the JSONL log.
- Match surrounding style: `from __future__ import annotations`, module docstrings, sparse comments.

## Review Focus

1. **Reconnect within one batch** — `connected`, `features` and the new connection's samples can arrive in the same drained batch; rows must go to the right outlet, in order (Task 7 test `test_outlet_replaced_on_change_flushes_old_rows_first`).
2. **Device without DIS, or a DIS read that hangs/fails** — connecting must still succeed, delayed at most 2 s per characteristic (Task 5 tests `test_device_information_read_failure_skips_field`, `test_device_information_read_timeout_skips_field`).
3. **Existing user config with no `lsl`/`derive` sections** — must load unchanged with LSL on at 1 Hz (Task 4 test `test_defaults_applied`).
4. **Configured measurement the device does not support / notification fields not configured** — no channel for the former; the latter not pushed (Task 8 test `test_run_session_streams_to_lsl`).
5. **Outlet creation fails (liblsl/network)** — the session and its JSONL log continue (Task 8 test `test_run_session_lsl_failure_keeps_logging`).

---

### Task 1: Shared derivation module `derive.py`

**Files:**
- Create: `src/wahoo_lsl/derive.py`
- Test: `tests/test_derive.py`

**Interfaces:**
- Produces: `CpsDeriver(circumference_m: float = DEFAULT_CIRCUMFERENCE_M)` with `.circumference_m` (property), `.reset()`, `.update(t_lsl: float, fields: dict) -> dict[str, float | None]` returning exactly `{"speed_kmh": ..., "cadence_rpm": ...}`. Constants `DEFAULT_CIRCUMFERENCE_M = 2.105`, `ZERO_AFTER_S = 3.0`, `WHEEL_REV_WRAP = 1 << 32`, `CRANK_REV_WRAP = 1 << 16`, `EVENT_T_WRAP = 1 << 16`, `WHEEL_TICKS_PER_S = 2048`, `CRANK_TICKS_PER_S = 1024`.

- [ ] **Step 1: Write the failing tests** — `tests/test_derive.py`:

```python
import math

import pytest

from wahoo_lsl.derive import DEFAULT_CIRCUMFERENCE_M, CpsDeriver


def fields(wheel=None, crank=None, **extra):
    out = dict(extra)
    if wheel is not None:
        out["wheel_revs"], out["wheel_event_t"] = wheel
    if crank is not None:
        out["crank_revs"], out["crank_event_t"] = crank
    return out


def test_first_sample_primes_only():
    d = CpsDeriver()
    assert d.update(1.0, fields(wheel=(10, 0), crank=(5, 0))) == {"speed_kmh": None, "cadence_rpm": None}


def test_speed_and_cadence_from_deltas():
    d = CpsDeriver()
    d.update(1.0, fields(wheel=(10, 0), crank=(5, 0)))
    out = d.update(2.0, fields(wheel=(11, 2048), crank=(6, 1024)))
    assert out["speed_kmh"] == pytest.approx(DEFAULT_CIRCUMFERENCE_M * 3.6)  # 1 rev/s
    assert out["cadence_rpm"] == pytest.approx(60.0)


def test_custom_circumference():
    d = CpsDeriver(circumference_m=2.0)
    assert d.circumference_m == 2.0
    d.update(1.0, fields(wheel=(0, 0)))
    assert d.update(2.0, fields(wheel=(2, 2048)))["speed_kmh"] == pytest.approx(2 * 2.0 * 3.6)


def test_counter_and_event_time_wrap():
    d = CpsDeriver()
    d.update(1.0, fields(wheel=(2**32 - 1, 65000), crank=(65535, 65000)))
    out = d.update(2.0, fields(wheel=(1, 1000), crank=(0, 488)))
    # wheel: 2 revs in (1000 - 65000) % 65536 = 1536 ticks of 1/2048 s
    assert out["speed_kmh"] == pytest.approx(2 * 2048 / 1536 * DEFAULT_CIRCUMFERENCE_M * 3.6)
    # crank: 1 rev in (488 - 65000) % 65536 = 1024 ticks of 1/1024 s
    assert out["cadence_rpm"] == pytest.approx(60.0)


def test_no_new_revolution_holds_then_zeroes_after_3s():
    d = CpsDeriver()
    d.update(0.0, fields(crank=(0, 0)))
    assert d.update(1.0, fields(crank=(1, 1024)))["cadence_rpm"] == pytest.approx(60.0)
    assert d.update(2.0, fields(crank=(1, 1024)))["cadence_rpm"] == pytest.approx(60.0)  # held
    assert d.update(3.9, fields(crank=(1, 1024)))["cadence_rpm"] == pytest.approx(60.0)  # 2.9 s: held
    assert d.update(4.0, fields(crank=(1, 1024)))["cadence_rpm"] == 0.0  # 3 s since the last change


def test_stationary_from_start_reads_zero_after_3s():
    d = CpsDeriver()
    d.update(0.0, fields(wheel=(0, 0)))
    assert d.update(2.0, fields(wheel=(0, 0)))["speed_kmh"] is None
    assert d.update(3.0, fields(wheel=(0, 0)))["speed_kmh"] == 0.0


def test_revs_without_event_time_change_is_not_an_event():
    d = CpsDeriver()
    d.update(0.0, fields(crank=(0, 100)))
    assert d.update(1.0, fields(crank=(3, 100)))["cadence_rpm"] is None


def test_missing_fields_give_none_and_reprime():
    d = CpsDeriver()
    d.update(0.0, fields(wheel=(0, 0)))
    assert d.update(1.0, fields(power_w=100)) == {"speed_kmh": None, "cadence_rpm": None}
    assert d.update(2.0, fields(wheel=(5, 2048)))["speed_kmh"] is None  # re-primed, not a 5-rev delta


def test_non_int_fields_are_ignored():
    d = CpsDeriver()
    assert d.update(1.0, {"wheel_revs": 1.5, "wheel_event_t": 0, "crank_revs": True, "crank_event_t": 0}) == {
        "speed_kmh": None,
        "cadence_rpm": None,
    }


def test_reset_forgets_previous_counters():
    d = CpsDeriver()
    d.update(0.0, fields(wheel=(1000, 0)))
    d.reset()
    assert d.update(1.0, fields(wheel=(0, 2048)))["speed_kmh"] is None


@pytest.mark.parametrize("c", [0, -1, math.nan, math.inf, True])
def test_invalid_circumference_rejected(c):
    with pytest.raises(ValueError):
        CpsDeriver(circumference_m=c)
```

- [ ] **Step 2: Run to verify failure**

Run: `conda run -n wahoo python -m pytest tests/test_derive.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.derive'`.

- [ ] **Step 3: Implement** — `src/wahoo_lsl/derive.py`:

```python
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
        if d_revs and d_ticks:
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
```

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest tests/test_derive.py -q -W error` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/derive.py tests/test_derive.py
git commit -m "feat: shared CPS speed/cadence derivation module

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Dispatcher derives once; log and console carry `derived`

**Files:**
- Modify: `src/wahoo_lsl/pipeline.py` (`Sample`, `Dispatcher.__init__`, `Dispatcher._on_event`, `Dispatcher._process`, `Pipeline.__init__`)
- Modify: `src/wahoo_lsl/sinks/log_sink.py` (`sample_record`)
- Modify: `src/wahoo_lsl/sinks/console_sink.py` (`handle`)
- Test: `tests/test_pipeline.py`, `tests/test_sinks.py`

**Interfaces:**
- Consumes: `CpsDeriver` (Task 1).
- Produces: `Sample(seq, t_lsl, t_wall_ns, raw_hex, flags, fields, parse_error=None, derived={})` — `derived` is the new **last** field (default empty dict). `Dispatcher(..., *, queue_warn_depth=100, backlog_interval_s=10.0, deriver: CpsDeriver | None = None)`; `Pipeline(sinks, configured, warn, *, queue_warn_depth=100, deriver: CpsDeriver | None = None)`. Log records gain `"derived"`.

- [ ] **Step 1: Write failing tests.** In `tests/test_pipeline.py` add after the `TRUNCATED_WHEEL` constant:

```python
WHEEL_NEXT = struct.pack("<Hh", 1 << 4, 150) + struct.pack("<IH", 11, 4096)  # +1 rev in 1 s
```

and add `from wahoo_lsl.derive import CpsDeriver` to the imports. Replace the `run_pipeline` helper with one that accepts a deriver:

```python
def run_pipeline(items, sinks, configured=("power",), deriver=None):
    warn = WarnOnce(logging.getLogger("wahoo_lsl.test"))
    p = Pipeline(sinks, configured, warn, deriver=deriver)
    p.start()
    for item in items:
        p.put(item)
    assert p.close(timeout=5) is True
    return p
```

Append these tests:

```python
def connected_event() -> Event:
    return Event("connected", 99.5, 1_700_000_000_000_000_000, {"name": "KICKR", "address": "X"})


def test_samples_carry_derived_metrics():
    sink = RecordingSink()
    run_pipeline([raw(0, WHEEL), raw(1, WHEEL_NEXT)], [sink], deriver=CpsDeriver(2.0))
    s0, s1 = samples(sink)
    assert s0.derived == {"speed_kmh": None, "cadence_rpm": None}
    assert s1.derived["speed_kmh"] == pytest.approx(2.0 * 3.6)
    assert s1.derived["cadence_rpm"] is None


def test_connected_event_resets_derivation():
    sink = RecordingSink()
    run_pipeline([raw(0, WHEEL), connected_event(), raw(1, WHEEL_NEXT)], [sink])
    assert samples(sink)[1].derived["speed_kmh"] is None  # re-primed, no delta across the reconnect


def test_parse_error_sample_has_empty_derived():
    sink = RecordingSink()
    run_pipeline([raw(0, TRUNCATED_WHEEL)], [sink])
    assert samples(sink)[0].derived == {}
```

In `tests/test_sinks.py` change `test_sample_record_shape` to expect `"derived": {}` as the last key of the dict, and add:

```python
def test_sample_record_includes_derived():
    s = Sample(0, 1.0, 1_700_000_000_123_456_789, "", 0, {}, None, {"speed_kmh": 30.5, "cadence_rpm": None})
    assert sample_record(s)["derived"] == {"speed_kmh": 30.5, "cadence_rpm": None}


def test_console_sink_prints_derived():
    out = io.StringIO()
    s = Sample(7, 1.0, 0, "", 0, {"power_w": 1}, None, {"speed_kmh": 30.54, "cadence_rpm": None})
    ConsoleSink(out).handle(s)
    assert out.getvalue().rstrip("\n").endswith("power_w=1 speed_kmh=30.5 cadence_rpm=None")
```

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_pipeline.py tests/test_sinks.py -q -W error` → FAIL (`unexpected keyword argument 'deriver'`, `Sample` has no `derived`).

- [ ] **Step 3: Implement.**

`pipeline.py`: add `from .derive import CpsDeriver` to the imports. Change `Sample`:

```python
@dataclass(frozen=True, slots=True)
class Sample:
    seq: int
    t_lsl: float
    t_wall_ns: int
    raw_hex: str
    flags: int | None  # None if the payload was too short to contain flags
    fields: dict  # ALL parsed output keys (no config filtering in MVP)
    parse_error: str | None = None
    derived: dict = field(default_factory=dict)  # derive.CpsDeriver output; {} on parse errors
```

Update the module docstring's first diagram line to: `BLE callback -> ingest_q -> Dispatcher (parse, derive, warn, fan-out) -> one queue + thread per Sink.`

`Dispatcher.__init__`: add keyword parameter `deriver: CpsDeriver | None = None` after `backlog_interval_s`, and store `self._deriver = deriver if deriver is not None else CpsDeriver()`. Update the class docstring to `"""Parses RawSamples, derives speed/cadence, applies warn-once checks, fans out to sink queues."""`.

`Dispatcher._on_event`: add as first statement:

```python
        if event.kind == "connected":
            self._deriver.reset()  # precedes every connection's first sample; never derive across a link drop
```

`Dispatcher._process`: change the final return to:

```python
        derived = self._deriver.update(raw.t_lsl, m.values)
        return Sample(raw.seq, raw.t_lsl, raw.t_wall_ns, raw_hex, m.flags, dict(m.values), None, derived)
```

`Pipeline.__init__`: add keyword `deriver: CpsDeriver | None = None` and pass `deriver=deriver` to `Dispatcher(...)`.

`sinks/log_sink.py` `sample_record`: add `"derived": s.derived,` after `"fields": s.fields,`.

`sinks/console_sink.py` `handle`, sample branch, replace the `fields = ...` line with:

```python
            values = [f"{k}={v}" for k, v in item.fields.items()]
            values += [f"{k}={v:.1f}" if v is not None else f"{k}=None" for k, v in item.derived.items()]
            fields = " ".join(values)
```

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/pipeline.py src/wahoo_lsl/sinks/log_sink.py src/wahoo_lsl/sinks/console_sink.py tests/test_pipeline.py tests/test_sinks.py
git commit -m "feat: derive speed/cadence once in the dispatcher; log derived values

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Batched sink delivery

**Files:**
- Modify: `src/wahoo_lsl/pipeline.py` (`Sink`, `SinkRunner`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `Sink.batched: bool = False` class attribute and `Sink.handle_batch(items: list) -> None` (default: `handle` each in order). `SinkRunner` drains its queue after each blocking `get()`; a batched sink receives the whole drained list in one `handle_batch` call; a non-batched sink gets `handle` per item with per-item error isolation (exactly as today).

- [ ] **Step 1: Write failing tests** — append to `tests/test_pipeline.py` (add `SinkRunner` to the `wahoo_lsl.pipeline` import list):

```python
class BatchSink(Sink):
    name = "batch"
    batched = True

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list] = []
        self.fail = fail
        self.stopped = False

    def handle(self, item) -> None:
        raise AssertionError("batched sinks receive handle_batch")

    def handle_batch(self, items) -> None:
        self.batches.append(list(items))
        if self.fail:
            raise RuntimeError("batch boom")

    def stop(self) -> None:
        self.stopped = True


def run_runner(sink, items):
    q: queue.Queue = queue.Queue()
    for item in items:
        q.put(item)
    SinkRunner(sink, q, WarnOnce(logging.getLogger("wahoo_lsl.test"))).run()  # synchronous
    return q


def test_runner_delivers_queued_items_as_one_batch():
    sink = BatchSink()
    run_runner(sink, ["a", "b", "c", STOP])
    assert sink.batches == [["a", "b", "c"]]
    assert sink.stopped


def test_runner_stop_mid_drain_processes_earlier_items_only():
    sink = BatchSink()
    q = run_runner(sink, ["a", STOP, "late"])
    assert sink.batches == [["a"]]
    assert sink.stopped
    assert q.get_nowait() == "late"  # never consumed


def test_failing_batch_is_warned_once_and_runner_continues(caplog):
    sink = BatchSink(fail=True)
    q: queue.Queue = queue.Queue()
    runner = SinkRunner(sink, q, WarnOnce(logging.getLogger("wahoo_lsl.test")))
    with caplog.at_level(logging.ERROR):
        runner.start()
        q.put("a")
        for _ in range(200):  # wait until the first batch is consumed
            if sink.batches:
                break
            threading.Event().wait(0.01)
        q.put("b")
        q.put(STOP)
        runner.join(5)
    assert [x for batch in sink.batches for x in batch] == ["a", "b"]
    assert len([r for r in caplog.records if "batch" in r.getMessage()]) == 1


def test_unbatched_sink_errors_stay_per_item():
    class FailSecond(RecordingSink):
        def handle(self, item) -> None:
            if item == "b":
                raise RuntimeError("only b")
            super().handle(item)

    sink = FailSecond()
    run_runner(sink, ["a", "b", "c", STOP])
    assert sink.items == ["a", "c"]
```

Add `import threading` to the test module imports.

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_pipeline.py -q -W error` → FAIL (`BatchSink` receives `handle` → AssertionError logged, batches empty).

- [ ] **Step 3: Implement** in `pipeline.py`.

In `class Sink`, after `name: str = "sink"`:

```python
    batched: bool = False  # True: the runner passes each drained queue batch to handle_batch()
```

and after `handle`:

```python
    def handle_batch(self, items: list[Sample | Event]) -> None:
        """Process the items drained from the queue in one go (batched sinks only)."""
        for item in items:
            self.handle(item)
```

Replace `SinkRunner.run` and add helpers:

```python
    def run(self) -> None:
        started = True
        try:
            self.sink.start()
        except Exception:
            log.exception("Sink %s failed to start; its data will be discarded", self.sink.name)
            started = False
        while True:
            batch, stopping = self._next_batch()
            if started and batch:
                self._deliver(batch)
            if stopping:
                break
        if started:
            try:
                self.sink.stop()
            except Exception:
                log.exception("Sink %s failed to stop cleanly", self.sink.name)

    def _next_batch(self) -> tuple[list, bool]:
        """Block for one item, then take whatever else is already queued. True once STOP is seen."""
        batch: list = []
        item = self.queue.get()
        while item is not STOP:
            batch.append(item)
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return batch, False
        return batch, True

    def _deliver(self, batch: list) -> None:
        if self.sink.batched:
            self._call(self.sink.handle_batch, batch)
        else:
            for item in batch:
                self._call(self.sink.handle, item)

    def _call(self, fn: Callable[[Any], None], arg: Any) -> None:
        try:
            fn(arg)
        except Exception as e:
            if self._warn.check(("sink_error", self.sink.name, type(e).__name__)):
                log.exception(
                    "Sink %s failed handling a batch; further %s errors suppressed"
                    if self.sink.batched
                    else "Sink %s failed handling an item; further %s errors suppressed",
                    self.sink.name,
                    type(e).__name__,
                )
```

Add `from collections.abc import Callable, Sequence` (extend the existing `Sequence` import) and `from typing import Any`. Update the `SinkRunner` docstring to `"""Feeds one sink from its queue until STOP, in drained batches. A failing sink never blocks the dispatcher."""`.

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS (existing `test_failing_sink_does_not_block_others` still expects one ERROR containing "broken").

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/pipeline.py tests/test_pipeline.py
git commit -m "feat: sink runners drain queues in batches; opt-in handle_batch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `lsl` and `derive` config sections

**Files:**
- Modify: `src/wahoo_lsl/config.py`
- Modify: `config.example.json`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `derive.DEFAULT_CIRCUMFERENCE_M` (Task 1).
- Produces: `LslConfig(enabled: bool = True, nominal_srate: float = 1.0)`, `DeriveConfig(wheel_circumference_m: float = 2.105)`; `Config.lsl: LslConfig`, `Config.derive: DeriveConfig` (both with defaults).

- [ ] **Step 1: Write failing tests.** In `tests/test_config.py`:

Import `DeriveConfig, LslConfig` from `wahoo_lsl.config`. In `test_example_config_loads` add:

```python
    assert cfg.lsl == LslConfig(enabled=True, nominal_srate=1.0)
    assert cfg.derive == DeriveConfig(wheel_circumference_m=2.105)
```

In `test_defaults_applied` add the same two asserts. Add to the `test_invalid_configs_rejected` parameter list:

```python
        (_set("lsl", []), "'lsl' must be an object"),
        (_set("lsl.extra", 1), "unknown key"),
        (_set("lsl.enabled", "yes"), "lsl.enabled"),
        (_set("lsl.nominal_srate", -1), "lsl.nominal_srate"),
        (_set("lsl.nominal_srate", True), "lsl.nominal_srate"),
        (_set("lsl.nominal_srate", float("inf")), "lsl.nominal_srate"),
        (_set("derive.wheel_circumference_m", 0), "wheel_circumference_m"),
        (_set("derive.wheel_circumference_m", float("nan")), "wheel_circumference_m"),
        (_set("derive.wheel_circumference_m", float("inf")), "wheel_circumference_m"),
        (_set("derive.spoke_count", 32), "unknown key"),
```

and append:

```python
def test_lsl_and_derive_values():
    d = base()
    d["lsl"] = {"enabled": False, "nominal_srate": 0}
    d["derive"] = {"wheel_circumference_m": 2.096}
    cfg = parse_config(d)
    assert cfg.lsl == LslConfig(enabled=False, nominal_srate=0.0)
    assert cfg.derive == DeriveConfig(wheel_circumference_m=2.096)
```

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_config.py -q -W error` → FAIL (ImportError `LslConfig`).

- [ ] **Step 3: Implement** in `config.py`:

Add `import math` and `from .derive import DEFAULT_CIRCUMFERENCE_M`. Change `_TOP_KEYS` to `{"device", "measurements", "log", "reconnect", "queue_warn_depth", "lsl", "derive"}`. Add dataclasses after `ReconnectConfig`:

```python
@dataclass(frozen=True)
class LslConfig:
    enabled: bool = True
    nominal_srate: float = 1.0  # 0 = irregular


@dataclass(frozen=True)
class DeriveConfig:
    wheel_circumference_m: float = DEFAULT_CIRCUMFERENCE_M
```

Add to `Config` (after `queue_warn_depth`):

```python
    lsl: LslConfig = field(default_factory=LslConfig)
    derive: DeriveConfig = field(default_factory=DeriveConfig)
```

In `parse_config`, add to the `Config(...)` call:

```python
        lsl=_parse_lsl(section(obj, "lsl", {"enabled", "nominal_srate"})),
        derive=_parse_derive(section(obj, "derive", {"wheel_circumference_m"})),
```

Add the parsers after `_parse_log`:

```python
def _parse_lsl(d: dict) -> LslConfig:
    enabled = d.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("lsl.enabled must be true or false")
    srate = d.get("nominal_srate", 1.0)
    if not _is_number(srate) or not math.isfinite(srate) or srate < 0:
        raise ConfigError(f"lsl.nominal_srate must be a finite number >= 0 (0 = irregular), got {srate!r}")
    return LslConfig(enabled=enabled, nominal_srate=float(srate))


def _parse_derive(d: dict) -> DeriveConfig:
    where = "derive.wheel_circumference_m"
    circumference = pos_number(d.get("wheel_circumference_m", DEFAULT_CIRCUMFERENCE_M), where)
    if not math.isfinite(circumference):
        raise ConfigError(f"{where} must be a finite positive number, got {circumference!r}")
    return DeriveConfig(wheel_circumference_m=circumference)
```

(`pos_number` lets NaN and infinity through — `nan <= 0` is False — so the `isfinite` check rejects them.)

`config.example.json`: add, after `"queue_warn_depth": 100` (add a comma to that line):

```json
  "lsl": {
    "enabled": true,
    "nominal_srate": 1.0
  },
  "derive": {
    "wheel_circumference_m": 2.105
  }
```

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/config.py config.example.json tests/test_config.py
git commit -m "feat: lsl and derive config sections

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Device Information Service read

**Files:**
- Modify: `src/wahoo_lsl/cps.py` (DIS UUID constants)
- Modify: `src/wahoo_lsl/ble.py` (`DeviceInfo`, `connect_and_read`, `_open_session`, new `_read_device_information`, `DIS_READ_TIMEOUT_S`)
- Modify: `src/wahoo_lsl/cli.py` (`format_features`)
- Modify: `tests/fakes.py` (`FakeServices`, `FakeClient.read_gatt_char`, `FakeBackend`)
- Test: `tests/test_ble.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `cps.DEVICE_INFORMATION_SERVICE_UUID`, `cps.DEVICE_INFORMATION_FIELDS: dict[str, str]` (char UUID → key, in order manufacturer, model_number, serial_number, hardware_revision, firmware_revision, software_revision). `DeviceInfo.device_info: dict[str, str]` (default `{}`). The `connected` event detail gains `"device_info": {...}`. `FakeBackend(device_info: dict[str, bytes] | None = None, dis_fail=(), dis_hang=())` — keys are characteristic UUIDs; `None` means the service is absent.

- [ ] **Step 1: Write failing tests.**

`tests/fakes.py`: import `DEVICE_INFORMATION_SERVICE_UUID` from `wahoo_lsl.cps`. Replace `FakeServices` and add `FakeDisService`:

```python
class FakeDisService:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get_characteristic(self, uuid: str):
        return SimpleNamespace(uuid=uuid) if uuid in self._values else None


class FakeServices:
    def __init__(self, backend: "FakeBackend") -> None:
        self._backend = backend

    def get_service(self, uuid: str):
        if uuid == CPS_SERVICE_UUID and self._backend.has_cps:
            return FakeService()
        if uuid == DEVICE_INFORMATION_SERVICE_UUID and self._backend.device_info is not None:
            return FakeDisService(self._backend.device_info)
        return None

    def get_characteristic(self, uuid: str):
        if uuid == SENSOR_LOCATION_UUID:  # CSC + CPS both carry Sensor Location, as on real trainers
            raise BleakError("Multiple Characteristics with this UUID")
        return None
```

In `FakeClient.__init__` change to `self.services = FakeServices(backend)`. In `FakeClient.read_gatt_char`, before the final `raise`:

```python
        dis = self.backend.device_info or {}
        if uuid in dis:
            if uuid in self.backend.dis_hang:
                await asyncio.Event().wait()
            if uuid in self.backend.dis_fail:
                raise BleakError("simulated read failure")
            return bytearray(dis[uuid])
```

`FakeBackend.__init__`: add keyword params `device_info=None, dis_fail=(), dis_hang=()` and store `self.device_info = device_info`, `self.dis_fail = set(dis_fail)`, `self.dis_hang = set(dis_hang)`.

Append to `tests/test_ble.py` (import `DEVICE_INFORMATION_FIELDS` from `wahoo_lsl.cps`):

```python
DIS_UUID = {key: uuid for uuid, key in DEVICE_INFORMATION_FIELDS.items()}
DIS_VALUES = {DIS_UUID["manufacturer"]: b"Wahoo Fitness", DIS_UUID["model_number"]: b"KICKR V6\x00 "}


def test_read_info_reads_device_information():
    client, _ = make_client(FakeBackend(device_info=DIS_VALUES))
    info = asyncio.run(client.read_info())
    assert info.device_info == {"manufacturer": "Wahoo Fitness", "model_number": "KICKR V6"}


def test_device_information_absent_is_empty():
    client, _ = make_client(FakeBackend())
    assert asyncio.run(client.read_info()).device_info == {}


def test_device_information_empty_value_skipped():
    client, _ = make_client(FakeBackend(device_info={DIS_UUID["serial_number"]: b"\x00"}))
    assert asyncio.run(client.read_info()).device_info == {}


def test_device_information_read_failure_skips_field():
    backend = FakeBackend(device_info=DIS_VALUES, dis_fail={DIS_UUID["model_number"]})
    client, _ = make_client(backend)
    assert asyncio.run(client.read_info()).device_info == {"manufacturer": "Wahoo Fitness"}


def test_device_information_read_timeout_skips_field(monkeypatch):
    monkeypatch.setattr(ble_mod, "DIS_READ_TIMEOUT_S", 0.05)
    backend = FakeBackend(device_info=DIS_VALUES, dis_hang={DIS_UUID["manufacturer"]})
    client, _ = make_client(backend)
    assert asyncio.run(client.read_info()).device_info == {"model_number": "KICKR V6"}


def test_connected_event_carries_device_info():
    backend = FakeBackend(sessions=[([POWER], False)], device_info=DIS_VALUES)
    client, items = make_client(backend)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: len(raws(items)) == 1)
        stop.set()
        await task

    asyncio.run(scenario())
    connected = next(i for i in items if isinstance(i, Event) and i.kind == "connected")
    assert connected.detail["device_info"] == {"manufacturer": "Wahoo Fitness", "model_number": "KICKR V6"}
```

Append to `tests/test_cli.py`:

```python
def test_format_features_prints_device_information():
    info = DeviceInfo(
        "KICKR", "X", "uuid", parse_features(b"\x00\x00\x00\x00"), None, {"model_number": "KICKR V6"}
    )
    assert "Model number: KICKR V6" in format_features(info, []).splitlines()
```

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_ble.py tests/test_cli.py -q -W error` → FAIL (ImportError `DEVICE_INFORMATION_FIELDS`).

- [ ] **Step 3: Implement.**

`cps.py`, after `SENSOR_LOCATION_UUID`:

```python
DEVICE_INFORMATION_SERVICE_UUID = _uuid16(0x180A)
# Device Information Service string characteristics, in display order.
DEVICE_INFORMATION_FIELDS: dict[str, str] = {
    _uuid16(0x2A29): "manufacturer",
    _uuid16(0x2A24): "model_number",
    _uuid16(0x2A25): "serial_number",
    _uuid16(0x2A27): "hardware_revision",
    _uuid16(0x2A26): "firmware_revision",
    _uuid16(0x2A28): "software_revision",
}
```

`ble.py`: import `DEVICE_INFORMATION_FIELDS, DEVICE_INFORMATION_SERVICE_UUID` from `.cps`. After `SHUTDOWN_TIMEOUT_S` add:

```python
# Upper bound for each optional Device Information read, so a slow device never stalls connecting.
DIS_READ_TIMEOUT_S = 2.0
```

`DeviceInfo`: add last field `device_info: dict[str, str] = field(default_factory=dict)  # Device Information Service strings`.

In `connect_and_read`, inside the `try`, after the sensor-location block:

```python
            device_info = await _read_device_information(client)
```

and pass `device_info=device_info` to the `DeviceInfo(...)` constructor.

In `_open_session` change the `connected` emit to:

```python
            self._emit(
                make_event(
                    "connected",
                    name=info.name,
                    address=info.address,
                    address_kind=info.address_kind,
                    device_info=info.device_info,
                )
            )
```

Add the module-level helper before `_wait_first`:

```python
async def _read_device_information(client: Any) -> dict[str, str]:
    """Best-effort Device Information Service read; anything absent, empty or failing is skipped."""
    service = client.services.get_service(DEVICE_INFORMATION_SERVICE_UUID)
    if service is None:
        return {}
    out: dict[str, str] = {}
    for uuid, key in DEVICE_INFORMATION_FIELDS.items():
        char = service.get_characteristic(uuid)
        if char is None:
            continue
        try:
            raw = await asyncio.wait_for(client.read_gatt_char(char), DIS_READ_TIMEOUT_S)
        except COMMS_ERRORS as e:
            log.debug("Device Information %s not read: %r", key, e)
            continue
        text = bytes(raw).decode("utf-8", errors="replace").strip("\x00 \t\r\n")
        if text:
            out[key] = text
    return out
```

`cli.py` `format_features`: remove the `f"Feature bits (0x{f.raw:08X}):",` entry from the `lines = [...]` literal (leaving the Device and Sensor location lines), and directly after the literal add:

```python
    for key, value in info.device_info.items():
        lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    lines.append(f"Feature bits (0x{f.raw:08X}):")
```

The rest of the function is unchanged.

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/cps.py src/wahoo_lsl/ble.py src/wahoo_lsl/cli.py tests/fakes.py tests/test_ble.py tests/test_cli.py
git commit -m "feat: best-effort Device Information Service read on connect

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Channel vocabulary and generic LSL helpers

**Files:**
- Modify: `src/wahoo_lsl/cps.py` (`FieldInfo`, `FIELD_INFO`, `DERIVED_KEYS`)
- Create: `src/wahoo_lsl/lsl.py`
- Test: `tests/test_cps.py`, `tests/test_lsl.py`

**Interfaces:**
- Produces: `cps.FieldInfo(unit: str, description: str, encoding: dict | None = None)`; `cps.FIELD_INFO: dict[str, FieldInfo]` covering every key of every `MEASUREMENTS[*].keys` plus `speed_kmh`, `cadence_rpm`; `cps.DERIVED_KEYS = ("speed_kmh", "cadence_rpm")`. `lsl.Channel(label, unit, type, description="")`; `lsl.sanitize(text) -> str`; `lsl.add_dict(parent, name, mapping) -> XMLElement` (skips `None` values, `str()` of the rest); `lsl.add_channels(desc, channels)`; `lsl.stream_info(name, type_, source_id, channels, nominal_srate, *, fmt="double64", acquisition=None) -> pylsl.StreamInfo` (writes `desc/acquisition` with `software=wahoo_lsl`, `version=wahoo_lsl.__version__` plus the `acquisition` mapping, then `desc/channels`).

- [ ] **Step 1: Write failing tests.**

Append to `tests/test_cps.py`:

```python
def test_field_info_covers_every_measurement_key():
    from wahoo_lsl.cps import DERIVED_KEYS, FIELD_INFO, MEASUREMENTS

    keys = {k for spec in MEASUREMENTS.values() for k in spec.keys}
    assert keys | set(DERIVED_KEYS) == set(FIELD_INFO)
    assert FIELD_INFO["torque_source"].encoding == {"wheel": 0.0, "crank": 1.0}
    assert FIELD_INFO["offset_compensation"].encoding == {False: 0.0, True: 1.0}
    assert FIELD_INFO["speed_kmh"].unit == "km/h"
```

Create `tests/test_lsl.py`:

```python
import xml.etree.ElementTree as ET

import pylsl

from wahoo_lsl import __version__
from wahoo_lsl.lsl import Channel, add_dict, sanitize, stream_info

CHANNELS = [Channel("power_w", "W", "measured", "instantaneous power"), Channel("speed_kmh", "km/h", "derived")]


def xml(info: pylsl.StreamInfo) -> ET.Element:
    return ET.fromstring(info.as_xml())


def test_sanitize_keeps_alphanumerics():
    assert sanitize("KICKR V6-ä!") == "KICKRV6"


def test_stream_info_identity_and_format():
    info = stream_info("KICKR-CPS-abcdef", "CPS", "KICKR-CPS-abcdef", CHANNELS, 1.0)
    assert info.name() == "KICKR-CPS-abcdef"
    assert info.source_id() == "KICKR-CPS-abcdef"
    assert info.type() == "CPS"
    assert info.channel_count() == 2
    assert info.nominal_srate() == 1.0
    assert info.channel_format() == pylsl.cf_double64


def test_stream_info_desc_acquisition_and_channels():
    info = stream_info("n", "CPS", "s", CHANNELS, 0.0, acquisition={"clock": "local_clock", "skip": None})
    root = xml(info)
    acq = root.find("desc/acquisition")
    assert acq.findtext("software") == "wahoo_lsl"
    assert acq.findtext("version") == __version__
    assert acq.findtext("clock") == "local_clock"
    assert acq.find("skip") is None
    chans = root.findall("desc/channels/channel")
    assert [(c.findtext("label"), c.findtext("unit"), c.findtext("type")) for c in chans] == [
        ("power_w", "W", "measured"),
        ("speed_kmh", "km/h", "derived"),
    ]
    assert chans[0].findtext("description") == "instantaneous power"


def test_add_dict_stringifies_and_skips_none():
    info = stream_info("n", "CPS", "s", CHANNELS, 1.0)
    add_dict(info.desc(), "device", {"name": "KICKR", "features_raw": 4622, "missing": None})
    dev = xml(info).find("desc/device")
    assert dev.findtext("name") == "KICKR"
    assert dev.findtext("features_raw") == "4622"
    assert dev.find("missing") is None
```

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_cps.py tests/test_lsl.py -q -W error` → FAIL (ImportError).

- [ ] **Step 3: Implement.**

`cps.py`, after the `MEASUREMENTS` dict:

```python
@dataclass(frozen=True)
class FieldInfo:
    unit: str
    description: str
    encoding: dict | None = None  # text/bool value -> numeric channel value


# Units and meanings of every output key, plus derived metrics (shared by LSL channel metadata).
FIELD_INFO: dict[str, FieldInfo] = {
    "power_w": FieldInfo("W", "instantaneous power"),
    "pedal_power_balance_pct": FieldInfo("%", "pedal power balance"),
    "pedal_power_balance_ref": FieldInfo("", "pedal power balance reference", {"unknown": 0.0, "left": 1.0}),
    "accumulated_torque_nm": FieldInfo("N.m", "cumulative torque, wraps at 2048 N.m"),
    "torque_source": FieldInfo("", "accumulated torque source", {"wheel": 0.0, "crank": 1.0}),
    "wheel_revs": FieldInfo("rev", "cumulative wheel revolutions (uint32)"),
    "wheel_event_t": FieldInfo("1/2048 s", "last wheel event time, wraps at 65536"),
    "crank_revs": FieldInfo("rev", "cumulative crank revolutions, wraps at 65536"),
    "crank_event_t": FieldInfo("1/1024 s", "last crank event time, wraps at 65536"),
    "max_force_n": FieldInfo("N", "maximum force magnitude"),
    "min_force_n": FieldInfo("N", "minimum force magnitude"),
    "max_torque_nm": FieldInfo("N.m", "maximum torque magnitude"),
    "min_torque_nm": FieldInfo("N.m", "minimum torque magnitude"),
    "max_angle_deg": FieldInfo("deg", "angle of maximum magnitude"),
    "min_angle_deg": FieldInfo("deg", "angle of minimum magnitude"),
    "top_dead_spot_deg": FieldInfo("deg", "top dead spot angle"),
    "bottom_dead_spot_deg": FieldInfo("deg", "bottom dead spot angle"),
    "accumulated_energy_kj": FieldInfo("kJ", "cumulative energy"),
    "offset_compensation": FieldInfo("", "offset compensation indicator", {False: 0.0, True: 1.0}),
    "speed_kmh": FieldInfo("km/h", "speed from wheel revolutions and wheel circumference"),
    "cadence_rpm": FieldInfo("rpm", "cadence from crank revolutions"),
}
DERIVED_KEYS = ("speed_kmh", "cadence_rpm")
```

Create `src/wahoo_lsl/lsl.py`:

```python
"""Generic LSL helpers shared by every wahoo_lsl outlet (CPS now, FTMS control events later)."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pylsl

from . import __version__

SOFTWARE = "wahoo_lsl"
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


@dataclass(frozen=True)
class Channel:
    label: str
    unit: str
    type: str  # "measured" or "derived"
    description: str = ""


def sanitize(text: str) -> str:
    """Keep ASCII letters and digits only (safe for stream names and source ids)."""
    return _NOT_ALNUM.sub("", text)


def add_dict(parent: Any, name: str, mapping: Mapping[str, object]) -> Any:
    """Append <name> to an XML element with one text child per non-None entry."""
    node = parent.append_child(name)
    for key, value in mapping.items():
        if value is not None:
            node.append_child_value(key, str(value))
    return node


def add_channels(desc: Any, channels: Sequence[Channel]) -> None:
    """Channel metadata in the XDF convention: desc/channels/channel/{label,unit,type,description}."""
    node = desc.append_child("channels")
    for ch in channels:
        add_dict(node, "channel", {"label": ch.label, "unit": ch.unit, "type": ch.type, "description": ch.description})


def stream_info(
    name: str,
    type_: str,
    source_id: str,
    channels: Sequence[Channel],
    nominal_srate: float,
    *,
    fmt: str = "double64",
    acquisition: Mapping[str, object] | None = None,
) -> pylsl.StreamInfo:
    info = pylsl.StreamInfo(name, type_, len(channels), nominal_srate, fmt, source_id)
    desc = info.desc()
    add_dict(desc, "acquisition", {"software": SOFTWARE, "version": __version__, **(acquisition or {})})
    add_channels(desc, channels)
    return info
```

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/cps.py src/wahoo_lsl/lsl.py tests/test_cps.py tests/test_lsl.py
git commit -m "feat: CPS channel vocabulary and generic LSL stream helpers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `LslSink`

**Files:**
- Create: `src/wahoo_lsl/sinks/lsl_sink.py`
- Modify: `src/wahoo_lsl/sinks/__init__.py` (export `LslSink`)
- Modify: `tests/fakes.py` (add `FakeOutlet`)
- Test: `tests/test_lsl_sink.py`

**Interfaces:**
- Consumes: `Config` (`measurements`, `lsl.nominal_srate`, `derive.wheel_circumference_m`) from Task 4; `Sample.derived` (Task 2); `Sink.batched`/`handle_batch` (Task 3); `connected` detail `device_info` (Task 5); `FIELD_INFO`, `DERIVED_KEYS`, `lsl.*` (Task 6); `derive` constants (Task 1).
- Produces: `LslSink(cfg: Config, outlet_factory: Callable[[pylsl.StreamInfo], Any] | None = None)` (default `pylsl.StreamOutlet`), `name = "lsl"`, `batched = True`. Module functions `stream_id(name, address, device_info) -> str`, `channel_keys(measurements, supported) -> list[str]`. `tests/fakes.py::FakeOutlet(info)` with `.info`, `.chunks: list[tuple[list[list[float]], list[float]]]`, `.push_chunk(rows, timestamps)`.

- [ ] **Step 1: Write failing tests.**

Append to `tests/fakes.py`:

```python
class FakeOutlet:
    """Stand-in for pylsl.StreamOutlet: records pushed chunks."""

    def __init__(self, info) -> None:
        self.info = info
        self.chunks: list = []

    def push_chunk(self, rows, timestamps) -> None:
        self.chunks.append(([list(r) for r in rows], list(timestamps)))
```

Create `tests/test_lsl_sink.py`:

```python
import math
import uuid
import xml.etree.ElementTree as ET

import pylsl
import pytest
from fakes import FakeOutlet

from wahoo_lsl.config import parse_config
from wahoo_lsl.pipeline import Event, Sample
from wahoo_lsl.sinks.lsl_sink import LslSink, channel_keys, stream_id

ADDRESS = "CF46355D-B7CF-4353-8FA5-9F2DB2F24BFA"
ALL = ("power", "wheel_revolutions", "accumulated_torque", "crank_revolutions")
KEYS = [
    "power_w",
    "wheel_revs",
    "wheel_event_t",
    "accumulated_torque_nm",
    "torque_source",
    "crank_revs",
    "crank_event_t",
    "speed_kmh",
    "cadence_rpm",
]


def cfg(measurements=ALL, **sections):
    return parse_config({"device": {"name": "KICKR"}, "measurements": list(measurements), **sections})


def connected(name="Wahoo KICKR 58EC", address=ADDRESS, device_info=None):
    detail = {"name": name, "address": address, "address_kind": "uuid", "device_info": device_info or {}}
    return Event("connected", 1.0, 0, detail)


def features(supported=ALL):
    detail = {"raw": 4622, "supported": list(supported), "unavailable": [], "sensor_location": "Other"}
    return Event("features", 1.0, 0, detail)


def sample(t, fields=None, derived=None, parse_error=None):
    return Sample(0, t, 0, "", 0, fields or {}, parse_error, derived or {})


class Factory:
    def __init__(self, fail_times=0):
        self.outlets: list[FakeOutlet] = []
        self.fail_times = fail_times

    def __call__(self, info):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("liblsl unavailable")
        outlet = FakeOutlet(info)
        self.outlets.append(outlet)
        return outlet


def rows(outlet):
    return [r for chunk, _ in outlet.chunks for r in chunk]


def same(a, b):
    return len(a) == len(b) and all((math.isnan(x) and math.isnan(y)) or x == y for x, y in zip(a, b))


@pytest.mark.parametrize(
    "name, address, device_info, expected",
    [
        ("Wahoo KICKR 58EC", ADDRESS, {}, "KICKR-CPS-f24bfa"),
        ("Wahoo KICKR 58EC", ADDRESS, {"model_number": "KICKR V6"}, "KICKRV6-CPS-f24bfa"),
        ("KICKR CORE 1234", "AA:BB:CC:DD:EE:01", {}, "KICKRCORE-CPS-ddee01"),
        (None, "AA:BB:CC:DD:EE:01", {}, "DEVICE-CPS-ddee01"),
        ("Wahoo 58EC", "AA:BB:CC:DD:EE:01", {"model_number": " "}, "DEVICE-CPS-ddee01"),
    ],
)
def test_stream_id(name, address, device_info, expected):
    assert stream_id(name, address, device_info) == expected


def test_channel_keys_follow_config_order_and_support():
    assert channel_keys(ALL, set(ALL)) == KEYS
    # torque unsupported, no crank support -> no torque channels, no cadence
    assert channel_keys(ALL, {"power", "wheel_revolutions"}) == ["power_w", "wheel_revs", "wheel_event_t", "speed_kmh"]
    # derived speed needs only device support, not configuration
    assert channel_keys(("power",), {"power", "wheel_revolutions"}) == ["power_w", "speed_kmh"]


def test_outlet_created_on_features_with_identity_and_metadata():
    factory = Factory()
    sink = LslSink(cfg(derive={"wheel_circumference_m": 2.096}), factory)
    sink.handle_batch([connected(device_info={"manufacturer": "Wahoo Fitness"})])
    assert factory.outlets == []  # nothing until the device's features are known
    sink.handle_batch([features()])
    (outlet,) = factory.outlets
    info = outlet.info
    assert (info.name(), info.source_id(), info.type()) == ("KICKR-CPS-f24bfa",) * 2 + ("CPS",)
    assert info.nominal_srate() == 1.0
    assert info.channel_format() == pylsl.cf_double64
    root = ET.fromstring(info.as_xml())
    chans = root.findall("desc/channels/channel")
    assert [c.findtext("label") for c in chans] == KEYS
    assert [c.findtext("unit") for c in chans][-2:] == ["km/h", "rpm"]
    assert [c.findtext("type") for c in chans][-3:] == ["measured", "derived", "derived"]
    assert "wheel=0, crank=1" in chans[KEYS.index("torque_source")].findtext("description")
    dev = root.find("desc/device")
    assert dev.findtext("name") == "Wahoo KICKR 58EC"
    assert dev.findtext("address") == ADDRESS
    assert dev.findtext("sensor_location") == "Other"
    assert dev.findtext("features_raw") == "4622"
    assert dev.findtext("manufacturer") == "Wahoo Fitness"
    assert root.find("desc/derivation").findtext("wheel_circumference_m") == "2.096"
    acq = root.find("desc/acquisition")
    assert acq.findtext("software") == "wahoo_lsl"
    assert acq.findtext("nominal_srate") == "1.0"


def test_irregular_rate():
    factory = Factory()
    sink = LslSink(cfg(lsl={"nominal_srate": 0}), factory)
    sink.handle_batch([connected(), features()])
    assert factory.outlets[0].info.nominal_srate() == pylsl.IRREGULAR_RATE


def test_rows_values_encodings_nan_and_timestamps():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    full = {
        "power_w": 200,
        "wheel_revs": 5,
        "wheel_event_t": 2048,
        "accumulated_torque_nm": 1.5,
        "torque_source": "crank",
        "crank_revs": 3,
        "crank_event_t": 1024,
        "offset_compensation": False,
    }
    sink.handle_batch(
        [
            connected(),
            features(),
            sample(10.0, full, {"speed_kmh": 30.0, "cadence_rpm": None}),
            sample(11.0, {"power_w": 150}),  # other fields absent
            sample(12.0, parse_error="truncated"),  # skipped
        ]
    )
    (outlet,) = factory.outlets
    assert len(outlet.chunks) == 1  # one push per batch
    (chunk, stamps) = outlet.chunks[0]
    assert stamps == [10.0, 11.0]
    nan = math.nan
    assert same(chunk[0], [200.0, 5.0, 2048.0, 1.5, 1.0, 3.0, 1024.0, 30.0, nan])
    assert same(chunk[1], [150.0] + [nan] * 8)


def test_offset_compensation_bool_encoding():
    factory = Factory()
    sink = LslSink(cfg(("power", "offset_compensation")), factory)
    sink.handle_batch([connected(), features(("power", "offset_compensation")), sample(1.0, {"offset_compensation": True})])
    assert same(rows(factory.outlets[0])[0], [math.nan, 1.0])


def test_outlet_kept_on_same_reconnect():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features(), sample(1.0, {"power_w": 1})])
    sink.handle_batch([connected(), features(), sample(5.0, {"power_w": 2})])
    (outlet,) = factory.outlets
    assert [r[0] for r in rows(outlet)] == [1.0, 2.0]


def test_outlet_replaced_on_change_flushes_old_rows_first(caplog):
    factory = Factory()
    sink = LslSink(cfg(), factory)
    with caplog.at_level("INFO"):
        sink.handle_batch(
            [
                connected(),
                features(),
                sample(1.0, {"power_w": 1}),
                connected(address="AA:BB:CC:DD:EE:01"),
                features(),
                sample(2.0, {"power_w": 2}),
            ]
        )
    old, new = factory.outlets
    assert [r[0] for r in rows(old)] == [1.0]
    assert [r[0] for r in rows(new)] == [2.0]
    assert new.info.source_id() == "KICKR-CPS-ddee01"
    assert any("Replacing LSL outlet" in r.getMessage() for r in caplog.records)


def test_creation_failure_drops_samples_until_next_features(caplog):
    factory = Factory(fail_times=1)
    sink = LslSink(cfg(), factory)
    with caplog.at_level("ERROR"):
        sink.handle_batch([connected(), features(), sample(1.0, {"power_w": 1})])
    assert factory.outlets == []
    assert any("Cannot create LSL outlet" in r.getMessage() for r in caplog.records)
    sink.handle_batch([connected(), features(), sample(2.0, {"power_w": 2})])
    assert [r[0] for r in rows(factory.outlets[0])] == [2.0]


def test_push_failure_does_not_resend_rows():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features()])
    outlet = factory.outlets[0]
    original = outlet.push_chunk

    def broken(rows, stamps):
        raise RuntimeError("push failed")

    outlet.push_chunk = broken
    with pytest.raises(RuntimeError):
        sink.handle_batch([sample(1.0, {"power_w": 1})])
    outlet.push_chunk = original
    sink.handle_batch([sample(2.0, {"power_w": 2})])
    assert [r[0] for r in rows(outlet)] == [2.0]


def test_stop_releases_outlet():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features()])
    sink.stop()
    sink.handle_batch([sample(1.0, {"power_w": 1})])
    assert rows(factory.outlets[0]) == []


def test_loopback_real_outlet_reaches_an_inlet():
    address = uuid.uuid4().hex
    sink = LslSink(cfg())
    sink.handle_batch([connected(address=address), features()])
    source_id = f"KICKR-CPS-{address[-6:]}"
    streams = pylsl.resolve_byprop("source_id", source_id, timeout=5.0)
    if not streams:
        sink.stop()
        pytest.skip("LSL stream not resolvable on this host (multicast blocked?)")
    inlet = pylsl.StreamInlet(streams[0])
    try:
        inlet.open_stream(timeout=5.0)
        now = pylsl.local_clock()
        sink.handle_batch([sample(now, {"power_w": 123}, {"speed_kmh": 25.0, "cadence_rpm": 90.0})])
        got: list = []
        deadline = pylsl.local_clock() + 5.0
        while not got and pylsl.local_clock() < deadline:
            got, _ = inlet.pull_chunk(timeout=0.5)
        assert got, "no samples received"
        assert got[0][0] == 123.0
        assert got[0][-2:] == [25.0, 90.0]
        assert inlet.info().source_id() == source_id
    finally:
        inlet.close_stream()
        sink.stop()
```

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_lsl_sink.py -q -W error` → FAIL (`No module named 'wahoo_lsl.sinks.lsl_sink'`).

- [ ] **Step 3: Implement** — `src/wahoo_lsl/sinks/lsl_sink.py`:

```python
"""LSL outlet sink: the regular CPS stream (spec: CPS LSL outlet design).

Runs on its own SinkRunner thread; each drained queue batch becomes one push_chunk.
The outlet is created when the device's features are known and kept across BLE
reconnects to the same device, so LSL resolvers see a gap rather than a new stream.
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pylsl

from .. import derive
from ..config import Config
from ..cps import DERIVED_KEYS, FIELD_INFO, MEASUREMENTS
from ..lsl import Channel, add_dict, sanitize, stream_info
from ..pipeline import Event, Sample, Sink

log = logging.getLogger(__name__)

STREAM_TYPE = "CPS"
_HEX = re.compile(r"[^0-9A-Fa-f]")
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")


def _model(name: str | None, device_info: dict) -> str:
    model = sanitize(device_info.get("model_number") or "")
    if model:
        return model
    words = (name or "").split()
    if words and words[0].lower() == "wahoo":
        words = words[1:]
    if words and _HEX4.fullmatch(words[-1]):
        words = words[:-1]
    return sanitize("".join(words)) or "DEVICE"


def stream_id(name: str | None, address: str, device_info: dict) -> str:
    """`<MODEL>-CPS-<last 6 hex digits of the MAC/UUID>`, used as both stream name and source_id."""
    return f"{_model(name, device_info)}-{STREAM_TYPE}-{_HEX.sub('', address)[-6:].lower()}"


def channel_keys(measurements: Sequence[str], supported: Iterable[str]) -> list[str]:
    """Configured + supported measurement keys in config order, then derived metrics the device can feed."""
    supported = set(supported)
    keys = [k for m in measurements if m in supported for k in MEASUREMENTS[m].keys]
    if "wheel_revolutions" in supported:
        keys.append("speed_kmh")
    if "crank_revolutions" in supported:
        keys.append("cadence_rpm")
    return keys


def _channel(key: str) -> Channel:
    info = FIELD_INFO[key]
    description = info.description
    if info.encoding:
        codes = ", ".join(f"{str(k).lower()}={v:g}" for k, v in info.encoding.items())
        description = f"{description} ({codes})"
    return Channel(key, info.unit, "derived" if key in DERIVED_KEYS else "measured", description)


def _row(sample: Sample, keys: Sequence[str]) -> list[float]:
    values = {**sample.fields, **sample.derived}
    row = []
    for key in keys:
        value = values.get(key)
        encoding = FIELD_INFO[key].encoding
        if value is None:
            row.append(math.nan)
        elif encoding is not None:
            row.append(encoding.get(value, math.nan))
        else:
            row.append(float(value))
    return row


class LslSink(Sink):
    name = "lsl"
    batched = True

    def __init__(self, cfg: Config, outlet_factory: Callable[[pylsl.StreamInfo], Any] | None = None) -> None:
        self._cfg = cfg
        self._factory = outlet_factory or pylsl.StreamOutlet
        self._connected: dict = {}
        self._outlet: Any = None
        self._identity: tuple[str, tuple[str, ...]] | None = None  # (source_id, channel keys)
        self._keys: list[str] = []
        self._rows: list[list[float]] = []
        self._stamps: list[float] = []

    def handle(self, item: Sample | Event) -> None:
        self.handle_batch([item])

    def handle_batch(self, items: list[Sample | Event]) -> None:
        try:
            for item in items:
                if isinstance(item, Sample):
                    if self._outlet is not None and item.parse_error is None:
                        self._rows.append(_row(item, self._keys))
                        self._stamps.append(item.t_lsl)
                elif item.kind == "connected":
                    self._connected = item.detail
                elif item.kind == "features":
                    self._flush()
                    self._open(item.detail)
        finally:
            self._flush()

    def stop(self) -> None:
        self._flush()
        self._outlet = None
        self._identity = None

    def _flush(self) -> None:
        if not self._rows:
            return
        rows, stamps = self._rows, self._stamps
        self._rows, self._stamps = [], []  # never re-sent, even if the push fails
        self._outlet.push_chunk(rows, stamps)

    def _open(self, features: dict) -> None:
        c = self._connected
        supported = sorted(features.get("supported", ()))
        keys = channel_keys(self._cfg.measurements, supported)
        source_id = stream_id(c.get("name"), c.get("address") or "", c.get("device_info") or {})
        identity = (source_id, tuple(keys))
        if self._outlet is not None and identity == self._identity:
            return  # same device, same layout: keep the stream alive across the reconnect
        if self._outlet is not None:
            log.info("Replacing LSL outlet %s", self._identity[0])
        self._outlet, self._identity, self._keys = None, None, []
        try:
            info = self._stream_info(source_id, keys, features, supported)
            self._outlet = self._factory(info)
        except Exception:
            log.exception("Cannot create LSL outlet %s; LSL samples dropped until the device reconnects", source_id)
            return
        self._identity, self._keys = identity, keys
        log.info("LSL outlet %s: %d channels (%s)", source_id, len(keys), ", ".join(keys))

    def _stream_info(self, source_id: str, keys: list[str], features: dict, supported: list[str]) -> pylsl.StreamInfo:
        c = self._cfg
        info = stream_info(
            source_id,
            STREAM_TYPE,
            source_id,
            [_channel(k) for k in keys],
            c.lsl.nominal_srate,
            acquisition={
                "clock": "pylsl.local_clock at BLE notify entry",
                "timestamps": "notification arrival time (BLE delivery), not device measurement time",
                "nominal_srate": c.lsl.nominal_srate,
            },
        )
        desc = info.desc()
        conn = self._connected
        add_dict(
            desc,
            "device",
            {
                "name": conn.get("name"),
                "address": conn.get("address"),
                "address_kind": conn.get("address_kind"),
                "sensor_location": features.get("sensor_location"),
                "features_raw": features.get("raw"),
                "supported": ",".join(supported),
                **(conn.get("device_info") or {}),
            },
        )
        add_dict(
            desc,
            "derivation",
            {
                "wheel_circumference_m": c.derive.wheel_circumference_m,
                "zero_after_s": derive.ZERO_AFTER_S,
                "method": "delta revolutions / delta event time between successive notifications",
                "wheel_ticks_per_s": derive.WHEEL_TICKS_PER_S,
                "crank_ticks_per_s": derive.CRANK_TICKS_PER_S,
                "wheel_rev_wrap": derive.WHEEL_REV_WRAP,
                "crank_rev_wrap": derive.CRANK_REV_WRAP,
                "event_time_wrap": derive.EVENT_T_WRAP,
            },
        )
        return info
```

`src/wahoo_lsl/sinks/__init__.py`:

```python
from .console_sink import ConsoleSink
from .log_sink import LogSink, event_record, format_log_path, sample_record
from .lsl_sink import LslSink

__all__ = ["ConsoleSink", "LogSink", "LslSink", "event_record", "format_log_path", "sample_record"]
```

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS (the loopback test may SKIP if multicast is blocked; report whether it passed or skipped).

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/sinks/lsl_sink.py src/wahoo_lsl/sinks/__init__.py tests/fakes.py tests/test_lsl_sink.py
git commit -m "feat: LslSink broadcasts CPS samples as a regular LSL stream

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Wire into `wahoo-lsl run`; README

**Files:**
- Modify: `src/wahoo_lsl/cli.py` (`run_session`)
- Modify: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `LslSink` (Task 7), `CpsDeriver` (Task 1), `Config.lsl`/`Config.derive` (Task 4), `Pipeline(..., deriver=)` (Task 2), `FakeOutlet` (Task 7).
- Produces: `run_session(cfg, *, debug=False, backend=None, start=None, outlet_factory=None) -> int`.

- [ ] **Step 1: Write failing tests** in `tests/test_cli.py`.

Change `write_config` so existing tests never open real outlets — the `cfg` dict gains `"lsl": {"enabled": False},` before `cfg.update(overrides)`. Import `FakeOutlet` from `fakes` (extend the existing `from fakes import KICKR, FakeBackend`) and add `import xml.etree.ElementTree as ET`. Append:

```python
def test_run_session_streams_to_lsl(tmp_path):
    outlets: list = []

    def factory(info):
        outlets.append(FakeOutlet(info))
        return outlets[-1]

    cfg = parse_config(json.loads(write_config(tmp_path, lsl={"enabled": True}).read_text(encoding="utf-8")))
    backend = FakeBackend(sessions=[([POWER, WHEEL], True)])
    code = run_session(cfg, backend=backend, start=datetime(2026, 10, 20), outlet_factory=factory)
    assert code == 4
    (outlet,) = outlets
    assert outlet.info.source_id() == "KICKRCORE-CPS-ddee01"
    # accumulated_torque is configured but unsupported -> no channel; wheel/crank fields not configured -> not pushed
    chans = ET.fromstring(outlet.info.as_xml()).findall("desc/channels/channel")
    assert [c.findtext("label") for c in chans] == ["power_w", "speed_kmh", "cadence_rpm"]
    rows = [r for chunk, _ in outlet.chunks for r in chunk]
    assert [r[0] for r in rows] == [200.0, 150.0]
    samples = [r for r in read_log(tmp_path / "logs" / "cps_20261020.jsonl") if r["type"] == "sample"]
    assert samples[0]["derived"] == {"speed_kmh": None, "cadence_rpm": None}


def test_run_session_lsl_disabled_creates_no_outlet(tmp_path):
    outlets: list = []
    backend = FakeBackend(sessions=[([POWER], True)])
    run_session(load_cfg(tmp_path), backend=backend, start=datetime(2026, 10, 20), outlet_factory=outlets.append)
    assert outlets == []


def test_run_session_lsl_failure_keeps_logging(tmp_path, caplog):
    def factory(info):
        raise RuntimeError("liblsl unavailable")

    cfg = parse_config(json.loads(write_config(tmp_path, lsl={"enabled": True}).read_text(encoding="utf-8")))
    backend = FakeBackend(sessions=[([POWER, POWER], True)])
    with caplog.at_level(logging.ERROR):
        code = run_session(cfg, backend=backend, start=datetime(2026, 10, 20), outlet_factory=factory)
    assert code == 4  # same as without LSL: link dropped, reconnect disabled
    records = read_log(tmp_path / "logs" / "cps_20261020.jsonl")
    assert len([r for r in records if r["type"] == "sample"]) == 2
    assert any("Cannot create LSL outlet" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run to verify failure** — `conda run -n wahoo python -m pytest tests/test_cli.py -q -W error` → FAIL (`unexpected keyword argument 'outlet_factory'`).

- [ ] **Step 3: Implement** in `cli.py`.

Imports: add `from collections.abc import Callable, Sequence` (extend the existing import), `from typing import Any`, `from .derive import CpsDeriver`, and change the sinks import to `from .sinks import ConsoleSink, LogSink, LslSink, format_log_path`.

`run_session` signature gains `outlet_factory: Callable[[Any], Any] | None = None` (keyword-only, last). Replace the sink/pipeline construction with:

```python
    warn = WarnOnce(log)
    sinks: list[Sink] = [LogSink(log_path, cfg.log.flush_every_n)]
    if cfg.lsl.enabled:
        sinks.append(LslSink(cfg, outlet_factory))
    if debug:
        sinks.append(ConsoleSink())
    deriver = CpsDeriver(cfg.derive.wheel_circumference_m)
    pipeline = Pipeline(sinks, cfg.measurements, warn, queue_warn_depth=cfg.queue_warn_depth, deriver=deriver)
    pipeline.start()
    pipeline.put(make_event("clock_sync"))
    log.info("Logging samples to %s", log_path)
    if cfg.lsl.enabled:
        rate = f"{cfg.lsl.nominal_srate:g} Hz" if cfg.lsl.nominal_srate else "irregular"
        log.info("LSL outlet enabled (nominal rate %s); it appears once the device is connected", rate)
```

README.md: in the "Log format" example, add `,"derived":{"speed_kmh":31.2,"cadence_rpm":88.0}` after the sample's `"fields":{...}` object, and add one sentence after the event-kinds line: `` `derived` holds speed and cadence computed once from successive wheel/crank counters (see LSL stream); it is `{}` on parse errors. `` In the "Manual hardware checklist" add item `7. \`list-features\` prints the Device Information fields (model number etc.); \`run\` logs "LSL outlet KICKR…-CPS-…" and the resolver snippet under "LSL stream" shows live samples.` Then insert a new section before `## Exit codes`:

````markdown
## LSL stream

`wahoo-lsl run` also publishes a Lab Streaming Layer stream (on by default; `"lsl": {"enabled": false}` turns it off).

- **Name and `source_id`:** `<MODEL>-CPS-<id6>`, e.g. `KICKR-CPS-f24bfa`. `MODEL` is the Device Information model
  number, or the advertised name without "Wahoo" and its hex suffix; `id6` is the last 6 hex digits of the MAC/UUID.
  Resolve by `source_id` to reconnect reliably. On macOS the UUID is assigned per host by CoreBluetooth, so the
  `source_id` is stable on one machine but differs between machines; on Windows/Linux the MAC makes it global.
- **Type** `CPS`, format `double64`, nominal rate `lsl.nominal_srate` (default 1.0 Hz; 0 = irregular).
- **Channels:** the numeric fields of each configured measurement the device supports, in config order, then
  `speed_kmh` (if the device reports wheel revolutions) and `cadence_rpm` (if crank revolutions). Text/flag fields are
  encoded (`torque_source` wheel=0/crank=1, `offset_compensation` false=0/true=1); absent values are NaN.
  Units and types are in `desc/channels`.
- **Metadata:** `desc/acquisition` (software, version, clock), `desc/device` (name, address, sensor location,
  CP feature bits, Device Information fields), `desc/derivation` (`wheel_circumference_m` from
  `derive.wheel_circumference_m`, default 2.105 m, and the derivation constants).
- The stream appears once the device is connected and stays up across BLE reconnects to the same device.

**Timing.** Each sample's timestamp is its `t_lsl` (BLE notification arrival, same clock as the log). The KICKR
notifies at ≈ 0.9998 Hz with ±0.1 s delivery jitter, and gaps (disconnects, parse errors) are not filled. Use the
timestamps, not sample index ÷ rate (that drifts ≈ 0.7 s/h and misaligns after gaps). pyxdf's default
`dejitter_timestamps=True` replaces timestamps of regular streams with a linear fit; pass `False` for raw arrival times.

Quick check while `run` is going:

```python
import pylsl
(info,) = pylsl.resolve_byprop("type", "CPS", timeout=10)
inlet = pylsl.StreamInlet(info)
print(inlet.info().as_xml())
while True:
    sample, ts = inlet.pull_sample()
    print(f"{ts:.3f}", sample)
```
````

- [ ] **Step 4: Run tests** — `conda run -n wahoo python -m pytest -q -W error` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/cli.py README.md tests/test_cli.py
git commit -m "feat: wahoo-lsl run publishes the CPS LSL stream; README

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
