# Wahoo KICKR CPS Logger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A cross-platform Python CLI that connects to a Wahoo KICKR's BLE Cycling Power Service, subscribes to CP Measurement notifications, and timestamps, parses, buffers and logs every payload to JSONL — structured so an LSL sink can be added later without touching BLE or parsing code.

**Architecture:** asyncio/bleak on the main thread; the notify callback only stamps `pylsl.local_clock()` + `time.time_ns()`, assigns a sequence number and enqueues raw bytes. A dispatcher thread parses (pure `cps.py`), applies warn-once availability checks, and fans immutable `Sample`/`Event` objects out to one queue + thread per sink (JSONL log, debug console).

**Tech Stack:** Python ≥ 3.10 (the existing `wahoo` conda env runs 3.12.0), `bleak` (≥ 1.0, verified with 3.0.2), `pylsl` (≥ 1.17, verified with 1.18.6, bundles liblsl), `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-20-wahoo-cps-logger-design.md` — read it before starting any task.

## Global Constraints

- Python `>=3.10`; code must run unchanged on macOS, Windows and Linux.
- Only `src/wahoo_lsl/ble.py` imports `bleak`. Only `src/wahoo_lsl/clock.py` reads clocks (`pylsl.local_clock()`, `time.time_ns()`).
- `sys.platform` checks are allowed ONLY in `config.address_kind` and `cli._stream` (signal handling). No other OS branches.
- `t_lsl` = `pylsl.local_clock()` (sole timing reference); `t_wall` = `time.time_ns()` rendered ISO-8601 UTC with microseconds (reference only).
- In the notify callback, `clock.now_lsl()` is the FIRST statement and `clock.now_wall_ns()` the SECOND. No parsing or logging in the callback.
- All queues are unbounded `queue.Queue`; nothing is ever dropped.
- Every parsed field is logged; `measurements` in the config drives availability warnings only (no output filtering in MVP).
- `cps.py` has no I/O, no bleak, no threading.
- Exit codes: 0 ok, 1 unexpected error, 2 config error (incl. unwritable log path), 3 device/connection setup error, 4 reconnect exhausted/disabled after link loss, 130 Ctrl-C during `scan`/`list-features`.
- Use the existing **`wahoo` conda environment** (do NOT use uv, venv, or create a new env). Run every command through `conda run -n wahoo ...` (non-interactive shells can't `conda activate`). Install with `conda run -n wahoo python -m pip ...` only.
- Tests must not require BLE hardware. Run everything with `conda run -n wahoo python -m pytest`.
- Commit after every task with the message given; end every commit message with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

Clarifications relative to the spec (intentional, keep them):
- `STOP` is put on the ingest queue by `Pipeline.close()` (called from `cli.run_session`), and the `stopped` event is emitted by `cli.run_session`, not by `ble.py`. Same ordering guarantees as spec §7.6.
- `cli.run_session` pre-flights the log path (create parent dir, open for append) in the main thread and exits 2 if that fails, so a bad path can never cause a silent run without a log.

## Review Focus

1. **Unwritable or malformed log path** (`log.path` with `{foo}`, or a parent that is a file) → exit 2 before touching BLE, never a run with no log. Pinned in Task 4 (`test_invalid_configs_rejected` template cases) and Task 8 (`test_run_session_unwritable_log_path`).
2. **Supported measurement missing from a notification** (content masking / firmware quirk) → exactly one WARNING, every other field still logged. Pinned in Task 5 (`test_missing_configured_field_warns_once`).
3. **Link drops during setup** (`start_notify` fails after connect) → exit 3 with no dangling connection. Pinned in Task 7 (`test_setup_failure_when_subscribe_fails`).
4. **Ctrl-C while waiting in reconnect backoff** → prompt clean return, no further attempts. Pinned in Task 7 (`test_stop_during_backoff_returns_promptly`).
5. **Truncated/malformed payload from firmware** → sample still logged with `raw` + `parse_error`, counted in the `stopped` event. Pinned in Task 5 (`test_parse_error_forwarded_with_raw_and_counted`) and Task 8 (`test_run_session_end_to_end`).

## File Structure

```
pyproject.toml                 # Task 1  — package metadata + console script
environment.yml                # Task 1  — conda env spec (recreates `wahoo`)
requirements.txt               # Task 1  — runtime pip deps
requirements-dev.txt           # Task 1  — + pytest
.gitignore                     # Task 1
config.example.json            # Task 4
README.md                      # Task 8
src/wahoo_lsl/__init__.py      # Task 1
src/wahoo_lsl/__main__.py      # Task 8
src/wahoo_lsl/clock.py         # Task 1  — clocks
src/wahoo_lsl/warn.py          # Task 1  — WarnOnce
src/wahoo_lsl/cps.py           # Tasks 2–3 — CPS constants + pure parsers
src/wahoo_lsl/config.py        # Task 4  — config load/validate, identifier selection
src/wahoo_lsl/pipeline.py      # Task 5  — types, Dispatcher, SinkRunner, Pipeline
src/wahoo_lsl/sinks/__init__.py      # Task 6
src/wahoo_lsl/sinks/log_sink.py      # Task 6
src/wahoo_lsl/sinks/console_sink.py  # Task 6
src/wahoo_lsl/ble.py           # Task 7  — bleak wrapper, reconnect
src/wahoo_lsl/cli.py           # Task 8  — argparse, wiring
tests/test_env.py tests/test_clock.py tests/test_warn.py  # Task 1
tests/test_cps.py                            # Tasks 2–3
tests/test_config.py                         # Task 4
tests/test_pipeline.py                       # Task 5
tests/test_sinks.py                          # Task 6
tests/fakes.py tests/test_ble.py             # Task 7
tests/test_cli.py                            # Task 8
```

---

### Task 1: Environment/requirements setup, clock, WarnOnce

**Files:**
- Create: `environment.yml`, `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `.gitignore`, `src/wahoo_lsl/__init__.py`, `src/wahoo_lsl/clock.py`, `src/wahoo_lsl/warn.py`
- Test: `tests/test_env.py`, `tests/test_clock.py`, `tests/test_warn.py`

**Interfaces:**
- Consumes: the existing `wahoo` conda env.
- Produces:
  - Installed editable package `wahoo_lsl` + `wahoo-lsl` console script in the `wahoo` env
  - `clock.now_lsl() -> float`, `clock.now_wall_ns() -> int`, `clock.wall_iso(ns: int) -> str`
  - `warn.WarnOnce(logger: logging.Logger)` with `.check(key) -> bool`, `.warn(key, msg: str, *args) -> bool`, `.counts() -> dict`

- [ ] **Step 1: Capture the environment and requirements**

The `wahoo` conda env already exists (Python 3.12.0, bleak 3.0.2, pylsl 1.18.6 with bundled liblsl, pyobjc on macOS). These files record it so it can be recreated on Windows/Linux; bleak pulls in its own platform backend (pyobjc / winrt / dbus-fast) automatically.

**`requirements.txt`** (create):
```
bleak>=1.0
pylsl>=1.17
```

**`requirements-dev.txt`** (create):
```
-r requirements.txt
pytest>=8
```

**`environment.yml`** (create):
```yaml
# Recreate with: conda env create -f environment.yml   (then: conda run -n wahoo python -m pip install -e .)
name: wahoo
channels:
  - conda-forge
dependencies:
  - python=3.12
  - pip
  - pip:
      - -r requirements-dev.txt
```

**`pyproject.toml`** (create):
```toml
[project]
name = "wahoo-lsl"
version = "0.1.0"
description = "Stream and log Cycling Power Service data from a Wahoo KICKR over BLE"
requires-python = ">=3.10"
dependencies = [
    "bleak>=1.0",
    "pylsl>=1.17",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
wahoo-lsl = "wahoo_lsl.cli:main"

[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**`.gitignore`** (create):
```
__pycache__/
*.egg-info/
.pytest_cache/
build/
logs/
```

**`src/wahoo_lsl/__init__.py`** (create):
```python
"""Stream and log Cycling Power Service data from a Wahoo KICKR."""

__version__ = "0.1.0"
```

Run: `conda run -n wahoo python -m pip install -r requirements-dev.txt`
Expected: bleak and pylsl reported as already satisfied; pytest installed.

Run: `conda run -n wahoo python -m pip install -e .`
Expected: `Successfully installed wahoo-lsl-0.1.0` (editable). The `wahoo-lsl` console script now exists in the env.

- [ ] **Step 2: Write the failing tests**

**`tests/test_env.py`** (create):
```python
"""Guards the captured environment: required packages importable at the minimum versions."""
from importlib.metadata import version

import bleak  # noqa: F401
import pylsl

import wahoo_lsl


def _major_minor(v: str) -> tuple[int, int]:
    major, minor = v.split(".")[:2]
    return int(major), int(minor)


def test_bleak_version():
    assert _major_minor(version("bleak")) >= (1, 0)


def test_pylsl_version_and_liblsl_loaded():
    assert _major_minor(version("pylsl")) >= (1, 17)
    assert pylsl.library_version() > 0  # liblsl shared library actually loads


def test_package_installed():
    assert wahoo_lsl.__version__ == "0.1.0"
```

**`tests/test_clock.py`** (create):
```python
import time

import pylsl

from wahoo_lsl import clock


def test_now_lsl_is_monotonic_float():
    a = clock.now_lsl()
    b = clock.now_lsl()
    assert isinstance(a, float)
    assert b >= a


def test_now_lsl_is_pylsl_local_clock():
    before = pylsl.local_clock()
    t = clock.now_lsl()
    after = pylsl.local_clock()
    assert before <= t <= after


def test_now_wall_ns_is_system_realtime():
    before = time.time_ns()
    t = clock.now_wall_ns()
    after = time.time_ns()
    assert before <= t <= after


def test_wall_iso_epoch():
    assert clock.wall_iso(0) == "1970-01-01T00:00:00.000000+00:00"


def test_wall_iso_truncates_to_microseconds():
    assert clock.wall_iso(1_500_000_000 * 10**9 + 123_456_789) == "2017-07-14T02:40:00.123456+00:00"
```

**`tests/test_warn.py`** (create):
```python
import logging
import threading

from wahoo_lsl.warn import WarnOnce


def test_warns_once_per_key(caplog):
    w = WarnOnce(logging.getLogger("t"))
    with caplog.at_level(logging.WARNING, logger="t"):
        assert w.warn("a", "msg %s", 1) is True
        assert w.warn("a", "msg %s", 2) is False
        assert w.warn("b", "other") is True
    assert [r.getMessage() for r in caplog.records] == ["msg 1", "other"]
    assert w.counts() == {"a": 2, "b": 1}


def test_check_records_without_logging(caplog):
    w = WarnOnce(logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG, logger="t"):
        assert w.check(("k", 1)) is True
        assert w.check(("k", 1)) is False
    assert caplog.records == []
    assert w.counts() == {("k", 1): 2}


def test_thread_safe_single_emit(caplog):
    w = WarnOnce(logging.getLogger("t"))
    results = []

    def worker():
        for _ in range(200):
            results.append(w.warn("same", "x"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with caplog.at_level(logging.WARNING, logger="t"):
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert results.count(True) == 1
    assert w.counts()["same"] == 1600
    assert len(caplog.records) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_env.py tests/test_clock.py tests/test_warn.py -v`
Expected: FAIL with `ModuleNotFoundError` for `wahoo_lsl.clock` and `wahoo_lsl.warn`. (`tests/test_env.py` already passes: 3 passed.)

- [ ] **Step 4: Implement**

**`src/wahoo_lsl/clock.py`** (create):
```python
"""The only module that reads clocks (spec §4).

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
```

**`src/wahoo_lsl/warn.py`** (create):
```python
"""Thread-safe warn-once helper (spec §10)."""
from __future__ import annotations

import logging
import threading
from collections.abc import Hashable


class WarnOnce:
    """Emit a WARNING the first time a key is seen; count every occurrence."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._counts: dict[Hashable, int] = {}

    def check(self, key: Hashable) -> bool:
        """Record one occurrence of ``key``. Return True only for the first occurrence."""
        with self._lock:
            n = self._counts.get(key, 0)
            self._counts[key] = n + 1
        return n == 0

    def warn(self, key: Hashable, msg: str, *args: object) -> bool:
        """Log ``msg % args`` at WARNING the first time ``key`` is seen. Return True if logged."""
        first = self.check(key)
        if first:
            self._logger.warning(msg, *args)
        return first

    def counts(self) -> dict[Hashable, int]:
        with self._lock:
            return dict(self._counts)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_env.py tests/test_clock.py tests/test_warn.py -v`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add environment.yml requirements.txt requirements-dev.txt pyproject.toml .gitignore src tests
git commit -m "feat: capture wahoo conda env/requirements; clock and WarnOnce"
```

---

### Task 2: CPS measurement parser and vocabulary

**Files:**
- Create: `src/wahoo_lsl/cps.py`
- Test: `tests/test_cps.py`

**Interfaces:**
- Consumes: nothing.
- Produces (in `wahoo_lsl.cps`):
  - UUID strings `CPS_SERVICE_UUID`, `CP_MEASUREMENT_UUID`, `CP_FEATURE_UUID`, `SENSOR_LOCATION_UUID` (lowercase 128-bit form)
  - `class ParseError(ValueError)` with attribute `flags: int | None`
  - `@dataclass(frozen=True) MeasurementSpec(name: str, keys: tuple[str, ...], flag_bits: tuple[int, ...], feature_bit: int | None)`
  - `MEASUREMENTS: dict[str, MeasurementSpec]` — the 11 measurement names
  - `@dataclass(frozen=True) Measurement(flags: int, values: dict[str, object], present: frozenset[str])`
  - `parse_measurement(data: bytes | bytearray) -> Measurement` (raises `ParseError`)

- [ ] **Step 1: Write the failing tests**

**`tests/test_cps.py`** (create):
```python
import struct

import pytest

from wahoo_lsl.cps import MEASUREMENTS, ParseError, parse_measurement


def payload(flags: int, power: int, *parts: bytes) -> bytes:
    return struct.pack("<Hh", flags, power) + b"".join(parts)


def test_power_only():
    m = parse_measurement(payload(0, 250))
    assert m.flags == 0
    assert m.values == {"power_w": 250, "offset_compensation": False}
    assert m.present == {"power", "offset_compensation"}


def test_negative_power():
    assert parse_measurement(payload(0, -5)).values["power_w"] == -5


def test_accepts_bytearray():
    assert parse_measurement(bytearray(payload(0, 1))).values["power_w"] == 1


def test_pedal_power_balance_left_reference():
    m = parse_measurement(payload(0b11, 100, bytes([101])))
    assert m.values["pedal_power_balance_pct"] == 50.5
    assert m.values["pedal_power_balance_ref"] == "left"
    assert "pedal_power_balance" in m.present


def test_pedal_power_balance_unknown_reference():
    m = parse_measurement(payload(0b01, 100, bytes([100])))
    assert m.values["pedal_power_balance_ref"] == "unknown"


def test_accumulated_torque_crank_source():
    m = parse_measurement(payload(0b1100, 0, struct.pack("<H", 320)))
    assert m.values["accumulated_torque_nm"] == 10.0
    assert m.values["torque_source"] == "crank"
    assert "accumulated_torque" in m.present


def test_accumulated_torque_wheel_source():
    m = parse_measurement(payload(0b0100, 0, struct.pack("<H", 32)))
    assert m.values["accumulated_torque_nm"] == 1.0
    assert m.values["torque_source"] == "wheel"


def test_wheel_revolutions():
    m = parse_measurement(payload(1 << 4, 0, struct.pack("<IH", 100_000, 40_321)))
    assert m.values["wheel_revs"] == 100_000
    assert m.values["wheel_event_t"] == 40_321
    assert "wheel_revolutions" in m.present


def test_crank_revolutions():
    m = parse_measurement(payload(1 << 5, 0, struct.pack("<HH", 77, 1024)))
    assert m.values["crank_revs"] == 77
    assert m.values["crank_event_t"] == 1024
    assert "crank_revolutions" in m.present


def test_extreme_force():
    m = parse_measurement(payload(1 << 6, 0, struct.pack("<hh", 500, -20)))
    assert (m.values["max_force_n"], m.values["min_force_n"]) == (500, -20)
    assert "extreme_force" in m.present


def test_extreme_torque():
    m = parse_measurement(payload(1 << 7, 0, struct.pack("<hh", 64, -32)))
    assert (m.values["max_torque_nm"], m.values["min_torque_nm"]) == (2.0, -1.0)
    assert "extreme_torque" in m.present


def test_extreme_angles_bit_packing():
    packed = 270 | (90 << 12)
    m = parse_measurement(payload(1 << 8, 0, packed.to_bytes(3, "little")))
    assert (m.values["max_angle_deg"], m.values["min_angle_deg"]) == (270, 90)
    assert "extreme_angles" in m.present


def test_dead_spot_angles_both():
    m = parse_measurement(payload((1 << 9) | (1 << 10), 0, struct.pack("<HH", 10, 190)))
    assert m.values["top_dead_spot_deg"] == 10
    assert m.values["bottom_dead_spot_deg"] == 190
    assert "dead_spot_angles" in m.present


def test_dead_spot_angles_top_only():
    m = parse_measurement(payload(1 << 9, 0, struct.pack("<H", 10)))
    assert m.values["top_dead_spot_deg"] == 10
    assert "bottom_dead_spot_deg" not in m.values
    assert "dead_spot_angles" in m.present


def test_accumulated_energy():
    m = parse_measurement(payload(1 << 11, 0, struct.pack("<H", 1234)))
    assert m.values["accumulated_energy_kj"] == 1234
    assert "accumulated_energy" in m.present


def test_offset_compensation_indicator():
    assert parse_measurement(payload(1 << 12, 0)).values["offset_compensation"] is True


def test_combined_fields_follow_spec_order():
    flags = (1 << 2) | (1 << 4) | (1 << 5) | (1 << 11)
    data = payload(
        flags,
        300,
        struct.pack("<H", 64),
        struct.pack("<IH", 5, 2048),
        struct.pack("<HH", 3, 1024),
        struct.pack("<H", 7),
    )
    m = parse_measurement(data)
    assert m.values == {
        "power_w": 300,
        "accumulated_torque_nm": 2.0,
        "torque_source": "wheel",
        "wheel_revs": 5,
        "wheel_event_t": 2048,
        "crank_revs": 3,
        "crank_event_t": 1024,
        "accumulated_energy_kj": 7,
        "offset_compensation": False,
    }
    assert m.present == {
        "power",
        "accumulated_torque",
        "wheel_revolutions",
        "crank_revolutions",
        "accumulated_energy",
        "offset_compensation",
    }


@pytest.mark.parametrize(
    "bit, name",
    [
        (0, "pedal_power_balance"),
        (2, "accumulated_torque"),
        (4, "wheel_revolutions"),
        (5, "crank_revolutions"),
        (6, "extreme_force"),
        (7, "extreme_torque"),
        (8, "extreme_angles"),
        (9, "top_dead_spot_angle"),
        (10, "bottom_dead_spot_angle"),
        (11, "accumulated_energy"),
    ],
)
def test_truncated_optional_field_names_field_and_keeps_flags(bit, name):
    with pytest.raises(ParseError) as exc:
        parse_measurement(payload(1 << bit, 0))
    assert name in str(exc.value)
    assert exc.value.flags == 1 << bit


def test_empty_payload():
    with pytest.raises(ParseError) as exc:
        parse_measurement(b"")
    assert "flags" in str(exc.value)
    assert exc.value.flags is None


def test_missing_power():
    with pytest.raises(ParseError) as exc:
        parse_measurement(struct.pack("<H", 0))
    assert "power" in str(exc.value)
    assert exc.value.flags == 0


def test_trailing_bytes_reported_not_error():
    m = parse_measurement(payload(0, 1, b"\xaa\xbb"))
    assert m.values["_trailing_bytes"] == 2


def test_vocabulary_names_and_keys_unique():
    assert set(MEASUREMENTS) == {
        "power",
        "pedal_power_balance",
        "accumulated_torque",
        "wheel_revolutions",
        "crank_revolutions",
        "extreme_force",
        "extreme_torque",
        "extreme_angles",
        "dead_spot_angles",
        "accumulated_energy",
        "offset_compensation",
    }
    keys = [k for spec in MEASUREMENTS.values() for k in spec.keys]
    assert len(keys) == len(set(keys))


def test_vocabulary_keys_match_parser_output():
    data = payload(
        0x1FFF,
        1,
        bytes([1]),
        struct.pack("<H", 1),
        struct.pack("<IH", 1, 1),
        struct.pack("<HH", 1, 1),
        struct.pack("<hh", 1, 1),
        struct.pack("<hh", 1, 1),
        (1).to_bytes(3, "little"),
        struct.pack("<H", 1),
        struct.pack("<H", 1),
        struct.pack("<H", 1),
    )
    m = parse_measurement(data)
    assert set(m.values) == {k for spec in MEASUREMENTS.values() for k in spec.keys}
    assert m.present == set(MEASUREMENTS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_cps.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.cps'`.

- [ ] **Step 3: Implement**

**`src/wahoo_lsl/cps.py`** (create):
```python
"""Cycling Power Service constants and pure parsers (spec §6).

No I/O, no bleak, no threading: everything here is testable with byte fixtures.
All multi-byte values are little-endian.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


CPS_SERVICE_UUID = _uuid16(0x1818)
CP_MEASUREMENT_UUID = _uuid16(0x2A63)
CP_FEATURE_UUID = _uuid16(0x2A65)
SENSOR_LOCATION_UUID = _uuid16(0x2A5D)


class ParseError(ValueError):
    """Payload could not be decoded. ``flags`` is set if the flags field was read."""

    def __init__(self, message: str, flags: int | None = None) -> None:
        super().__init__(message)
        self.flags = flags


@dataclass(frozen=True)
class MeasurementSpec:
    name: str
    keys: tuple[str, ...]  # output keys in Measurement.values
    flag_bits: tuple[int, ...]  # CP Measurement flag bits; () = always present
    feature_bit: int | None  # CP Feature bit; None = always supported


# Single source of truth for the config vocabulary (spec §6.3).
MEASUREMENTS: dict[str, MeasurementSpec] = {
    spec.name: spec
    for spec in (
        MeasurementSpec("power", ("power_w",), (), None),
        MeasurementSpec("pedal_power_balance", ("pedal_power_balance_pct", "pedal_power_balance_ref"), (0,), 0),
        MeasurementSpec("accumulated_torque", ("accumulated_torque_nm", "torque_source"), (2,), 1),
        MeasurementSpec("wheel_revolutions", ("wheel_revs", "wheel_event_t"), (4,), 2),
        MeasurementSpec("crank_revolutions", ("crank_revs", "crank_event_t"), (5,), 3),
        MeasurementSpec("extreme_force", ("max_force_n", "min_force_n"), (6,), 4),
        MeasurementSpec("extreme_torque", ("max_torque_nm", "min_torque_nm"), (7,), 4),
        MeasurementSpec("extreme_angles", ("max_angle_deg", "min_angle_deg"), (8,), 5),
        MeasurementSpec("dead_spot_angles", ("top_dead_spot_deg", "bottom_dead_spot_deg"), (9, 10), 6),
        MeasurementSpec("accumulated_energy", ("accumulated_energy_kj",), (11,), 7),
        MeasurementSpec("offset_compensation", ("offset_compensation",), (), 8),
    )
}


@dataclass(frozen=True)
class Measurement:
    flags: int
    values: dict[str, object]  # every output key decoded from this payload
    present: frozenset[str]  # measurement names present in this payload


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self.pos

    def read(self, fmt: str, what: str) -> tuple:
        size = struct.calcsize(fmt)
        if size > self.remaining:
            raise ParseError(
                f"truncated payload reading {what}: need {size} bytes at offset {self.pos}, have {self.remaining}"
            )
        values = struct.unpack_from(fmt, self._data, self.pos)
        self.pos += size
        return values


def parse_measurement(data: bytes | bytearray) -> Measurement:
    """Decode a CP Measurement (0x2A63) payload. Raises ParseError if truncated."""
    reader = _Reader(bytes(data))
    (flags,) = reader.read("<H", "flags")
    try:
        return _parse_body(reader, flags)
    except ParseError as e:
        raise ParseError(str(e), flags=flags) from None


def _parse_body(r: _Reader, flags: int) -> Measurement:
    def has(bit: int) -> bool:
        return bool((flags >> bit) & 1)

    (power,) = r.read("<h", "power")
    values: dict[str, object] = {"power_w": power}
    present = {"power", "offset_compensation"}

    if has(0):
        (balance,) = r.read("<B", "pedal_power_balance")
        values["pedal_power_balance_pct"] = balance / 2
        values["pedal_power_balance_ref"] = "left" if has(1) else "unknown"
        present.add("pedal_power_balance")
    if has(2):
        (torque,) = r.read("<H", "accumulated_torque")
        values["accumulated_torque_nm"] = torque / 32
        values["torque_source"] = "crank" if has(3) else "wheel"
        present.add("accumulated_torque")
    if has(4):
        revs, event_t = r.read("<IH", "wheel_revolutions")
        values["wheel_revs"] = revs
        values["wheel_event_t"] = event_t
        present.add("wheel_revolutions")
    if has(5):
        revs, event_t = r.read("<HH", "crank_revolutions")
        values["crank_revs"] = revs
        values["crank_event_t"] = event_t
        present.add("crank_revolutions")
    if has(6):
        f_max, f_min = r.read("<hh", "extreme_force")
        values["max_force_n"] = f_max
        values["min_force_n"] = f_min
        present.add("extreme_force")
    if has(7):
        t_max, t_min = r.read("<hh", "extreme_torque")
        values["max_torque_nm"] = t_max / 32
        values["min_torque_nm"] = t_min / 32
        present.add("extreme_torque")
    if has(8):
        b0, b1, b2 = r.read("<BBB", "extreme_angles")
        packed = b0 | (b1 << 8) | (b2 << 16)
        values["max_angle_deg"] = packed & 0xFFF
        values["min_angle_deg"] = packed >> 12
        present.add("extreme_angles")
    if has(9):
        (top,) = r.read("<H", "top_dead_spot_angle")
        values["top_dead_spot_deg"] = top
    if has(10):
        (bottom,) = r.read("<H", "bottom_dead_spot_angle")
        values["bottom_dead_spot_deg"] = bottom
    if has(9) or has(10):
        present.add("dead_spot_angles")
    if has(11):
        (energy,) = r.read("<H", "accumulated_energy")
        values["accumulated_energy_kj"] = energy
        present.add("accumulated_energy")
    values["offset_compensation"] = has(12)
    if r.remaining:
        values["_trailing_bytes"] = r.remaining
    return Measurement(flags, values, frozenset(present))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_cps.py -v`
Expected: all passed (32 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/cps.py tests/test_cps.py
git commit -m "feat: CP Measurement parser and measurement vocabulary"
```

---

### Task 3: CP Feature decode and Sensor Location

**Files:**
- Modify: `src/wahoo_lsl/cps.py` (append)
- Test: `tests/test_cps.py` (append)

**Interfaces:**
- Consumes: `MEASUREMENTS`, `ParseError` from Task 2.
- Produces (in `wahoo_lsl.cps`):
  - `FEATURE_BITS: dict[int, str]` (bits 0–19), `DISTRIBUTED_SYSTEM_SUPPORT = "distributed_system_support"`
  - `@dataclass(frozen=True) Features(raw: int, flags: dict[str, bool | int], supported: frozenset[str])`
  - `parse_features(data: bytes | bytearray) -> Features` (raises `ParseError` if < 4 bytes)
  - `SENSOR_LOCATIONS: tuple[str, ...]`, `sensor_location_name(code: int) -> str`

- [ ] **Step 1: Write the failing tests**

**`tests/test_cps.py`** (append):
```python


from wahoo_lsl.cps import FEATURE_BITS, parse_features, sensor_location_name  # noqa: E402


def feat(raw: int) -> bytes:
    return struct.pack("<I", raw)


def test_features_none_supported_still_has_power():
    f = parse_features(feat(0))
    assert f.raw == 0
    assert f.supported == {"power"}
    assert f.flags["wheel_revolution_data"] is False
    assert f.flags["distributed_system_support"] == 0


def test_features_wheel_and_crank():
    f = parse_features(feat(0b1100))
    assert f.supported == {"power", "wheel_revolutions", "crank_revolutions"}
    assert f.flags["crank_revolution_data"] is True


def test_extreme_magnitudes_force_based_context():
    assert parse_features(feat(1 << 4)).supported == {"power", "extreme_force"}


def test_extreme_magnitudes_torque_based_context():
    f = parse_features(feat((1 << 4) | (1 << 16)))
    assert f.supported == {"power", "extreme_torque"}
    assert f.flags["sensor_measurement_context"] is True


def test_feature_bit_mapping_for_each_measurement():
    for spec in MEASUREMENTS.values():
        if spec.feature_bit is None or spec.name == "extreme_torque":
            continue
        assert spec.name in parse_features(feat(1 << spec.feature_bit)).supported


def test_all_named_bits_decoded():
    f = parse_features(feat(0x000FFFFF))
    assert all(f.flags[name] is True for name in FEATURE_BITS.values())


def test_distributed_system_support_two_bit_value():
    assert parse_features(feat(2 << 20)).flags["distributed_system_support"] == 2
    assert parse_features(feat(3 << 20)).flags["distributed_system_support"] == 3


def test_features_short_payload():
    with pytest.raises(ParseError):
        parse_features(b"\x01\x02\x03")


def test_features_extra_bytes_ignored():
    assert "wheel_revolutions" in parse_features(feat(0b100) + b"\xff").supported


def test_sensor_location_names():
    assert sensor_location_name(0) == "Other"
    assert sensor_location_name(13) == "Rear hub"
    assert sensor_location_name(16) == "Chain ring"
    assert sensor_location_name(17) == "Unknown(17)"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_cps.py -v`
Expected: FAIL with `ImportError: cannot import name 'FEATURE_BITS'`.

- [ ] **Step 3: Implement**

**`src/wahoo_lsl/cps.py`** (append):
```python


# CP Feature (0x2A65) named single bits (spec §6.4). Bits 20–21 are a 2-bit value.
FEATURE_BITS: dict[int, str] = {
    0: "pedal_power_balance",
    1: "accumulated_torque",
    2: "wheel_revolution_data",
    3: "crank_revolution_data",
    4: "extreme_magnitudes",
    5: "extreme_angles",
    6: "dead_spot_angles",
    7: "accumulated_energy",
    8: "offset_compensation_indicator",
    9: "offset_compensation",
    10: "content_masking",
    11: "multiple_sensor_locations",
    12: "crank_length_adjustment",
    13: "chain_length_adjustment",
    14: "chain_weight_adjustment",
    15: "span_length_adjustment",
    16: "sensor_measurement_context",
    17: "instantaneous_measurement_direction",
    18: "factory_calibration_date",
    19: "enhanced_offset_compensation",
}
DISTRIBUTED_SYSTEM_SUPPORT = "distributed_system_support"
_TORQUE_CONTEXT_BIT = 16  # 0 = force-based, 1 = torque-based


@dataclass(frozen=True)
class Features:
    raw: int
    flags: dict[str, bool | int]  # every FEATURE_BITS name -> bool, plus distributed_system_support -> int 0–3
    supported: frozenset[str]  # measurement names from MEASUREMENTS; always includes "power"


def parse_features(data: bytes | bytearray) -> Features:
    """Decode the 32-bit CP Feature characteristic. Extra bytes are ignored."""
    data = bytes(data)
    if len(data) < 4:
        raise ParseError(f"CP Feature needs 4 bytes, got {len(data)}")
    (raw,) = struct.unpack_from("<I", data)
    flags: dict[str, bool | int] = {name: bool((raw >> bit) & 1) for bit, name in FEATURE_BITS.items()}
    flags[DISTRIBUTED_SYSTEM_SUPPORT] = (raw >> 20) & 0b11
    torque_based = bool((raw >> _TORQUE_CONTEXT_BIT) & 1)
    supported: set[str] = set()
    for spec in MEASUREMENTS.values():
        if spec.feature_bit is None:
            supported.add(spec.name)
            continue
        if not (raw >> spec.feature_bit) & 1:
            continue
        if spec.name == "extreme_force" and torque_based:
            continue
        if spec.name == "extreme_torque" and not torque_based:
            continue
        supported.add(spec.name)
    return Features(raw, flags, frozenset(supported))


SENSOR_LOCATIONS: tuple[str, ...] = (
    "Other",
    "Top of shoe",
    "In shoe",
    "Hip",
    "Front wheel",
    "Left crank",
    "Right crank",
    "Left pedal",
    "Right pedal",
    "Front hub",
    "Rear dropout",
    "Chainstay",
    "Rear wheel",
    "Rear hub",
    "Chest",
    "Spider",
    "Chain ring",
)


def sensor_location_name(code: int) -> str:
    return SENSOR_LOCATIONS[code] if 0 <= code < len(SENSOR_LOCATIONS) else f"Unknown({code})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_cps.py -v`
Expected: all passed (42 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/cps.py tests/test_cps.py
git commit -m "feat: CP Feature decode and sensor location names"
```

---

### Task 4: Config loading, validation and platform identifier selection

**Files:**
- Create: `src/wahoo_lsl/config.py`, `config.example.json`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `cps.MEASUREMENTS`.
- Produces (in `wahoo_lsl.config`):
  - `class ConfigError(ValueError)`
  - frozen dataclasses `DeviceConfig(name: str, mac: str | None = None, uuid: str | None = None, scan_timeout_s: float = 10.0)`, `LogConfig(path: str = "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl", flush_every_n: int = 10)`, `ReconnectConfig(enabled: bool = True, max_attempts: int = 0, backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0))`, `Config(device: DeviceConfig, measurements: tuple[str, ...] = (), log: LogConfig, reconnect: ReconnectConfig, queue_warn_depth: int = 100)`
  - `load_config(path: str | Path, *, require_measurements: bool = True) -> Config`
  - `parse_config(obj: Any, *, require_measurements: bool = True) -> Config`
  - `address_kind(platform: str | None = None) -> str` → `"uuid"` on darwin else `"mac"`
  - `select_identifier(device: DeviceConfig, platform: str | None = None) -> tuple[str, str]` → `("mac"|"uuid"|"name", value)`

- [ ] **Step 1: Create the example config**

**`config.example.json`** (create):
```json
{
  "device": {
    "name": "KICKR",
    "mac": null,
    "uuid": null,
    "scan_timeout_s": 10
  },
  "measurements": ["power", "wheel_revolutions", "accumulated_torque"],
  "log": {
    "path": "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl",
    "flush_every_n": 10
  },
  "reconnect": {
    "enabled": true,
    "max_attempts": 0,
    "backoff_s": [1, 2, 5, 10]
  },
  "queue_warn_depth": 100
}
```

- [ ] **Step 2: Write the failing tests**

**`tests/test_config.py`** (create):
```python
import sys
from pathlib import Path

import pytest

from wahoo_lsl.config import (
    ConfigError,
    DeviceConfig,
    address_kind,
    load_config,
    parse_config,
    select_identifier,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"
MAC = "AA:BB:CC:DD:EE:FF"
UUID = "12345678-1234-5678-1234-567812345678"


def base() -> dict:
    return {"device": {"name": "KICKR"}, "measurements": ["power"]}


def _set(path: str, value):
    def mutate(d: dict) -> None:
        *parents, last = path.split(".")
        target = d
        for p in parents:
            target = target.setdefault(p, {})
        target[last] = value

    return mutate


def _delete(key: str):
    def mutate(d: dict) -> None:
        del d[key]

    return mutate


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert cfg.device == DeviceConfig(name="KICKR", mac=None, uuid=None, scan_timeout_s=10.0)
    assert cfg.measurements == ("power", "wheel_revolutions", "accumulated_torque")
    assert cfg.log.path == "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl"
    assert cfg.log.flush_every_n == 10
    assert cfg.reconnect.enabled is True
    assert cfg.reconnect.max_attempts == 0
    assert cfg.reconnect.backoff_s == (1.0, 2.0, 5.0, 10.0)
    assert cfg.queue_warn_depth == 100


def test_defaults_applied():
    cfg = parse_config(base())
    assert cfg.log.path == "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl"
    assert cfg.log.flush_every_n == 10
    assert cfg.reconnect.enabled is True
    assert cfg.reconnect.max_attempts == 0
    assert cfg.reconnect.backoff_s == (1.0, 2.0, 5.0, 10.0)
    assert cfg.device.scan_timeout_s == 10.0
    assert cfg.queue_warn_depth == 100


def test_name_is_stripped_and_mac_uuid_accepted():
    d = base()
    d["device"] = {"name": " KICKR ", "mac": MAC.lower(), "uuid": UUID}
    cfg = parse_config(d)
    assert cfg.device.name == "KICKR"
    assert cfg.device.mac == MAC.lower()
    assert cfg.device.uuid == UUID


def test_measurements_optional_when_not_required():
    cfg = parse_config({"device": {"name": "KICKR"}}, require_measurements=False)
    assert cfg.measurements == ()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (_set("extra", 1), "unknown top-level"),
        (_delete("device"), "device"),
        (_set("device", "KICKR"), "'device' must be an object"),
        (_set("device.name", ""), "device.name"),
        (_set("device.colour", "red"), "unknown key"),
        (_set("device.mac", "not-a-mac"), "device.mac"),
        (_set("device.uuid", "nope"), "device.uuid"),
        (_set("device.scan_timeout_s", 0), "scan_timeout_s"),
        (_set("device.scan_timeout_s", True), "scan_timeout_s"),
        (_delete("measurements"), "measurements"),
        (_set("measurements", []), "measurements"),
        (_set("measurements", ["power", "watts"]), "watts"),
        (_set("measurements", ["power", "power"]), "duplicate"),
        (_set("log.path", ""), "log.path"),
        (_set("log.path", "logs/{foo}.jsonl"), "log.path"),
        (_set("log.path", "logs/{start.nope}.jsonl"), "log.path"),
        (_set("log.path", "logs/{.jsonl"), "log.path"),
        (_set("log.flush_every_n", 0), "flush_every_n"),
        (_set("reconnect.enabled", "yes"), "reconnect.enabled"),
        (_set("reconnect.max_attempts", -1), "max_attempts"),
        (_set("reconnect.backoff_s", []), "backoff_s"),
        (_set("reconnect.backoff_s", [1, -2]), "backoff_s"),
        (_set("queue_warn_depth", 0), "queue_warn_depth"),
    ],
)
def test_invalid_configs_rejected(mutate, message):
    d = base()
    mutate(d)
    with pytest.raises(ConfigError) as exc:
        parse_config(d)
    assert message in str(exc.value)


def test_non_object_config_rejected():
    with pytest.raises(ConfigError):
        parse_config(["device"])


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "missing.json")


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(p)


@pytest.mark.parametrize(
    "platform, mac, uuid, expected",
    [
        ("darwin", MAC, UUID, ("uuid", UUID)),
        ("darwin", MAC, None, ("name", "KICKR")),
        ("win32", MAC, UUID, ("mac", MAC)),
        ("linux", MAC, UUID, ("mac", MAC)),
        ("linux", None, UUID, ("name", "KICKR")),
        ("freebsd", MAC, None, ("mac", MAC)),
    ],
)
def test_select_identifier(platform, mac, uuid, expected):
    device = DeviceConfig(name="KICKR", mac=mac, uuid=uuid)
    assert select_identifier(device, platform) == expected


def test_address_kind_defaults_to_sys_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert address_kind() == "uuid"
    monkeypatch.setattr(sys, "platform", "win32")
    assert address_kind() == "mac"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.config'`.

- [ ] **Step 4: Implement**

**`src/wahoo_lsl/config.py`** (create):
```python
"""Load and validate the JSON config (spec §5)."""
from __future__ import annotations

import json
import logging
import re
import sys
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .cps import MEASUREMENTS

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")
_TOP_KEYS = {"device", "measurements", "log", "reconnect", "queue_warn_depth"}


class ConfigError(ValueError):
    """Invalid or unreadable config. CLI exit code 2."""


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    mac: str | None = None  # used on Windows/Linux only
    uuid: str | None = None  # used on macOS only
    scan_timeout_s: float = 10.0


@dataclass(frozen=True)
class LogConfig:
    path: str = "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl"
    flush_every_n: int = 10


@dataclass(frozen=True)
class ReconnectConfig:
    enabled: bool = True
    max_attempts: int = 0  # 0 = unlimited
    backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)


@dataclass(frozen=True)
class Config:
    device: DeviceConfig
    measurements: tuple[str, ...] = ()
    log: LogConfig = field(default_factory=LogConfig)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    queue_warn_depth: int = 100


def load_config(path: str | Path, *, require_measurements: bool = True) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from None
    return parse_config(obj, require_measurements=require_measurements)


def parse_config(obj: Any, *, require_measurements: bool = True) -> Config:
    if not isinstance(obj, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(obj) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")
    if "device" not in obj:
        raise ConfigError("missing required section 'device'")
    return Config(
        device=_parse_device(_section(obj, "device", {"name", "mac", "uuid", "scan_timeout_s"})),
        measurements=_parse_measurements(obj.get("measurements"), require_measurements),
        log=_parse_log(_section(obj, "log", {"path", "flush_every_n"})),
        reconnect=_parse_reconnect(_section(obj, "reconnect", {"enabled", "max_attempts", "backoff_s"})),
        queue_warn_depth=_int(obj.get("queue_warn_depth", 100), "queue_warn_depth", minimum=1),
    )


def address_kind(platform: str | None = None) -> str:
    """'uuid' on macOS (CoreBluetooth hides MAC addresses), 'mac' everywhere else."""
    return "uuid" if (platform or sys.platform) == "darwin" else "mac"


def select_identifier(device: DeviceConfig, platform: str | None = None) -> tuple[str, str]:
    """Pick how to find the device on this platform: ('mac'|'uuid', value) or ('name', name)."""
    kind = address_kind(platform)
    if kind == "uuid":
        value, other_kind, other = device.uuid, "mac", device.mac
    else:
        value, other_kind, other = device.mac, "uuid", device.uuid
    if other is not None:
        log.debug("ignoring device.%s=%s on platform %s", other_kind, other, platform or sys.platform)
    if value:
        return kind, value
    return "name", device.name


def _section(obj: dict, key: str, allowed: set[str]) -> dict:
    section = obj.get(key, {})
    if not isinstance(section, dict):
        raise ConfigError(f"'{key}' must be an object")
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in '{key}': {sorted(unknown)}")
    return section


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pos_number(value: Any, where: str) -> float:
    if not _is_number(value) or value <= 0:
        raise ConfigError(f"{where} must be a positive number, got {value!r}")
    return float(value)


def _int(value: Any, where: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}, got {value!r}")
    return value


def _opt_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string or null, got {value!r}")
    return value.strip()


def _parse_device(d: dict) -> DeviceConfig:
    name = d.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("device.name must be a non-empty string")
    mac = _opt_str(d.get("mac"), "device.mac")
    if mac is not None and not _MAC_RE.fullmatch(mac):
        raise ConfigError(f"device.mac must look like AA:BB:CC:DD:EE:FF, got {mac!r}")
    uuid = _opt_str(d.get("uuid"), "device.uuid")
    if uuid is not None:
        try:
            uuidlib.UUID(uuid)
        except ValueError:
            raise ConfigError(f"device.uuid is not a valid UUID: {uuid!r}") from None
    return DeviceConfig(
        name=name.strip(),
        mac=mac,
        uuid=uuid,
        scan_timeout_s=_pos_number(d.get("scan_timeout_s", 10), "device.scan_timeout_s"),
    )


def _parse_measurements(m: Any, required: bool) -> tuple[str, ...]:
    if m is None:
        if required:
            raise ConfigError("missing required 'measurements' list")
        return ()
    if not isinstance(m, list) or not m or not all(isinstance(x, str) for x in m):
        raise ConfigError("measurements must be a non-empty list of strings")
    unknown = [x for x in m if x not in MEASUREMENTS]
    if unknown:
        raise ConfigError(f"unknown measurement(s) {unknown}; valid names: {sorted(MEASUREMENTS)}")
    if len(set(m)) != len(m):
        raise ConfigError(f"duplicate measurement names in {m}")
    return tuple(m)


def _parse_log(lg: dict) -> LogConfig:
    path = lg.get("path", LogConfig.path)
    if not isinstance(path, str) or not path.strip():
        raise ConfigError("log.path must be a non-empty string")
    try:
        path.format(start=datetime.now())
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise ConfigError(f"log.path template is invalid ({e!r}); only {{start:<strftime>}} is supported") from None
    return LogConfig(path=path, flush_every_n=_int(lg.get("flush_every_n", 10), "log.flush_every_n", minimum=1))


def _parse_reconnect(rc: dict) -> ReconnectConfig:
    enabled = rc.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("reconnect.enabled must be true or false")
    backoff = rc.get("backoff_s", [1, 2, 5, 10])
    if not isinstance(backoff, list) or not backoff:
        raise ConfigError("reconnect.backoff_s must be a non-empty list of positive numbers")
    return ReconnectConfig(
        enabled=enabled,
        max_attempts=_int(rc.get("max_attempts", 0), "reconnect.max_attempts", minimum=0),
        backoff_s=tuple(_pos_number(b, "reconnect.backoff_s[]") for b in backoff),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_config.py -v`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add config.example.json src/wahoo_lsl/config.py tests/test_config.py
git commit -m "feat: config loading, validation and per-platform device identifier"
```

---

### Task 5: Pipeline — types, dispatcher, sink runners

**Files:**
- Create: `src/wahoo_lsl/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `clock.now_lsl/now_wall_ns`, `cps.parse_measurement/ParseError`, `warn.WarnOnce`.
- Produces (in `wahoo_lsl.pipeline`):
  - frozen slotted dataclasses `RawSample(seq: int, t_lsl: float, t_wall_ns: int, data: bytes)`, `Event(kind: str, t_lsl: float, t_wall_ns: int, detail: dict = {})`, `Sample(seq, t_lsl, t_wall_ns, raw_hex: str, flags: int | None, fields: dict, parse_error: str | None = None)`
  - `STOP` sentinel; `make_event(kind: str, **detail) -> Event`
  - `class Sink(ABC)`: attribute `name: str`; methods `start()`, abstract `handle(item: Sample | Event)`, `stop()`
  - `Dispatcher(ingest_q, sink_queues: dict[str, queue.Queue], configured: Sequence[str], warn: WarnOnce, *, queue_warn_depth=100, backlog_interval_s=10.0)` (a `threading.Thread`; attribute `parse_errors: int`)
  - `Pipeline(sinks: Sequence[Sink], configured: Sequence[str], warn: WarnOnce, *, queue_warn_depth=100)` with `.start()`, `.put(item)`, `.close(timeout=5.0) -> bool`, attribute `ingest_q`
  - Event kinds used across tasks: `clock_sync`, `connected`, `features` (detail: `raw`, `supported`, `unavailable`, `sensor_location`), `disconnected`, `reconnect_attempt`, `reconnected`, `stopped` (dispatcher adds `parse_errors`).

- [ ] **Step 1: Write the failing tests**

**`tests/test_pipeline.py`** (create):
```python
import logging
import queue
import struct

import pytest

from wahoo_lsl import clock
from wahoo_lsl.pipeline import (
    STOP,
    Dispatcher,
    Event,
    Pipeline,
    RawSample,
    Sample,
    Sink,
    make_event,
)
from wahoo_lsl.warn import WarnOnce

POWER_ONLY = struct.pack("<Hh", 0, 200)
WHEEL = struct.pack("<Hh", 1 << 4, 150) + struct.pack("<IH", 10, 2048)
TRUNCATED_WHEEL = struct.pack("<Hh", 1 << 4, 5)


class RecordingSink(Sink):
    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.items: list = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def handle(self, item) -> None:
        self.items.append(item)

    def stop(self) -> None:
        self.stopped = True


class BrokenSink(Sink):
    name = "broken"

    def handle(self, item) -> None:
        raise RuntimeError("boom")


class UnstartableSink(Sink):
    name = "unstartable"

    def start(self) -> None:
        raise OSError("cannot open")

    def handle(self, item) -> None:
        raise AssertionError("must not be called")


def raw(seq: int, data: bytes) -> RawSample:
    return RawSample(seq, 100.0 + seq, 1_700_000_000_000_000_000 + seq, data)


def features_event(unavailable=()) -> Event:
    return Event("features", 99.0, 1_700_000_000_000_000_000, {"unavailable": list(unavailable)})


def run_pipeline(items, sinks, configured=("power",)):
    warn = WarnOnce(logging.getLogger("wahoo_lsl.test"))
    p = Pipeline(sinks, configured, warn)
    p.start()
    for item in items:
        p.put(item)
    assert p.close(timeout=5) is True
    return p


def samples(sink):
    return [i for i in sink.items if isinstance(i, Sample)]


def test_samples_parsed_and_all_fields_forwarded_in_order():
    a, b = RecordingSink("a"), RecordingSink("b")
    run_pipeline([features_event(), raw(0, POWER_ONLY), raw(1, WHEEL)], [a, b], configured=("power",))
    assert a.started and a.stopped and b.started and b.stopped
    assert len(a.items) == len(b.items) == 3
    assert all(x is y for x, y in zip(a.items, b.items))
    assert isinstance(a.items[0], Event) and a.items[0].kind == "features"
    s0, s1 = samples(a)
    assert (s0.seq, s0.t_lsl, s0.t_wall_ns) == (0, 100.0, 1_700_000_000_000_000_000)
    assert s0.fields == {"power_w": 200, "offset_compensation": False}
    assert s1.raw_hex == WHEEL.hex()
    assert s1.flags == 1 << 4
    # no filtering: wheel fields are logged even though only "power" is configured
    assert s1.fields == {"power_w": 150, "wheel_revs": 10, "wheel_event_t": 2048, "offset_compensation": False}
    assert s1.parse_error is None


def test_missing_configured_field_warns_once(caplog):
    sink = RecordingSink()
    with caplog.at_level(logging.WARNING):
        run_pipeline(
            [features_event(), raw(0, POWER_ONLY), raw(1, POWER_ONLY), raw(2, POWER_ONLY)],
            [sink],
            configured=("power", "wheel_revolutions"),
        )
    warnings = [r.getMessage() for r in caplog.records if "not present in notification" in r.getMessage()]
    assert len(warnings) == 1
    assert "wheel_revolutions" in warnings[0]
    assert [s.fields["power_w"] for s in samples(sink)] == [200, 200, 200]


def test_unavailable_measurement_not_warned_again(caplog):
    sink = RecordingSink()
    with caplog.at_level(logging.WARNING):
        run_pipeline(
            [features_event(["wheel_revolutions"]), raw(0, POWER_ONLY)],
            [sink],
            configured=("power", "wheel_revolutions"),
        )
    assert not [r for r in caplog.records if "not present in notification" in r.getMessage()]


def test_parse_error_forwarded_with_raw_and_counted(caplog):
    sink = RecordingSink()
    stopped = Event("stopped", 200.0, 1_700_000_000_000_000_000, {"reason": "user"})
    with caplog.at_level(logging.WARNING):
        run_pipeline([raw(0, TRUNCATED_WHEEL), raw(1, TRUNCATED_WHEEL), stopped], [sink])
    s0, s1 = samples(sink)
    for s in (s0, s1):
        assert s.fields == {}
        assert s.flags == 1 << 4
        assert s.raw_hex == TRUNCATED_WHEEL.hex()
        assert "wheel_revolutions" in s.parse_error
    parse_warnings = [r for r in caplog.records if "parse error" in r.getMessage()]
    assert len(parse_warnings) == 1
    final = sink.items[-1]
    assert final.kind == "stopped"
    assert final.detail == {"reason": "user", "parse_errors": 2}


def _run_dispatcher_without_consumers(n_items: int, depth: int, interval: float, caplog):
    ingest: queue.Queue = queue.Queue()
    sink_q: queue.Queue = queue.Queue()
    d = Dispatcher(
        ingest,
        {"slow": sink_q},
        ("power",),
        WarnOnce(logging.getLogger("wahoo_lsl.test")),
        queue_warn_depth=depth,
        backlog_interval_s=interval,
    )
    for i in range(n_items):
        ingest.put(raw(i, POWER_ONLY))
    ingest.put(STOP)
    with caplog.at_level(logging.WARNING):
        d.run()  # synchronous: no thread needed for this test
    assert sink_q.qsize() == n_items + 1  # samples + STOP, nothing dropped
    return [r.getMessage() for r in caplog.records if "backlog" in r.getMessage()]


def test_backlog_warning_rate_limited(caplog):
    backlog = _run_dispatcher_without_consumers(6, depth=2, interval=10.0, caplog=caplog)
    assert len(backlog) == 1
    assert "slow" in backlog[0] or "ingest" in backlog[0]


def test_backlog_warning_repeats_after_interval(caplog):
    backlog = _run_dispatcher_without_consumers(6, depth=2, interval=0.0, caplog=caplog)
    assert len(backlog) >= 2


def test_failing_sink_does_not_block_others(caplog):
    good = RecordingSink("good")
    with caplog.at_level(logging.ERROR):
        run_pipeline([raw(0, POWER_ONLY), raw(1, POWER_ONLY), raw(2, POWER_ONLY)], [BrokenSink(), good])
    assert len(samples(good)) == 3
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "broken" in r.getMessage()]
    assert len(errors) == 1


def test_sink_that_fails_to_start_is_drained(caplog):
    good = RecordingSink("good")
    with caplog.at_level(logging.ERROR):
        run_pipeline([raw(0, POWER_ONLY)], [UnstartableSink(), good])
    assert len(samples(good)) == 1
    assert any("unstartable" in r.getMessage() for r in caplog.records)


def test_duplicate_sink_names_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        Pipeline([RecordingSink("x"), RecordingSink("x")], (), WarnOnce(logging.getLogger("t")))


def test_make_event_reads_both_clocks():
    before = clock.now_lsl()
    e = make_event("clock_sync", a=1)
    after = clock.now_lsl()
    assert e.kind == "clock_sync"
    assert e.detail == {"a": 1}
    assert before <= e.t_lsl <= after
    assert isinstance(e.t_wall_ns, int)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.pipeline'`.

- [ ] **Step 3: Implement**

**`src/wahoo_lsl/pipeline.py`** (create):
```python
"""Queues, dispatcher and sink runners (spec §7).

BLE callback -> ingest_q -> Dispatcher (parse, warn, fan-out) -> one queue + thread per Sink.
Queues are unbounded: nothing is ever dropped. Items are immutable and shared by all sinks.
"""
from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from . import clock
from .cps import ParseError, parse_measurement
from .warn import WarnOnce

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RawSample:
    seq: int  # session-monotonic, starts at 0, never reset on reconnect
    t_lsl: float  # pylsl.local_clock() at callback entry
    t_wall_ns: int  # time.time_ns() immediately after
    data: bytes


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    t_lsl: float
    t_wall_ns: int
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Sample:
    seq: int
    t_lsl: float
    t_wall_ns: int
    raw_hex: str
    flags: int | None  # None if the payload was too short to contain flags
    fields: dict  # ALL parsed output keys (no config filtering in MVP)
    parse_error: str | None = None


class _Stop:
    def __repr__(self) -> str:
        return "STOP"


STOP = _Stop()


def make_event(kind: str, **detail: object) -> Event:
    """Create an Event stamped with both clocks, read back-to-back (t_lsl first)."""
    t_lsl = clock.now_lsl()
    t_wall_ns = clock.now_wall_ns()
    return Event(kind, t_lsl, t_wall_ns, dict(detail))


class Sink(ABC):
    """Consumer of Samples and Events. All methods are called on the sink's own thread."""

    name: str = "sink"

    def start(self) -> None:
        """Open resources."""

    @abstractmethod
    def handle(self, item: Sample | Event) -> None:
        """Process one item."""

    def stop(self) -> None:
        """Flush and close resources."""


class SinkRunner(threading.Thread):
    """Feeds one sink from its queue until STOP. A failing sink never blocks the dispatcher."""

    def __init__(self, sink: Sink, q: queue.Queue, warn: WarnOnce) -> None:
        super().__init__(name=f"sink-{sink.name}", daemon=True)
        self.sink = sink
        self.queue = q
        self._warn = warn

    def run(self) -> None:
        started = True
        try:
            self.sink.start()
        except Exception:
            log.exception("Sink %s failed to start; its data will be discarded", self.sink.name)
            started = False
        while True:
            item = self.queue.get()
            if item is STOP:
                break
            if not started:
                continue
            try:
                self.sink.handle(item)
            except Exception as e:
                if self._warn.check(("sink_error", self.sink.name, type(e).__name__)):
                    log.exception(
                        "Sink %s failed handling an item; further %s errors suppressed",
                        self.sink.name,
                        type(e).__name__,
                    )
        if started:
            try:
                self.sink.stop()
            except Exception:
                log.exception("Sink %s failed to stop cleanly", self.sink.name)


class Dispatcher(threading.Thread):
    """Parses RawSamples, applies warn-once availability checks, fans out to sink queues."""

    def __init__(
        self,
        ingest_q: queue.Queue,
        sink_queues: dict[str, queue.Queue],
        configured: Sequence[str],
        warn: WarnOnce,
        *,
        queue_warn_depth: int = 100,
        backlog_interval_s: float = 10.0,
    ) -> None:
        super().__init__(name="dispatcher", daemon=True)
        self._ingest = ingest_q
        self._sinks = sink_queues
        self._configured = tuple(configured)
        self._warn = warn
        self._depth = queue_warn_depth
        self._interval = backlog_interval_s
        self._unavailable: frozenset[str] = frozenset()
        self._last_backlog_warn: float | None = None
        self.parse_errors = 0

    def run(self) -> None:
        while True:
            item = self._ingest.get()
            if item is STOP:
                for q in self._sinks.values():
                    q.put(STOP)
                return
            if isinstance(item, RawSample):
                out: Sample | Event = self._process(item)
            elif isinstance(item, Event):
                out = self._on_event(item)
            else:
                log.error("Dispatcher ignoring unexpected item %r", item)
                continue
            for q in self._sinks.values():
                q.put(out)
            self._check_backlog()

    def _on_event(self, event: Event) -> Event:
        if event.kind == "features":
            self._unavailable = frozenset(event.detail.get("unavailable", ()))
        if event.kind == "stopped":
            return replace(event, detail={**event.detail, "parse_errors": self.parse_errors})
        return event

    def _process(self, raw: RawSample) -> Sample:
        raw_hex = raw.data.hex()
        try:
            m = parse_measurement(raw.data)
        except ParseError as e:
            self.parse_errors += 1
            msg = str(e)
            self._warn.warn(
                ("parse_error", msg),
                "CP Measurement parse error: %s (raw=%s); further identical errors suppressed",
                msg,
                raw_hex,
            )
            return Sample(raw.seq, raw.t_lsl, raw.t_wall_ns, raw_hex, e.flags, {}, msg)
        for name in self._configured:
            if name in m.present or name in self._unavailable:
                continue
            self._warn.warn(
                ("missing_field", name),
                "Measurement '%s' not present in notification (flags=0x%04x); further occurrences suppressed",
                name,
                m.flags,
            )
        return Sample(raw.seq, raw.t_lsl, raw.t_wall_ns, raw_hex, m.flags, dict(m.values), None)

    def _check_backlog(self) -> None:
        depths = {"ingest": self._ingest.qsize(), **{name: q.qsize() for name, q in self._sinks.items()}}
        over = {name: d for name, d in depths.items() if d > self._depth}
        if not over:
            return
        now = clock.now_lsl()
        if self._last_backlog_warn is not None and now - self._last_backlog_warn < self._interval:
            return
        self._last_backlog_warn = now
        log.warning("Queue backlog above %d items: %s", self._depth, over)


class Pipeline:
    """Owns the ingest queue, the dispatcher and one runner per sink."""

    def __init__(
        self,
        sinks: Sequence[Sink],
        configured: Sequence[str],
        warn: WarnOnce,
        *,
        queue_warn_depth: int = 100,
    ) -> None:
        names = [s.name for s in sinks]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate sink names: {names}")
        self.ingest_q: queue.Queue = queue.Queue()
        sink_queues = {s.name: queue.Queue() for s in sinks}
        self.dispatcher = Dispatcher(
            self.ingest_q, sink_queues, configured, warn, queue_warn_depth=queue_warn_depth
        )
        self.runners = [SinkRunner(s, sink_queues[s.name], warn) for s in sinks]

    def start(self) -> None:
        for runner in self.runners:
            runner.start()
        self.dispatcher.start()

    def put(self, item: RawSample | Event) -> None:
        """Thread-safe, non-blocking. Used as the BLE layer's emit callback."""
        self.ingest_q.put_nowait(item)

    def close(self, timeout: float = 5.0) -> bool:
        """Send STOP, join all threads. Return False if any thread failed to stop in time."""
        self.ingest_q.put(STOP)
        ok = True
        for thread in (self.dispatcher, *self.runners):
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Thread %s did not stop within %.1fs", thread.name, timeout)
                ok = False
        return ok
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_pipeline.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/pipeline.py tests/test_pipeline.py
git commit -m "feat: dispatcher, sink runners and pipeline wiring"
```

---

### Task 6: JSONL log sink and debug console sink

**Files:**
- Create: `src/wahoo_lsl/sinks/__init__.py`, `src/wahoo_lsl/sinks/log_sink.py`, `src/wahoo_lsl/sinks/console_sink.py`
- Test: `tests/test_sinks.py`

**Interfaces:**
- Consumes: `pipeline.Sink/Sample/Event`, `clock.wall_iso`.
- Produces (in `wahoo_lsl.sinks`):
  - `format_log_path(template: str, start: datetime) -> Path`
  - `sample_record(s: Sample) -> dict`, `event_record(e: Event) -> dict`
  - `LogSink(path: Path, flush_every_n: int = 10)` (name `"log"`)
  - `ConsoleSink(stream: TextIO | None = None)` (name `"console"`, defaults to `sys.stdout`)

- [ ] **Step 1: Write the failing tests**

**`tests/test_sinks.py`** (create):
```python
import io
import json
import re
from datetime import datetime
from pathlib import Path

from wahoo_lsl.pipeline import Event, Sample
from wahoo_lsl.sinks import ConsoleSink, LogSink, event_record, format_log_path, sample_record

WALL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def sample(seq=0, parse_error=None) -> Sample:
    fields = {} if parse_error else {"power_w": 165, "wheel_revs": 1203}
    return Sample(seq, 12345.678901234, 1_700_000_000_123_456_789, "0000a500", 0, fields, parse_error)


def event(kind="disconnected") -> Event:
    return Event(kind, 12350.1, 1_700_000_000_000_000_000, {"address": "AA:BB:CC:DD:EE:FF"})


def test_format_log_path():
    p = format_log_path("logs/cps_{start:%Y%m%d_%H%M%S}.jsonl", datetime(2026, 10, 20, 18, 1, 2))
    assert p == Path("logs/cps_20261020_180102.jsonl")


def test_sample_record_shape():
    rec = sample_record(sample())
    assert rec == {
        "type": "sample",
        "seq": 0,
        "t_lsl": 12345.678901234,
        "t_wall": "2023-11-14T22:13:20.123456+00:00",
        "raw": "0000a500",
        "flags": 0,
        "fields": {"power_w": 165, "wheel_revs": 1203},
    }


def test_sample_record_includes_parse_error_only_when_set():
    assert "parse_error" not in sample_record(sample())
    assert sample_record(sample(parse_error="truncated"))["parse_error"] == "truncated"


def test_event_record_shape():
    rec = event_record(event())
    assert rec["type"] == "event"
    assert rec["kind"] == "disconnected"
    assert rec["t_lsl"] == 12350.1
    assert WALL_RE.match(rec["t_wall"])
    assert rec["detail"] == {"address": "AA:BB:CC:DD:EE:FF"}


def test_log_sink_writes_jsonl_and_creates_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "log.jsonl"
    sink = LogSink(path, flush_every_n=10)
    sink.start()
    sink.handle(sample(0))
    sink.handle(event())
    sink.handle(sample(1, parse_error="truncated payload"))
    sink.stop()
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["type"] for r in records] == ["sample", "event", "sample"]
    assert records[0]["t_lsl"] == 12345.678901234  # full float precision survives
    assert records[2]["parse_error"] == "truncated payload"


def test_log_sink_flush_policy(tmp_path):
    path = tmp_path / "log.jsonl"
    sink = LogSink(path, flush_every_n=3)
    sink.start()
    sink.handle(sample(0))
    sink.handle(sample(1))
    assert path.read_text(encoding="utf-8") == ""  # buffered
    sink.handle(sample(2))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3  # flushed at N
    sink.handle(sample(3))
    sink.handle(event())  # events flush immediately
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5
    sink.stop()


def test_log_sink_appends_to_existing_file(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"type":"event"}\n', encoding="utf-8")
    sink = LogSink(path)
    sink.start()
    sink.handle(sample())
    sink.stop()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_console_sink_formats_samples_and_events():
    out = io.StringIO()
    sink = ConsoleSink(out)
    sink.handle(sample(42))
    sink.handle(sample(43, parse_error="truncated"))
    sink.handle(event())
    lines = out.getvalue().splitlines()
    assert lines[0] == "[   42] t_lsl=12345.678901  power_w=165 wheel_revs=1203"
    assert "PARSE_ERROR=truncated" in lines[1] and "raw=0000a500" in lines[1]
    assert lines[2].startswith("[event] disconnected ")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_sinks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.sinks'`.

- [ ] **Step 3: Implement**

**`src/wahoo_lsl/sinks/__init__.py`** (create):
```python
from .console_sink import ConsoleSink
from .log_sink import LogSink, event_record, format_log_path, sample_record

__all__ = ["ConsoleSink", "LogSink", "event_record", "format_log_path", "sample_record"]
```

**`src/wahoo_lsl/sinks/log_sink.py`** (create):
```python
"""JSONL data log sink (spec §7.5): one JSON object per line."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import IO

from ..clock import wall_iso
from ..pipeline import Event, Sample, Sink


def format_log_path(template: str, start: datetime) -> Path:
    """Expand ``{start:<strftime>}`` in the configured log path."""
    return Path(template.format(start=start))


def sample_record(s: Sample) -> dict:
    record = {
        "type": "sample",
        "seq": s.seq,
        "t_lsl": s.t_lsl,
        "t_wall": wall_iso(s.t_wall_ns),
        "raw": s.raw_hex,
        "flags": s.flags,
        "fields": s.fields,
    }
    if s.parse_error is not None:
        record["parse_error"] = s.parse_error
    return record


def event_record(e: Event) -> dict:
    return {
        "type": "event",
        "kind": e.kind,
        "t_lsl": e.t_lsl,
        "t_wall": wall_iso(e.t_wall_ns),
        "detail": e.detail,
    }


class LogSink(Sink):
    name = "log"

    def __init__(self, path: Path, flush_every_n: int = 10) -> None:
        self.path = Path(path)
        self._flush_every_n = flush_every_n
        self._file: IO[str] | None = None
        self._unflushed = 0

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8", newline="\n")

    def handle(self, item: Sample | Event) -> None:
        if isinstance(item, Sample):
            self._write(sample_record(item))
            self._unflushed += 1
            if self._unflushed >= self._flush_every_n:
                self._flush()
        else:
            self._write(event_record(item))
            self._flush()

    def stop(self) -> None:
        if self._file is not None:
            self._flush()
            self._file.close()
            self._file = None

    def _write(self, record: dict) -> None:
        assert self._file is not None, "LogSink.start() was not called"
        # json uses repr() for floats, so t_lsl keeps full precision.
        self._file.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def _flush(self) -> None:
        assert self._file is not None
        self._file.flush()
        self._unflushed = 0
```

**`src/wahoo_lsl/sinks/console_sink.py`** (create):
```python
"""Debug sink: print each parsed sample and event (spec §7.5, --debug only)."""
from __future__ import annotations

import sys
from typing import TextIO

from ..pipeline import Event, Sample, Sink


class ConsoleSink(Sink):
    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def handle(self, item: Sample | Event) -> None:
        if isinstance(item, Sample):
            fields = " ".join(f"{k}={v}" for k, v in item.fields.items())
            line = f"[{item.seq:>5}] t_lsl={item.t_lsl:.6f}  {fields}"
            if item.parse_error:
                line += f" PARSE_ERROR={item.parse_error} raw={item.raw_hex}"
        else:
            line = f"[event] {item.kind} {item.detail}"
        print(line, file=self._stream, flush=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_sinks.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/sinks tests/test_sinks.py
git commit -m "feat: JSONL log sink and debug console sink"
```

---

### Task 7: BLE layer with reconnect

**Files:**
- Create: `src/wahoo_lsl/ble.py`, `tests/fakes.py`
- Test: `tests/test_ble.py`

**Interfaces:**
- Consumes: `config.Config/address_kind/select_identifier`, `cps.*UUID/parse_features/sensor_location_name/Features/ParseError`, `pipeline.RawSample/Event/make_event`, `clock.now_lsl/now_wall_ns`, `warn.WarnOnce`.
- Produces (in `wahoo_lsl.ble`):
  - Exceptions: `BleSetupError` (exit 3) with subclasses `DeviceNotFound`, `AmbiguousDevice`, `NoCpsService`; `ReconnectExhausted` (exit 4); tuple `COMMS_ERRORS = (BleakError, asyncio.TimeoutError, OSError)`
  - `@dataclass(frozen=True) ScanResult(name: str | None, address: str, rssi: int | None, cps_advertised: bool, device: Any = None)`
  - `@dataclass(frozen=True) DeviceInfo(name: str | None, address: str, address_kind: str, features: Features, sensor_location: str | None)`
  - `ReconnectPolicy(backoff_s: Sequence[float], max_attempts: int)` with `.delay(attempt: int) -> float`, `.exhausted(failures: int) -> bool`
  - `BleBackend` Protocol: `async discover(timeout) -> list[ScanResult]`, `async find_by_address(address, timeout) -> Any | None`, `make_client(device, disconnected_callback) -> client`; `BleakBackend` implements it.
  - `async scan_cps(timeout: float, backend: BleBackend | None = None) -> list[ScanResult]`
  - `CpsClient(cfg: Config, emit: Callable[[RawSample | Event], None], warn: WarnOnce, *, backend: BleBackend | None = None, platform: str | None = None)` with `async find_device()`, `async connect_and_read() -> DeviceInfo`, `async read_info() -> DeviceInfo` (connects, reads, disconnects), `async run(stop_requested: asyncio.Event) -> None`
- Client objects (bleak `BleakClient` or test fake) must provide: `async connect()`, `async disconnect()`, `async read_gatt_char(uuid) -> bytearray`, `async start_notify(uuid, callback)`, `async stop_notify(uuid)`, `is_connected: bool`, `services.get_service(uuid)`, `services.get_characteristic(uuid)`. `disconnected_callback(client)` is invoked by the client on ANY disconnect (including intentional ones).

- [ ] **Step 1: Write the test fakes**

**`tests/fakes.py`** (create):
```python
"""In-memory stand-ins for bleak, used by BLE and CLI tests."""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

from wahoo_lsl.ble import ScanResult
from wahoo_lsl.cps import CP_FEATURE_UUID, CPS_SERVICE_UUID, SENSOR_LOCATION_UUID

KICKR = SimpleNamespace(name="KICKR CORE 1234", address="AA:BB:CC:DD:EE:01")
WHEEL_AND_CRANK = struct.pack("<I", 0b1100)
ALWAYS = 10**9


class FakeServices:
    def __init__(self, has_cps: bool) -> None:
        self._has_cps = has_cps

    def get_service(self, uuid: str):
        return object() if uuid == CPS_SERVICE_UUID and self._has_cps else None

    def get_characteristic(self, uuid: str):
        return object() if uuid == SENSOR_LOCATION_UUID else None


class FakeClient:
    def __init__(self, backend: "FakeBackend", device, disconnected_callback) -> None:
        self.backend = backend
        self.device = device
        self._disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = FakeServices(backend.has_cps)
        self.notify_callback = None

    async def connect(self) -> None:
        if self.backend.connect_failures > 0:
            self.backend.connect_failures -= 1
            raise OSError("simulated connect failure")
        self.is_connected = True

    async def read_gatt_char(self, uuid: str) -> bytearray:
        if uuid == CP_FEATURE_UUID:
            return bytearray(self.backend.feature_bytes)
        if uuid == SENSOR_LOCATION_UUID:
            return bytearray([13])  # Rear hub
        raise AssertionError(f"unexpected read {uuid}")

    async def start_notify(self, uuid: str, callback) -> None:
        if self.backend.fail_start_notify:
            raise OSError("simulated start_notify failure")
        self.notify_callback = callback
        if self.backend.sessions:
            payloads, drop_after = self.backend.sessions.pop(0)
            asyncio.get_running_loop().call_soon(self._play, payloads, drop_after)

    async def stop_notify(self, uuid: str) -> None:
        self.notify_callback = None

    async def disconnect(self) -> None:
        was_connected = self.is_connected
        self.is_connected = False
        if was_connected:
            self._disconnected_callback(self)  # bleak also calls it on intentional disconnects

    def drop(self) -> None:
        """Simulate link loss."""
        self.is_connected = False
        self._disconnected_callback(self)

    def _play(self, payloads, drop_after: bool) -> None:
        for payload in payloads:
            self.notify_callback(None, bytearray(payload))
        if drop_after:
            self.drop()


class FakeBackend:
    def __init__(
        self,
        scan_results=None,
        *,
        feature_bytes: bytes = WHEEL_AND_CRANK,
        has_cps: bool = True,
        connect_failures: int = 0,
        fail_start_notify: bool = False,
        sessions=None,
    ) -> None:
        if scan_results is None:
            scan_results = [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR)]
        self.scan_results = list(scan_results)
        self.feature_bytes = feature_bytes
        self.has_cps = has_cps
        self.connect_failures = connect_failures
        self.fail_start_notify = fail_start_notify
        # Each session: (payloads to notify right after subscribe, drop link afterwards?)
        self.sessions = list(sessions or [])
        self.clients: list[FakeClient] = []
        self.address_lookups: list[str] = []

    async def discover(self, timeout: float):
        return list(self.scan_results)

    async def find_by_address(self, address: str, timeout: float):
        self.address_lookups.append(address)
        for r in self.scan_results:
            if r.address.lower() == address.lower():
                return r.device
        return None

    def make_client(self, device, disconnected_callback) -> FakeClient:
        client = FakeClient(self, device, disconnected_callback)
        self.clients.append(client)
        return client
```

- [ ] **Step 2: Write the failing tests**

**`tests/test_ble.py`** (create):
```python
import asyncio
import logging
import struct
from types import SimpleNamespace

import pytest
from fakes import ALWAYS, KICKR, FakeBackend

from wahoo_lsl.ble import (
    AmbiguousDevice,
    BleSetupError,
    CpsClient,
    DeviceNotFound,
    NoCpsService,
    ReconnectExhausted,
    ReconnectPolicy,
    ScanResult,
    scan_cps,
)
from wahoo_lsl.config import parse_config
from wahoo_lsl.pipeline import Event, RawSample
from wahoo_lsl.warn import WarnOnce

POWER = struct.pack("<Hh", 0, 200)
UUID = "12345678-1234-5678-1234-567812345678"


def make_cfg(device=None, reconnect=None):
    return parse_config(
        {
            "device": {"name": "KICKR", **(device or {})},
            "measurements": ["power", "wheel_revolutions", "accumulated_torque"],
            "reconnect": {"backoff_s": [0.01], **(reconnect or {})},
        }
    )


def make_client(backend, cfg=None, platform="linux"):
    items: list = []
    warn = WarnOnce(logging.getLogger("wahoo_lsl.test"))
    client = CpsClient(cfg or make_cfg(), items.append, warn, backend=backend, platform=platform)
    return client, items


def kinds(items):
    return [i.kind for i in items if isinstance(i, Event)]


def raws(items):
    return [i for i in items if isinstance(i, RawSample)]


def warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def test_reconnect_policy():
    p = ReconnectPolicy([1, 2, 5], 0)
    assert [p.delay(i) for i in range(5)] == [1, 2, 5, 5, 5]
    assert p.exhausted(1000) is False
    q = ReconnectPolicy([1], 3)
    assert q.exhausted(2) is False
    assert q.exhausted(3) is True


def test_find_by_name_single_match():
    client, _ = make_client(FakeBackend())
    assert asyncio.run(client.find_device()) is KICKR


def test_find_by_name_prefers_cps_advertisers():
    snap = SimpleNamespace(name="KICKR SNAP", address="AA:BB:CC:DD:EE:02")
    backend = FakeBackend(
        [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR), ScanResult(snap.name, snap.address, -40, False, snap)]
    )
    client, _ = make_client(backend)
    assert asyncio.run(client.find_device()) is KICKR


@pytest.mark.parametrize("platform, field", [("linux", "device.mac"), ("darwin", "device.uuid")])
def test_ambiguous_name_lists_candidates(platform, field):
    other = SimpleNamespace(name="KICKR BIKE", address="AA:BB:CC:DD:EE:03")
    backend = FakeBackend(
        [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR), ScanResult(other.name, other.address, -60, True, other)]
    )
    client, _ = make_client(backend, platform=platform)
    with pytest.raises(AmbiguousDevice) as exc:
        asyncio.run(client.find_device())
    msg = str(exc.value)
    assert KICKR.address in msg and other.address in msg and field in msg


def test_name_not_found():
    client, _ = make_client(FakeBackend([]))
    with pytest.raises(DeviceNotFound):
        asyncio.run(client.find_device())


def test_mac_used_on_linux():
    backend = FakeBackend()
    client, _ = make_client(backend, make_cfg({"mac": KICKR.address, "uuid": UUID}), platform="linux")
    assert asyncio.run(client.find_device()) is KICKR
    assert backend.address_lookups == [KICKR.address]


def test_uuid_used_on_darwin():
    mac_dev = SimpleNamespace(name="KICKR CORE 1234", address=UUID)
    backend = FakeBackend([ScanResult(mac_dev.name, UUID, -50, True, mac_dev)])
    client, _ = make_client(backend, make_cfg({"mac": KICKR.address, "uuid": UUID}), platform="darwin")
    assert asyncio.run(client.find_device()) is mac_dev
    assert backend.address_lookups == [UUID]


def test_address_not_found():
    client, _ = make_client(FakeBackend(), make_cfg({"mac": "11:22:33:44:55:66"}))
    with pytest.raises(DeviceNotFound):
        asyncio.run(client.find_device())


def test_read_info_reads_features_and_disconnects():
    backend = FakeBackend()
    client, items = make_client(backend)
    info = asyncio.run(client.read_info())
    assert info.name == KICKR.name
    assert info.address == KICKR.address
    assert info.address_kind == "mac"
    assert info.features.supported == {"power", "wheel_revolutions", "crank_revolutions"}
    assert info.sensor_location == "Rear hub"
    assert backend.clients[0].is_connected is False
    assert items == []  # intentional disconnect emits nothing


def test_read_info_no_cps_service():
    backend = FakeBackend(has_cps=False)
    client, _ = make_client(backend)
    with pytest.raises(NoCpsService):
        asyncio.run(client.read_info())
    assert backend.clients[0].is_connected is False


def test_read_info_comms_error_is_setup_error():
    client, _ = make_client(FakeBackend(connect_failures=1))
    with pytest.raises(BleSetupError):
        asyncio.run(client.read_info())


def test_read_info_bad_feature_payload_is_setup_error():
    client, _ = make_client(FakeBackend(feature_bytes=b"\x01"))
    with pytest.raises(BleSetupError):
        asyncio.run(client.read_info())


def test_run_streams_and_stops_cleanly(caplog):
    backend = FakeBackend(sessions=[([POWER, POWER], False)])
    client, items = make_client(backend)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: len(raws(items)) == 2)
        stop.set()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    samples = raws(items)
    assert [s.seq for s in samples] == [0, 1]
    assert samples[0].data == POWER
    assert samples[0].t_lsl <= samples[1].t_lsl
    assert kinds(items) == ["connected", "features"]
    features = next(i for i in items if isinstance(i, Event) and i.kind == "features")
    assert features.detail["unavailable"] == ["accumulated_torque"]
    assert backend.clients[0].is_connected is False
    ws = warnings(caplog)
    assert sum("accumulated_torque" in w for w in ws) == 1
    assert not any("lost" in w for w in ws)


def test_disconnect_triggers_reconnect_and_seq_continues(caplog):
    backend = FakeBackend(sessions=[([POWER], True), ([POWER], False)])
    client, items = make_client(backend)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: len(raws(items)) == 2 and "reconnected" in kinds(items))
        stop.set()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert [s.seq for s in raws(items)] == [0, 1]
    assert kinds(items) == [
        "connected",
        "features",
        "disconnected",
        "reconnect_attempt",
        "connected",
        "features",
        "reconnected",
    ]
    assert len(backend.clients) == 2
    ws = warnings(caplog)
    assert sum("accumulated_torque" in w for w in ws) == 1  # not repeated on reconnect
    assert sum("lost" in w for w in ws) == 1


def test_reconnect_gives_up_after_max_attempts():
    backend = FakeBackend()
    client, items = make_client(backend, make_cfg(reconnect={"max_attempts": 2}))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: backend.clients and backend.clients[0].notify_callback is not None)
        backend.connect_failures = ALWAYS
        backend.clients[0].drop()
        await task

    with pytest.raises(ReconnectExhausted):
        asyncio.run(scenario())
    assert kinds(items).count("reconnect_attempt") == 2


def test_reconnect_disabled_raises():
    backend = FakeBackend()
    client, items = make_client(backend, make_cfg(reconnect={"enabled": False}))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: backend.clients and backend.clients[0].notify_callback is not None)
        backend.clients[0].drop()
        await task

    with pytest.raises(ReconnectExhausted):
        asyncio.run(scenario())
    assert "reconnect_attempt" not in kinds(items)


def test_stop_during_backoff_returns_promptly():
    backend = FakeBackend()
    client, items = make_client(backend, make_cfg(reconnect={"backoff_s": [30]}))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: backend.clients and backend.clients[0].notify_callback is not None)
        backend.clients[0].drop()
        await until(lambda: "disconnected" in kinds(items))
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert "reconnect_attempt" not in kinds(items)
    assert len(backend.clients) == 1


def test_setup_failure_when_subscribe_fails():
    backend = FakeBackend(fail_start_notify=True)
    client, _ = make_client(backend)

    async def scenario():
        await client.run(asyncio.Event())

    with pytest.raises(BleSetupError):
        asyncio.run(scenario())
    assert backend.clients[0].is_connected is False


def test_scan_cps_filters_to_cps_or_kickr():
    phone = SimpleNamespace(name="Phone", address="AA:BB:CC:DD:EE:09")
    foo = SimpleNamespace(name="Foo", address="AA:BB:CC:DD:EE:08")
    bike = SimpleNamespace(name="KICKR BIKE", address="AA:BB:CC:DD:EE:07")
    backend = FakeBackend(
        [
            ScanResult(foo.name, foo.address, -50, True, foo),
            ScanResult(bike.name, bike.address, -60, False, bike),
            ScanResult(phone.name, phone.address, -70, False, phone),
        ]
    )
    names = [r.name for r in asyncio.run(scan_cps(1.0, backend))]
    assert names == ["Foo", "KICKR BIKE"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ble.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.ble'` (raised while importing `fakes`).

- [ ] **Step 4: Implement**

**`src/wahoo_lsl/ble.py`** (create):
```python
"""BLE layer: the only module that imports bleak (spec §8)."""
from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import clock
from .config import Config, address_kind, select_identifier
from .cps import (
    CP_FEATURE_UUID,
    CP_MEASUREMENT_UUID,
    CPS_SERVICE_UUID,
    SENSOR_LOCATION_UUID,
    Features,
    ParseError,
    parse_features,
    sensor_location_name,
)
from .pipeline import Event, RawSample, make_event
from .warn import WarnOnce

log = logging.getLogger(__name__)

# Errors bleak or the OS may raise during BLE operations on any backend.
COMMS_ERRORS = (BleakError, asyncio.TimeoutError, OSError)


class BleSetupError(Exception):
    """Could not find, connect to, or read the device. CLI exit code 3."""


class DeviceNotFound(BleSetupError):
    pass


class AmbiguousDevice(BleSetupError):
    pass


class NoCpsService(BleSetupError):
    pass


class ReconnectExhausted(Exception):
    """Connection lost and could not be (or may not be) restored. CLI exit code 4."""


@dataclass(frozen=True)
class ScanResult:
    name: str | None
    address: str
    rssi: int | None
    cps_advertised: bool
    device: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class DeviceInfo:
    name: str | None
    address: str
    address_kind: str  # "mac" or "uuid"
    features: Features
    sensor_location: str | None


class ReconnectPolicy:
    def __init__(self, backoff_s: Sequence[float], max_attempts: int) -> None:
        if not backoff_s:
            raise ValueError("backoff_s must not be empty")
        self._backoff = tuple(backoff_s)
        self._max_attempts = max_attempts

    def delay(self, attempt: int) -> float:
        """Seconds to wait before 0-based reconnect attempt ``attempt``."""
        return self._backoff[min(attempt, len(self._backoff) - 1)]

    def exhausted(self, failures: int) -> bool:
        return self._max_attempts > 0 and failures >= self._max_attempts


class BleBackend(Protocol):
    async def discover(self, timeout: float) -> list[ScanResult]: ...

    async def find_by_address(self, address: str, timeout: float) -> Any | None: ...

    def make_client(self, device: Any, disconnected_callback: Callable[[Any], None]) -> Any: ...


class BleakBackend:
    """Production backend (bleak on macOS, Windows and Linux)."""

    async def discover(self, timeout: float) -> list[ScanResult]:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results = []
        for device, adv in found.values():
            uuids = {u.lower() for u in (adv.service_uuids or ())}
            results.append(
                ScanResult(
                    name=adv.local_name or device.name,
                    address=device.address,
                    rssi=adv.rssi,
                    cps_advertised=CPS_SERVICE_UUID in uuids,
                    device=device,
                )
            )
        return results

    async def find_by_address(self, address: str, timeout: float) -> Any | None:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    def make_client(self, device: Any, disconnected_callback: Callable[[Any], None]) -> Any:
        return BleakClient(device, disconnected_callback=disconnected_callback)


async def scan_cps(timeout: float, backend: BleBackend | None = None) -> list[ScanResult]:
    """Devices advertising the CPS service, or whose name contains 'KICKR'."""
    results = await (backend or BleakBackend()).discover(timeout)
    return [r for r in results if r.cps_advertised or (r.name and "kickr" in r.name.lower())]


class CpsClient:
    """Finds, connects to and streams from one CPS device, reconnecting per config."""

    def __init__(
        self,
        cfg: Config,
        emit: Callable[[RawSample | Event], None],
        warn: WarnOnce,
        *,
        backend: BleBackend | None = None,
        platform: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._emit = emit
        self._warn = warn
        self._backend = backend or BleakBackend()
        self._platform = platform or sys.platform
        self._client: Any = None  # current client; None means "ignore disconnect callbacks"
        self._info: DeviceInfo | None = None
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._disconnected: asyncio.Event | None = None

    # ---- discovery & setup -------------------------------------------------

    async def find_device(self) -> Any:
        kind, value = select_identifier(self._cfg.device, self._platform)
        timeout = self._cfg.device.scan_timeout_s
        if kind != "name":
            device = await self._backend.find_by_address(value, timeout)
            if device is None:
                raise DeviceNotFound(f"no device with {kind} {value} found within {timeout:g}s")
            return device
        needle = value.lower()
        matches = [r for r in await self._backend.discover(timeout) if r.name and needle in r.name.lower()]
        if not matches:
            raise DeviceNotFound(f"no device with name containing '{value}' found within {timeout:g}s")
        candidates = [r for r in matches if r.cps_advertised] or matches
        if len(candidates) > 1:
            lines = "\n".join(f"  {r.name}  {r.address}  rssi={r.rssi}" for r in candidates)
            raise AmbiguousDevice(
                f"{len(candidates)} devices match name '{value}'; set device.{address_kind(self._platform)} "
                f"in the config to choose one:\n{lines}"
            )
        return candidates[0].device

    async def connect_and_read(self) -> DeviceInfo:
        """Connect and read CP Feature / Sensor Location. Leaves the client connected."""
        device = await self.find_device()
        client = self._backend.make_client(device, self._on_disconnect)
        self._client = client
        try:
            await client.connect()
            if client.services.get_service(CPS_SERVICE_UUID) is None:
                raise NoCpsService(f"{getattr(device, 'name', device)} does not expose the Cycling Power Service")
            features = parse_features(await client.read_gatt_char(CP_FEATURE_UUID))
            location = None
            if client.services.get_characteristic(SENSOR_LOCATION_UUID) is not None:
                loc_raw = await client.read_gatt_char(SENSOR_LOCATION_UUID)
                location = sensor_location_name(loc_raw[0]) if loc_raw else None
        except BaseException:
            await self._safe_disconnect()
            raise
        info = DeviceInfo(
            name=getattr(device, "name", None),
            address=getattr(device, "address", str(device)),
            address_kind=address_kind(self._platform),
            features=features,
            sensor_location=location,
        )
        self._info = info
        return info

    async def read_info(self) -> DeviceInfo:
        """Connect, read device info, disconnect. Used by `list-features`."""
        try:
            return await self.connect_and_read()
        except (ParseError, *COMMS_ERRORS) as e:
            raise BleSetupError(f"BLE setup failed: {e}") from e
        finally:
            await self._safe_disconnect()

    async def _open_session(self) -> DeviceInfo:
        """Connect, report features, subscribe. Any failure -> BleSetupError, nothing left connected."""
        try:
            info = await self.connect_and_read()
            self._emit(make_event("connected", name=info.name, address=info.address, address_kind=info.address_kind))
            self._report_features(info)  # before subscribing, so it precedes all samples
            await self._client.start_notify(CP_MEASUREMENT_UUID, self._on_notify)
        except BleSetupError:
            raise
        except (ParseError, *COMMS_ERRORS) as e:
            await self._safe_disconnect()
            raise BleSetupError(f"BLE setup failed: {e}") from e
        return info

    def _report_features(self, info: DeviceInfo) -> None:
        unavailable = [m for m in self._cfg.measurements if m not in info.features.supported]
        for name in unavailable:
            self._warn.warn(
                ("unavailable", name),
                "Measurement '%s' is not supported by %s (CP Feature 0x%08x); continuing without it",
                name,
                info.name,
                info.features.raw,
            )
        self._emit(
            make_event(
                "features",
                raw=info.features.raw,
                supported=sorted(info.features.supported),
                unavailable=unavailable,
                sensor_location=info.sensor_location,
            )
        )

    # ---- streaming ---------------------------------------------------------

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Stream until ``stop_requested`` is set.

        Raises BleSetupError if the first connection fails and ReconnectExhausted if the
        link is lost and cannot be restored. Always disconnects before returning.
        """
        self._loop = asyncio.get_running_loop()
        self._disconnected = asyncio.Event()
        policy = ReconnectPolicy(self._cfg.reconnect.backoff_s, self._cfg.reconnect.max_attempts)
        try:
            info = await self._open_session()
            log.info("Streaming from %s (%s %s)", info.name, info.address_kind, info.address)
            while True:
                await _wait_first(stop_requested, self._disconnected)
                if stop_requested.is_set():
                    return
                if not self._cfg.reconnect.enabled:
                    raise ReconnectExhausted("connection lost and reconnect is disabled")
                await self._reconnect(policy, stop_requested)
        finally:
            await self._shutdown()

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        t_lsl = clock.now_lsl()  # FIRST statement: minimal timing error
        t_wall_ns = clock.now_wall_ns()  # SECOND statement
        seq = self._seq
        self._seq += 1
        self._emit(RawSample(seq, t_lsl, t_wall_ns, bytes(data)))

    def _on_disconnect(self, client: Any) -> None:
        if client is not self._client:
            return  # intentional disconnect (we cleared _client first) or a stale client
        info = self._info
        log.warning("Connection to %s lost", info.name if info else "device")
        self._emit(
            make_event(
                "disconnected",
                name=info.name if info else None,
                address=info.address if info else None,
            )
        )
        if self._loop is not None and self._disconnected is not None:
            self._loop.call_soon_threadsafe(self._disconnected.set)

    async def _reconnect(self, policy: ReconnectPolicy, stop_requested: asyncio.Event) -> None:
        await self._safe_disconnect()  # release the dead client
        failures = 0
        while True:
            delay = policy.delay(failures)
            if await _sleep_unless(stop_requested, delay):
                return
            attempt = failures + 1
            log.warning("Reconnect attempt %d (after %.1fs)", attempt, delay)
            self._emit(make_event("reconnect_attempt", attempt=attempt, delay_s=delay))
            assert self._disconnected is not None
            self._disconnected.clear()
            try:
                info = await self._open_session()
            except BleSetupError as e:
                failures += 1
                log.warning("Reconnect attempt %d failed: %s", attempt, e)
                if policy.exhausted(failures):
                    raise ReconnectExhausted(f"gave up after {failures} failed reconnect attempt(s)") from e
                continue
            log.info("Reconnected to %s", info.name)
            self._emit(make_event("reconnected", attempt=attempt, name=info.name, address=info.address))
            return

    async def _shutdown(self) -> None:
        client = self._client
        if client is not None and getattr(client, "is_connected", False):
            try:
                await client.stop_notify(CP_MEASUREMENT_UUID)
            except Exception as e:
                log.debug("stop_notify failed during shutdown: %s", e)
        await self._safe_disconnect()

    async def _safe_disconnect(self) -> None:
        client, self._client = self._client, None  # clear first so the callback ignores this disconnect
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("disconnect failed: %s", e)


async def _wait_first(*events: asyncio.Event) -> None:
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _sleep_unless(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``. Return True if ``stop`` is (or becomes) set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ble.py -v`
Expected: all passed. Then run the whole suite: `conda run -n wahoo python -m pytest` — all passed.

- [ ] **Step 6: Commit**

```bash
git add src/wahoo_lsl/ble.py tests/fakes.py tests/test_ble.py
git commit -m "feat: BLE layer with device selection, feature read and reconnect"
```

---

### Task 8: CLI, entry point, README

**Files:**
- Create: `src/wahoo_lsl/cli.py`, `src/wahoo_lsl/__main__.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above. `tests/fakes.py` from Task 7.
- Produces (in `wahoo_lsl.cli`):
  - `build_parser() -> argparse.ArgumentParser`, `setup_logging(debug: bool, diag_log: Path | None = None)`
  - `format_scan(results: Sequence[ScanResult]) -> str`, `format_features(info: DeviceInfo, measurements: Sequence[str]) -> str`
  - `run_session(cfg: Config, *, debug=False, backend=None, start: datetime | None = None) -> int`
  - `main(argv: Sequence[str] | None = None, *, backend: BleBackend | None = None) -> int`
  - Exit code constants `EXIT_OK=0, EXIT_ERROR=1, EXIT_CONFIG=2, EXIT_SETUP=3, EXIT_RECONNECT=4, EXIT_INTERRUPTED=130`

- [ ] **Step 1: Write the failing tests**

**`tests/test_cli.py`** (create):
```python
import json
import re
import struct
from datetime import datetime
from pathlib import Path

import pytest
from fakes import KICKR, FakeBackend

from wahoo_lsl.ble import DeviceInfo, ScanResult
from wahoo_lsl.cli import build_parser, format_features, format_scan, main, run_session
from wahoo_lsl.config import parse_config
from wahoo_lsl.cps import parse_features

POWER = struct.pack("<Hh", 0, 200)
WHEEL = struct.pack("<Hh", 1 << 4, 150) + struct.pack("<IH", 10, 2048)
TRUNCATED_WHEEL = struct.pack("<Hh", 1 << 4, 5)
WALL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def write_config(tmp_path: Path, **overrides) -> Path:
    cfg = {
        "device": {"name": "KICKR", "scan_timeout_s": 1},
        "measurements": ["power", "accumulated_torque"],
        "log": {"path": str(tmp_path / "logs" / "cps_{start:%Y%m%d}.jsonl")},
        "reconnect": {"enabled": False},
    }
    cfg.update(overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_run_options():
    args = build_parser().parse_args(["run", "-c", "x.json", "--debug", "--diag-log", "d.log"])
    assert args.command == "run"
    assert args.config == Path("x.json")
    assert args.debug is True
    assert args.diag_log == Path("d.log")


def test_format_features():
    info = DeviceInfo("KICKR CORE 1234", "AA:BB:CC:DD:EE:01", "mac", parse_features(struct.pack("<I", 0b1100)), "Rear hub")
    text = format_features(info, ["power", "accumulated_torque"])
    lines = text.splitlines()
    assert "(mac: AA:BB:CC:DD:EE:01)" in lines[0]
    assert "Rear hub" in lines[1]
    assert "Feature bits (0x0000000C):" in text
    assert "  [x] wheel_revolution_data" in lines
    assert "  [ ] pedal_power_balance" in lines
    assert any(line.split() == ["power", "available"] for line in lines)
    assert any(line.split() == ["accumulated_torque", "UNAVAILABLE"] for line in lines)


def test_format_features_without_measurements():
    info = DeviceInfo(None, "X", "uuid", parse_features(b"\x00\x00\x00\x00"), None)
    text = format_features(info, [])
    assert "Configured measurements" not in text
    assert "(uuid: X)" in text


def test_format_scan():
    assert format_scan([]) == "No Cycling Power devices found."
    text = format_scan([ScanResult("KICKR CORE", "AA:BB:CC:DD:EE:01", -50, True)])
    assert "KICKR CORE" in text and "AA:BB:CC:DD:EE:01" in text and "yes" in text


def test_main_invalid_json_exit_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    assert main(["run", "-c", str(p)]) == 2


def test_main_missing_config_exit_2(tmp_path):
    assert main(["list-features", "-c", str(tmp_path / "missing.json")]) == 2


def test_list_features_with_fake_backend(tmp_path, capsys):
    p = write_config(tmp_path)
    assert main(["list-features", "-c", str(p)], backend=FakeBackend()) == 0
    out = capsys.readouterr().out
    assert KICKR.name in out
    assert "Rear hub" in out
    assert "UNAVAILABLE" in out


def test_list_features_device_not_found_exit_3(tmp_path):
    p = write_config(tmp_path)
    assert main(["list-features", "-c", str(p)], backend=FakeBackend([])) == 3


def test_scan_with_fake_backend(capsys):
    assert main(["scan", "--timeout", "0.1"], backend=FakeBackend()) == 0
    assert KICKR.name in capsys.readouterr().out


def test_run_session_end_to_end(tmp_path):
    cfg = parse_config(json.loads(write_config(tmp_path).read_text(encoding="utf-8")))
    backend = FakeBackend(sessions=[([POWER, WHEEL, TRUNCATED_WHEEL], True)])
    code = run_session(cfg, backend=backend, start=datetime(2026, 10, 20))
    assert code == 4  # link dropped and reconnect is disabled
    records = read_log(tmp_path / "logs" / "cps_20261020.jsonl")
    events = [r["kind"] for r in records if r["type"] == "event"]
    assert events == ["clock_sync", "connected", "features", "disconnected", "stopped"]
    samples = [r for r in records if r["type"] == "sample"]
    assert [s["seq"] for s in samples] == [0, 1, 2]
    assert samples[0]["fields"]["power_w"] == 200
    assert samples[1]["fields"]["wheel_revs"] == 10  # logged although not configured
    assert "wheel_revolutions" in samples[2]["parse_error"]
    assert samples[2]["raw"] == TRUNCATED_WHEEL.hex()
    assert samples[0]["t_lsl"] <= samples[1]["t_lsl"] <= samples[2]["t_lsl"]
    assert all(WALL_RE.match(r["t_wall"]) for r in records)
    stopped = records[-1]
    assert stopped["kind"] == "stopped"
    assert stopped["detail"] == {"reason": "reconnect_exhausted", "parse_errors": 1}


def test_run_session_setup_failure_exit_3(tmp_path):
    cfg = parse_config(json.loads(write_config(tmp_path).read_text(encoding="utf-8")))
    code = run_session(cfg, backend=FakeBackend([]), start=datetime(2026, 10, 20))
    assert code == 3
    records = read_log(tmp_path / "logs" / "cps_20261020.jsonl")
    assert [r["kind"] for r in records] == ["clock_sync", "stopped"]
    assert records[-1]["detail"]["reason"] == "setup_error"


def test_run_session_unwritable_log_path(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    cfg = parse_config(
        {
            "device": {"name": "KICKR"},
            "measurements": ["power"],
            "log": {"path": str(blocker / "cps.jsonl")},
        }
    )
    backend = FakeBackend()
    assert run_session(cfg, backend=backend) == 2
    assert backend.clients == []  # BLE never touched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.cli'`.

- [ ] **Step 3: Implement**

**`src/wahoo_lsl/cli.py`** (create):
```python
"""Command-line entry point (spec §11)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .ble import (
    COMMS_ERRORS,
    BleBackend,
    BleSetupError,
    CpsClient,
    DeviceInfo,
    ReconnectExhausted,
    ScanResult,
    scan_cps,
)
from .config import Config, ConfigError, load_config
from .cps import DISTRIBUTED_SYSTEM_SUPPORT, FEATURE_BITS
from .pipeline import Pipeline, Sink, make_event
from .sinks import ConsoleSink, LogSink, format_log_path
from .warn import WarnOnce

log = logging.getLogger("wahoo_lsl")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SETUP = 3
EXIT_RECONNECT = 4
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wahoo-lsl", description="Stream and log Cycling Power Service data from a Wahoo KICKR."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="list nearby devices advertising the Cycling Power Service")
    scan.add_argument("--timeout", type=float, default=10.0, help="scan duration in seconds (default 10)")
    features = sub.add_parser("list-features", help="connect and print the device's cycling power features")
    run = sub.add_parser("run", help="stream and log CP Measurement notifications")
    for p in (features, run):
        p.add_argument("-c", "--config", required=True, type=Path, help="path to JSON config")
    for p in (scan, features, run):
        p.add_argument("--debug", action="store_true", help="verbose logging (incl. bleak); print each sample")
    run.add_argument("--diag-log", type=Path, help="also write diagnostic log lines to this file")
    return parser


def setup_logging(debug: bool, diag_log: Path | None = None) -> None:
    """Diagnostics go to stderr (and optionally a file); data goes to the JSONL log only."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if diag_log is not None:
        diag_log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(diag_log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("wahoo_lsl").setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger("bleak").setLevel(logging.DEBUG if debug else logging.WARNING)


def format_scan(results: Sequence[ScanResult]) -> str:
    if not results:
        return "No Cycling Power devices found."
    lines = [f"{'name':<24} {'address':<38} {'rssi':>5}  cps"]
    for r in results:
        rssi = r.rssi if r.rssi is not None else "?"
        lines.append(f"{(r.name or '?'):<24} {r.address:<38} {rssi:>5}  {'yes' if r.cps_advertised else 'no'}")
    return "\n".join(lines)


def format_features(info: DeviceInfo, measurements: Sequence[str]) -> str:
    f = info.features
    lines = [
        f"Device:  {info.name or '?'}   ({info.address_kind}: {info.address})",
        f"Sensor location: {info.sensor_location or 'not reported'}",
        f"Feature bits (0x{f.raw:08X}):",
    ]
    for name in FEATURE_BITS.values():
        lines.append(f"  [{'x' if f.flags[name] else ' '}] {name}")
    lines.append(f"  {DISTRIBUTED_SYSTEM_SUPPORT} = {f.flags[DISTRIBUTED_SYSTEM_SUPPORT]}")
    lines.append("Measurements supported: " + ", ".join(sorted(f.supported)))
    if measurements:
        width = max(len(m) for m in measurements)
        lines.append("Configured measurements:")
        for m in measurements:
            lines.append(f"  {m:<{width}}  {'available' if m in f.supported else 'UNAVAILABLE'}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, backend: BleBackend | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.debug, getattr(args, "diag_log", None))
    try:
        if args.command == "scan":
            print(format_scan(asyncio.run(scan_cps(args.timeout, backend))))
            return EXIT_OK
        if args.command == "list-features":
            cfg = load_config(args.config, require_measurements=False)
            client = CpsClient(cfg, lambda _item: None, WarnOnce(log), backend=backend)
            info = asyncio.run(client.read_info())
            print(format_features(info, cfg.measurements))
            return EXIT_OK
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return EXIT_CONFIG
    except BleSetupError as e:
        log.error("%s", e)
        return EXIT_SETUP
    except COMMS_ERRORS as e:
        log.error("Bluetooth error: %s", e)
        return EXIT_SETUP
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return run_session(cfg, debug=args.debug, backend=backend)


def run_session(
    cfg: Config,
    *,
    debug: bool = False,
    backend: BleBackend | None = None,
    start: datetime | None = None,
) -> int:
    log_path = format_log_path(cfg.log.path, start or datetime.now())
    try:  # pre-flight in the main thread so a bad path can never mean a run without a log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8"):
            pass
    except OSError as e:
        log.error("Cannot write log file %s: %s", log_path, e)
        return EXIT_CONFIG

    warn = WarnOnce(log)
    sinks: list[Sink] = [LogSink(log_path, cfg.log.flush_every_n)]
    if debug:
        sinks.append(ConsoleSink())
    pipeline = Pipeline(sinks, cfg.measurements, warn, queue_warn_depth=cfg.queue_warn_depth)
    pipeline.start()
    pipeline.put(make_event("clock_sync"))
    log.info("Logging samples to %s", log_path)

    client = CpsClient(cfg, pipeline.put, warn, backend=backend)
    code, reason = EXIT_OK, "user"
    try:
        asyncio.run(_stream(client))
    except KeyboardInterrupt:
        pass  # Windows Ctrl-C; client.run() has already disconnected in its finally block
    except BleSetupError as e:
        log.error("%s", e)
        code, reason = EXIT_SETUP, "setup_error"
    except ReconnectExhausted as e:
        log.error("%s", e)
        code, reason = EXIT_RECONNECT, "reconnect_exhausted"
    except Exception:
        log.exception("Unexpected error")
        code, reason = EXIT_ERROR, "error"
    finally:
        pipeline.put(make_event("stopped", reason=reason))
        pipeline.close(timeout=5.0)
    log.info("Stopped (%s)", reason)
    return code


async def _stream(client: CpsClient) -> None:
    stop = asyncio.Event()
    if sys.platform != "win32":  # add_signal_handler is not implemented on Windows event loops
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    await client.run(stop)
```

**`src/wahoo_lsl/__main__.py`** (create):
```python
from .cli import main

raise SystemExit(main())
```

**`README.md`** (create):
````markdown
# wahoo-lsl

Stream and log Bluetooth LE Cycling Power Service (CPS) data from a Wahoo KICKR (tested target: KICKR V6).
Runs on macOS, Windows and Linux. Every CP Measurement notification is timestamped on arrival,
parsed, and written as one line of JSONL. Lab Streaming Layer output is planned.

Design: `docs/superpowers/specs/2026-10-20-wahoo-cps-logger-design.md`.

## Install

Uses the `wahoo` conda environment.

```bash
# first time on a new machine:
conda env create -f environment.yml
# then (and whenever setting up a checkout):
conda run -n wahoo python -m pip install -e .
```

Without conda: `pip install -r requirements-dev.txt && pip install -e .` in any Python >= 3.10 environment.

Linux: BlueZ must be running and your user must be allowed to use it (e.g. member of the `bluetooth` group).
macOS: the terminal app needs Bluetooth permission (System Settings → Privacy & Security → Bluetooth).

## Usage

```bash
conda run -n wahoo wahoo-lsl scan                                   # find nearby CPS devices
conda run -n wahoo wahoo-lsl list-features -c config.json           # print supported features and measurements
conda run -n wahoo wahoo-lsl run -c config.json                     # stream + log until Ctrl-C
conda run -n wahoo wahoo-lsl run -c config.json --debug             # also print each sample, verbose logging
```

Copy `config.example.json` to `config.json` and edit. The same config works on every platform:
`device.uuid` is used on macOS (CoreBluetooth hides MAC addresses), `device.mac` on Windows/Linux;
if the one for the current platform is null, the device is matched by `device.name` (case-insensitive substring).
`list-features` prints the identifier to paste in.

`measurements` lists what you expect the device to provide. Unsupported or missing measurements are
warned about once; all parsed fields are always logged.

## Clocks

| Field | Clock | Use |
|---|---|---|
| `t_lsl` | `pylsl.local_clock()` (monotonic) | the timing reference; future LSL timestamps |
| `t_wall` | system realtime clock, UTC | human reference only |
| `wheel_event_t`, `crank_event_t` | sensor clock (1/2048 s, 1/1024 s ticks, wrap at 65536) | raw device timing |

A `clock_sync` event at startup records `t_lsl` and `t_wall` read back-to-back.
`t_lsl` is host arrival time and includes BLE connection-interval latency.

## Log format

```json
{"type":"sample","seq":42,"t_lsl":12345.678901,"t_wall":"2026-10-20T18:01:02.123456+00:00","raw":"1000a500...","flags":16,"fields":{"power_w":165,"wheel_revs":1203,"wheel_event_t":40321,"offset_compensation":false}}
{"type":"event","kind":"disconnected","t_lsl":12350.1,"t_wall":"...","detail":{"name":"KICKR CORE","address":"..."}}
```

Event kinds: `clock_sync`, `connected`, `features`, `disconnected`, `reconnect_attempt`, `reconnected`, `stopped`.
Samples that fail to parse keep `raw` and add `parse_error`.

## Exit codes

0 ok · 1 unexpected error · 2 config error / unwritable log · 3 device or connection setup error · 4 connection lost and not restored · 130 interrupted (`scan`/`list-features`)

## Tests

```bash
conda run -n wahoo python -m pytest
```

## Manual hardware checklist

Run on each platform (macOS, Windows, Linux) with the same config, filling in only `uuid` (macOS) or `mac` (Windows/Linux):

1. `scan` lists the KICKR with `cps yes`.
2. `list-features` shows the device, sensor location, feature bits, and marks configured measurements available/UNAVAILABLE.
3. `run` for 2 minutes while pedalling: samples at the device rate, `seq` contiguous, `t_lsl` increasing.
4. Power-cycle the KICKR mid-run: a "Connection ... lost" warning, `disconnected` → `reconnect_attempt` → `connected` → `reconnected` events in the log, `seq` continues.
5. Ctrl-C: clean exit (code 0), last line of the log is a `stopped` event.
6. `run --debug`: each sample printed to stdout.
````

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest -v`
Expected: the full suite passes (all tasks' tests).

- [ ] **Step 5: Smoke-check the CLI without hardware**

Run: `conda run -n wahoo wahoo-lsl --help && conda run -n wahoo wahoo-lsl run -c missing.json; echo "exit=$?"`
Expected: help text listing `scan`, `list-features`, `run`; then a `Config error: cannot read config missing.json` line and `exit=2`.

- [ ] **Step 6: Commit**

```bash
git add src/wahoo_lsl/cli.py src/wahoo_lsl/__main__.py README.md tests/test_cli.py
git commit -m "feat: CLI (scan, list-features, run), entry point and README"
```
