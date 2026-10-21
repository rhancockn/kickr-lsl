# KICKR FTMS Power Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A localhost service (`wahoo-ftms serve`) that takes FTMS control of a Wahoo KICKR, holds ERG targets requested by one local client through a JSON-lines TCP endpoint, hides BLE latency behind cached state, and spins down to 0 W on client disconnect, error or shutdown; plus a stdlib-only Python client and a CLI client.

**Architecture:** One asyncio event loop per service process: `FtmsLink` (BLE, own connection), `Controller` (state + rate-limited latest-wins sender + reconnect supervisor + spin-down), `ControlServer` (asyncio TCP, one client). Pure protocol code in `ftms/protocol.py`. Client side (`ftms/client.py`) uses only the standard library.

**Tech Stack:** Python ≥ 3.10 (env runs 3.12), bleak ≥ 1.0 (3.0.2 installed), pylsl (clock only), asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-kickr-ftms-control-design.md`

## Global Constraints

- Environment: existing conda env `wahoo` (`/Users/rh687/miniconda3/envs/wahoo`). Run tests with `conda run -n wahoo python -m pytest -q -W error`. No uv, no new runtime dependencies.
- Work on branch `feat/ftms-control`. One commit per task; every commit message ends with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` preceded by a blank line.
- `wahoo_lsl/ftms/__init__.py` and `wahoo_lsl/ftms/client.py` import **only the standard library**.
- `wahoo_lsl/ftms/protocol.py` is pure (no I/O, no bleak, no asyncio).
- Only `wahoo_lsl/ftms/link.py` (among ftms modules) imports bleak; it otherwise reuses `wahoo_lsl.ble`.
- `wahoo_lsl/ftms/cli.py` imports `link`, `controller`, `server`, `wahoo_lsl.cli` and `wahoo_lsl.ble` **lazily** inside the serve path only.
- OS checks: only `sys.platform != "win32"` around `loop.add_signal_handler` in `ftms/cli.py`. No other platform branches.
- Timestamps: `t_lsl` from `wahoo_lsl.clock.now_lsl()` only (the controller takes it as an injectable `now`).
- Every BLE wait is bounded (`ack_timeout_s`, `SPIN_DOWN_TIMEOUT_S = 2.0`, `ble.SHUTDOWN_TIMEOUT_S = 5.0`).
- Defaults: host `127.0.0.1`, port `47610`, `max_rate_hz` 2, `ack_timeout_s` 1.0, reconnect backoff `[1, 2, 5, 10]`.
- All 157 existing tests keep passing unchanged (Task 1 only adds tests).
- Tests: no wall-clock assertions on rate limiting (use the injected fake clock); small real timeouts are allowed only inside `until(...)` polling helpers and ack-timeout tests.

## Review Focus

1. **Client killed mid-session (no clean EOF, TCP reset)** → the service must still spin down to 0 W and accept the next client. Pinned by `test_client_reset_triggers_spin_down` (Task 6) and the e2e test (Task 8).
2. **BLE link drops while a set is in flight** → after reconnect the stale in-flight success must not mark that value confirmed; target is 0 W, `link_epoch` bumped. Pinned by `test_stale_confirmation_ignored_after_reconnect` (Task 5).
3. **Burst of sets faster than `max_rate_hz`** → never more than one send per interval, latest value wins, and a single set after idle is sent with no delay. Pinned by `test_burst_coalesced_and_rate_limited` and `test_single_set_sent_immediately` (Task 5).
4. **Shutdown while a control-point command hangs** → shutdown still sends 0 W and finishes within bounds. Pinned by `test_close_cancels_inflight_and_spins_down` (Task 5) and `test_close_fails_pending_command` (Task 4).
5. **Another app takes control of the trainer (status 0xFF)** → treated as link loss → reconnect, re-take control, 0 W. Pinned by `test_control_permission_lost_reports_link_lost` (Task 4) and `test_link_lost_then_reconnect_resets_to_zero` (Task 5).

---

### Task 1: Shared BLE/config helpers for a second service

**Files:**
- Modify: `src/wahoo_lsl/ble.py` (ScanResult, BleakBackend.discover, new `find_device`, `CpsClient.find_device`)
- Modify: `src/wahoo_lsl/config.py` (rename four helpers to public names)
- Test: `tests/test_ble.py`, `tests/test_config.py` (append only)

**Interfaces:**
- Produces: `ble.ScanResult.service_uuids: frozenset[str]` (default `frozenset()`, lower-case full UUIDs); `async ble.find_device(backend: BleBackend, device_cfg: DeviceConfig, platform: str, advertises: Callable[[ScanResult], bool]) -> Any`; `config.section(obj, key, allowed) -> dict`, `config.parse_device(d) -> DeviceConfig`, `config.parse_reconnect(rc) -> ReconnectConfig`, `config.pos_number(value, where) -> float`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ble.py` (it already imports `asyncio`, `SimpleNamespace`, `pytest`, `KICKR`, `FakeBackend`, `AmbiguousDevice`, `ScanResult`):

```python
from wahoo_lsl.ble import find_device  # noqa: E402
from wahoo_lsl.config import DeviceConfig  # noqa: E402

OTHER_SVC = "0000feed-0000-1000-8000-00805f9b34fb"


def test_scan_result_service_uuids_defaults_empty():
    assert ScanResult("K", "AA", None, False).service_uuids == frozenset()


def test_find_device_prefers_predicate_match():
    plain = SimpleNamespace(name="KICKR A", address="AA:BB:CC:DD:EE:10")
    svc = SimpleNamespace(name="KICKR B", address="AA:BB:CC:DD:EE:11")
    backend = FakeBackend(
        [
            ScanResult(plain.name, plain.address, -40, True, plain),
            ScanResult(svc.name, svc.address, -60, False, svc, frozenset({OTHER_SVC})),
        ]
    )
    found = asyncio.run(find_device(backend, DeviceConfig("KICKR"), "linux", lambda r: OTHER_SVC in r.service_uuids))
    assert found is svc


def test_find_device_ambiguous_when_no_predicate_match():
    a = SimpleNamespace(name="KICKR A", address="AA:BB:CC:DD:EE:10")
    b = SimpleNamespace(name="KICKR B", address="AA:BB:CC:DD:EE:11")
    backend = FakeBackend([ScanResult(a.name, a.address, -40, False, a), ScanResult(b.name, b.address, -60, False, b)])
    with pytest.raises(AmbiguousDevice):
        asyncio.run(find_device(backend, DeviceConfig("KICKR"), "linux", lambda r: False))


def test_find_device_by_mac_uses_address_lookup():
    backend = FakeBackend()
    found = asyncio.run(find_device(backend, DeviceConfig("KICKR", mac=KICKR.address), "linux", lambda r: False))
    assert found is KICKR
    assert backend.address_lookups == [KICKR.address]
```

Append to `tests/test_config.py` (it already imports `pytest`):

```python
def test_shared_section_parsers_are_public():
    from wahoo_lsl.config import ConfigError, parse_device, parse_reconnect, pos_number, section

    assert parse_device({"name": " KICKR "}).name == "KICKR"
    assert parse_reconnect({}).backoff_s == (1.0, 2.0, 5.0, 10.0)
    assert pos_number(2, "x") == 2.0
    with pytest.raises(ConfigError):
        pos_number(0, "x")
    with pytest.raises(ConfigError):
        section({"s": {"bad": 1}}, "s", {"good"})
    assert section({}, "s", {"good"}) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ble.py tests/test_config.py -q -W error`
Expected: FAIL — `ImportError: cannot import name 'find_device'` / `'parse_device'`.

- [ ] **Step 3: Rename the config helpers**

```bash
perl -pi -e 's/\b_(section|parse_device|parse_reconnect|pos_number)\(/$1(/g' src/wahoo_lsl/config.py
grep -n "_section\|_parse_device\|_parse_reconnect\|_pos_number" src/wahoo_lsl/config.py   # expect no output
```

- [ ] **Step 4: Extract `find_device` in `ble.py`**

Change the import line `from .config import Config, address_kind, select_identifier` to:

```python
from .config import Config, DeviceConfig, address_kind, select_identifier
```

Replace the `ScanResult` dataclass with:

```python
@dataclass(frozen=True)
class ScanResult:
    name: str | None
    address: str
    rssi: int | None
    cps_advertised: bool
    device: Any = field(default=None, compare=False, repr=False)
    service_uuids: frozenset[str] = frozenset()  # lower-case full UUIDs from the advertisement
```

In `BleakBackend.discover`, add `service_uuids=frozenset(uuids),` to the `ScanResult(...)` call (after `device=device,`).

Add this module-level function directly after `scan_cps`:

```python
async def find_device(
    backend: BleBackend,
    device_cfg: DeviceConfig,
    platform: str,
    advertises: Callable[[ScanResult], bool],
) -> Any:
    """Find the configured device: by mac/uuid when set for this platform, else by name.

    Name matches for which ``advertises`` is true are preferred; more than one candidate
    raises AmbiguousDevice.
    """
    kind, value = select_identifier(device_cfg, platform)
    timeout = device_cfg.scan_timeout_s
    if kind != "name":
        device = await backend.find_by_address(value, timeout)
        if device is None:
            raise DeviceNotFound(f"no device with {kind} {value} found within {timeout:g}s")
        return device
    needle = value.lower()
    matches = [r for r in await backend.discover(timeout) if r.name and needle in r.name.lower()]
    if not matches:
        raise DeviceNotFound(f"no device with name containing '{value}' found within {timeout:g}s")
    candidates = [r for r in matches if advertises(r)] or matches
    if len(candidates) > 1:
        lines = "\n".join(f"  {r.name}  {r.address}  rssi={r.rssi}" for r in candidates)
        raise AmbiguousDevice(
            f"{len(candidates)} devices match name '{value}'; set device.{address_kind(platform)} "
            f"in the config to choose one:\n{lines}"
        )
    return candidates[0].device
```

Replace the body of `CpsClient.find_device` with:

```python
    async def find_device(self) -> Any:
        return await find_device(self._backend, self._cfg.device, self._platform, lambda r: r.cps_advertised)
```

- [ ] **Step 5: Run the full suite**

Run: `conda run -n wahoo python -m pytest -q -W error`
Expected: PASS — 162 passed (157 existing + 5 new).

- [ ] **Step 6: Commit**

```bash
git add src/wahoo_lsl/ble.py src/wahoo_lsl/config.py tests/test_ble.py tests/test_config.py
git commit -m "refactor: shared find_device and public config section parsers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: FTMS protocol codec (pure)

**Files:**
- Create: `src/wahoo_lsl/ftms/__init__.py`, `src/wahoo_lsl/ftms/protocol.py`
- Test: `tests/test_ftms_protocol.py`

**Interfaces:**
- Produces (`wahoo_lsl.ftms`): `DEFAULT_HOST = "127.0.0.1"`, `DEFAULT_PORT = 47610`.
- Produces (`wahoo_lsl.ftms.protocol`): UUID constants `FTMS_SERVICE_UUID, FEATURE_UUID, INDOOR_BIKE_DATA_UUID, POWER_RANGE_UUID, CONTROL_POINT_UUID, STATUS_UUID`; `ProtocolError(ValueError)`; opcodes `OP_REQUEST_CONTROL=0x00, OP_SET_TARGET_POWER=0x05, OP_START_OR_RESUME=0x07, OP_RESPONSE=0x80`; `RESULT_SUCCESS=0x01`; `STATUS_TARGET_POWER_CHANGED=0x08`, `STATUS_CONTROL_PERMISSION_LOST=0xFF`; `opcode_name(op)`, `result_name(code)`; dataclasses `FtmsFeatures(machine, target, power_target_supported)`, `CpResponse(request_opcode, result)`, `StatusEvent(opcode, name, target_power_w)`, `BikeData(power_w, cadence_rpm, speed_kmh)`, `PowerRange(min_w, max_w, step_w)`; `DEFAULT_POWER_RANGE = PowerRange(0, 2000, 1)`; encoders `request_control()`, `start_or_resume()`, `set_target_power(watts)`; decoders `parse_feature`, `parse_cp_response`, `parse_status`, `parse_bike_data`, `parse_power_range`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ftms_protocol.py`:

```python
import struct

import pytest

from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms import protocol as p
from wahoo_lsl.ftms.protocol import (
    BikeData,
    CpResponse,
    PowerRange,
    ProtocolError,
    StatusEvent,
    parse_bike_data,
    parse_cp_response,
    parse_feature,
    parse_power_range,
    parse_status,
)


def test_package_defaults():
    assert (DEFAULT_HOST, DEFAULT_PORT) == ("127.0.0.1", 47610)


def test_uuids_are_full_lowercase():
    assert p.FTMS_SERVICE_UUID == "00001826-0000-1000-8000-00805f9b34fb"
    assert p.CONTROL_POINT_UUID == "00002ad9-0000-1000-8000-00805f9b34fb"
    assert p.INDOOR_BIKE_DATA_UUID == "00002ad2-0000-1000-8000-00805f9b34fb"
    assert p.STATUS_UUID == "00002ada-0000-1000-8000-00805f9b34fb"
    assert p.FEATURE_UUID == "00002acc-0000-1000-8000-00805f9b34fb"
    assert p.POWER_RANGE_UUID == "00002ad8-0000-1000-8000-00805f9b34fb"


def test_encoders():
    assert p.request_control() == b"\x00"
    assert p.start_or_resume() == b"\x07"
    assert p.set_target_power(150) == b"\x05\x96\x00"
    assert p.set_target_power(0) == b"\x05\x00\x00"
    assert p.set_target_power(-1) == b"\x05\xff\xff"
    with pytest.raises(ValueError):
        p.set_target_power(40000)


def test_names():
    assert p.opcode_name(0x05) == "set_target_power"
    assert p.opcode_name(0x42) == "0x42"
    assert p.result_name(0x03) == "invalid parameter"
    assert p.result_name(0x05) == "control not permitted"
    assert p.result_name(0x77) == "0x77"


def test_feature_power_target_supported():
    f = parse_feature(struct.pack("<II", 0x5, 1 << 3))
    assert f == p.FtmsFeatures(machine=0x5, target=1 << 3, power_target_supported=True)
    assert parse_feature(struct.pack("<II", 0xFFFF, 0)).power_target_supported is False


def test_feature_short_payload_zero_padded():
    assert parse_feature(b"\x00\x00\x00\x00\x08").power_target_supported is True
    assert parse_feature(b"\x01").machine == 1


def test_feature_empty_is_error():
    with pytest.raises(ProtocolError):
        parse_feature(b"")


def test_cp_response():
    assert parse_cp_response(b"\x80\x05\x01") == CpResponse(0x05, 0x01)
    assert parse_cp_response(bytearray(b"\x80\x00\x05\x00")) == CpResponse(0x00, 0x05)
    with pytest.raises(ProtocolError):
        parse_cp_response(b"\x80\x05")
    with pytest.raises(ProtocolError):
        parse_cp_response(b"\x81\x05\x01")


def test_status():
    assert parse_status(b"\x08\x96\x00") == StatusEvent(0x08, "target power changed", 150)
    assert parse_status(b"\xff") == StatusEvent(0xFF, "control permission lost", None)
    assert parse_status(b"\x02\x01").name == "stopped or paused by user"
    assert parse_status(b"\x42") == StatusEvent(0x42, "0x42", None)
    with pytest.raises(ProtocolError):
        parse_status(b"\x08\x96")
    with pytest.raises(ProtocolError):
        parse_status(b"")


def test_bike_data_speed_cadence_power():
    # flags 0x0044: bit0 = 0 -> speed present; bit2 cadence; bit6 power
    data = struct.pack("<HHHh", 0x0044, 2550, 180, 150)
    assert parse_bike_data(data) == BikeData(power_w=150, cadence_rpm=90.0, speed_kmh=25.5)


def test_bike_data_more_data_bit_means_no_speed():
    assert parse_bike_data(struct.pack("<Hh", 0x0041, 200)) == BikeData(200, None, None)


def test_bike_data_without_power():
    assert parse_bike_data(struct.pack("<H", 0x0001)) == BikeData(None, None, None)


def test_bike_data_skips_distance_uint24():
    data = struct.pack("<H", 0x0051) + (1000).to_bytes(3, "little") + struct.pack("<h", 99)
    assert parse_bike_data(data).power_w == 99


def test_bike_data_skips_average_and_resistance_fields():
    # bits 0,1,3,5,6: no speed, avg speed, avg cadence, resistance, power
    data = struct.pack("<HHHhh", 0x006B, 100, 50, 7, 123)
    assert parse_bike_data(data) == BikeData(123, None, None)


def test_bike_data_negative_power():
    assert parse_bike_data(struct.pack("<Hh", 0x0041, -5)).power_w == -5


def test_bike_data_truncated_or_short_is_error():
    with pytest.raises(ProtocolError):
        parse_bike_data(struct.pack("<HH", 0x0044, 2550))
    with pytest.raises(ProtocolError):
        parse_bike_data(b"\x44")


def test_power_range():
    assert parse_power_range(struct.pack("<hhH", 0, 2000, 1)) == PowerRange(0, 2000, 1)
    assert p.DEFAULT_POWER_RANGE == PowerRange(0, 2000, 1)
    with pytest.raises(ProtocolError):
        parse_power_range(b"\x00\x00\xd0\x07\x01")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_protocol.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms'`.

- [ ] **Step 3: Write the implementation**

`src/wahoo_lsl/ftms/__init__.py`:

```python
"""FTMS power control for the Wahoo KICKR (spec 2026-10-21).

This package's __init__ and client.py import only the standard library, so the client can
be used from environments without bleak or pylsl.
"""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47610
```

`src/wahoo_lsl/ftms/protocol.py`:

```python
"""Pure FTMS codec (spec §5): UUIDs, control-point encoders, decoders. No I/O."""
from __future__ import annotations

import struct
from dataclasses import dataclass


def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


FTMS_SERVICE_UUID = _uuid16(0x1826)
FEATURE_UUID = _uuid16(0x2ACC)
INDOOR_BIKE_DATA_UUID = _uuid16(0x2AD2)
POWER_RANGE_UUID = _uuid16(0x2AD8)
CONTROL_POINT_UUID = _uuid16(0x2AD9)
STATUS_UUID = _uuid16(0x2ADA)


class ProtocolError(ValueError):
    """A payload from the device could not be decoded."""


OP_REQUEST_CONTROL = 0x00
OP_SET_TARGET_POWER = 0x05
OP_START_OR_RESUME = 0x07
OP_RESPONSE = 0x80

OPCODE_NAMES = {
    0x00: "request_control",
    0x01: "reset",
    0x05: "set_target_power",
    0x07: "start_or_resume",
    0x08: "stop_or_pause",
}

RESULT_SUCCESS = 0x01
RESULT_NAMES = {
    0x01: "success",
    0x02: "op code not supported",
    0x03: "invalid parameter",
    0x04: "operation failed",
    0x05: "control not permitted",
}

STATUS_TARGET_POWER_CHANGED = 0x08
STATUS_CONTROL_PERMISSION_LOST = 0xFF
STATUS_NAMES = {
    0x01: "reset",
    0x02: "stopped or paused by user",
    0x04: "started or resumed by user",
    0x08: "target power changed",
    0xFF: "control permission lost",
}

POWER_TARGET_SETTING_BIT = 3  # in target_setting_features


def opcode_name(op: int) -> str:
    return OPCODE_NAMES.get(op, f"0x{op:02X}")


def result_name(code: int) -> str:
    return RESULT_NAMES.get(code, f"0x{code:02X}")


@dataclass(frozen=True)
class FtmsFeatures:
    machine: int
    target: int
    power_target_supported: bool


@dataclass(frozen=True)
class CpResponse:
    request_opcode: int
    result: int


@dataclass(frozen=True)
class StatusEvent:
    opcode: int
    name: str
    target_power_w: int | None


@dataclass(frozen=True)
class BikeData:
    power_w: int | None
    cadence_rpm: float | None
    speed_kmh: float | None


@dataclass(frozen=True)
class PowerRange:
    min_w: int
    max_w: int
    step_w: int


DEFAULT_POWER_RANGE = PowerRange(0, 2000, 1)


# ---- encoders --------------------------------------------------------------


def request_control() -> bytes:
    return bytes([OP_REQUEST_CONTROL])


def start_or_resume() -> bytes:
    return bytes([OP_START_OR_RESUME])


def set_target_power(watts: int) -> bytes:
    if not -32768 <= watts <= 32767:
        raise ValueError(f"target power {watts} W does not fit in sint16")
    return struct.pack("<Bh", OP_SET_TARGET_POWER, watts)


# ---- decoders --------------------------------------------------------------


def parse_feature(data: bytes | bytearray) -> FtmsFeatures:
    """Two uint32 (machine, target-setting). Short payloads are zero-padded; empty is an error."""
    data = bytes(data)
    if not data:
        raise ProtocolError("Fitness Machine Feature is empty")
    machine, target = struct.unpack("<II", data[:8].ljust(8, b"\x00"))
    return FtmsFeatures(machine, target, bool((target >> POWER_TARGET_SETTING_BIT) & 1))


def parse_cp_response(data: bytes | bytearray) -> CpResponse:
    data = bytes(data)
    if len(data) < 3:
        raise ProtocolError(f"control point response needs 3 bytes, got {len(data)}")
    if data[0] != OP_RESPONSE:
        raise ProtocolError(f"control point response must start with 0x80, got 0x{data[0]:02X}")
    return CpResponse(data[1], data[2])


def parse_status(data: bytes | bytearray) -> StatusEvent:
    data = bytes(data)
    if not data:
        raise ProtocolError("Fitness Machine Status is empty")
    op = data[0]
    target = None
    if op == STATUS_TARGET_POWER_CHANGED:
        if len(data) < 3:
            raise ProtocolError("target power changed status needs 3 bytes")
        (target,) = struct.unpack_from("<h", data, 1)
    return StatusEvent(op, STATUS_NAMES.get(op, f"0x{op:02X}"), target)


def parse_bike_data(data: bytes | bytearray) -> BikeData:
    """Walk Indoor Bike Data fields in spec order up to instantaneous power (spec §5.5)."""
    data = bytes(data)
    if len(data) < 2:
        raise ProtocolError(f"Indoor Bike Data needs at least 2 bytes, got {len(data)}")
    (flags,) = struct.unpack_from("<H", data, 0)
    offset = 2

    def take(fmt: str, what: str) -> int:
        nonlocal offset
        size = struct.calcsize(fmt)
        if offset + size > len(data):
            raise ProtocolError(f"Indoor Bike Data truncated at {what} (flags 0x{flags:04X})")
        (value,) = struct.unpack_from(fmt, data, offset)
        offset += size
        return value

    speed = cadence = power = None
    if not flags & 0x0001:  # bit 0 "More Data" is inverted: 0 means speed present
        speed = take("<H", "instantaneous speed") / 100
    if flags & 0x0002:
        take("<H", "average speed")
    if flags & 0x0004:
        cadence = take("<H", "instantaneous cadence") / 2
    if flags & 0x0008:
        take("<H", "average cadence")
    if flags & 0x0010:
        take("<H", "total distance")
        take("<B", "total distance")  # uint24 = uint16 + uint8
    if flags & 0x0020:
        take("<h", "resistance level")
    if flags & 0x0040:
        power = take("<h", "instantaneous power")
    return BikeData(power, cadence, speed)


def parse_power_range(data: bytes | bytearray) -> PowerRange:
    data = bytes(data)
    if len(data) < 6:
        raise ProtocolError(f"Supported Power Range needs 6 bytes, got {len(data)}")
    min_w, max_w, step_w = struct.unpack_from("<hhH", data, 0)
    return PowerRange(min_w, max_w, step_w)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_protocol.py -q -W error`
Expected: PASS (17 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/ftms/__init__.py src/wahoo_lsl/ftms/protocol.py tests/test_ftms_protocol.py
git commit -m "feat(ftms): pure FTMS protocol codec

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: FTMS config

**Files:**
- Create: `src/wahoo_lsl/ftms/config.py`, `ftms.example.json`
- Test: `tests/test_ftms_config.py`

**Interfaces:**
- Consumes: `wahoo_lsl.config.{ConfigError, DeviceConfig, ReconnectConfig, parse_device, parse_reconnect, pos_number, section}` (Task 1); `wahoo_lsl.ftms.{DEFAULT_HOST, DEFAULT_PORT}` (Task 2).
- Produces: `FtmsSettings(host, port, max_rate_hz, ack_timeout_s, max_watts)`, `FtmsConfig(device, reconnect, ftms)`, `LOOPBACK_HOSTS`, `load_ftms_config(path) -> FtmsConfig`, `parse_ftms_config(obj) -> FtmsConfig`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ftms_config.py`:

```python
import json
from pathlib import Path

import pytest

from wahoo_lsl.config import ConfigError
from wahoo_lsl.ftms.config import FtmsSettings, load_ftms_config, parse_ftms_config

ROOT = Path(__file__).resolve().parents[1]


def cfg(**ftms):
    return parse_ftms_config({"device": {"name": "KICKR"}, "ftms": ftms})


def test_defaults():
    c = parse_ftms_config({"device": {"name": "KICKR"}})
    assert c.ftms == FtmsSettings("127.0.0.1", 47610, 2.0, 1.0, None)
    assert c.device.name == "KICKR"
    assert c.reconnect.backoff_s == (1.0, 2.0, 5.0, 10.0)


def test_example_file_loads():
    c = load_ftms_config(ROOT / "ftms.example.json")
    assert c.ftms.port == 47610
    assert c.ftms.max_rate_hz == 2.0


def test_values_parsed():
    c = cfg(host="localhost", port=50000, max_rate_hz=4, ack_timeout_s=0.5, max_watts=600)
    assert c.ftms == FtmsSettings("localhost", 50000, 4.0, 0.5, 600)
    assert cfg(host="::1").ftms.host == "::1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "", None, ["127.0.0.1"]])
def test_non_loopback_host_rejected(host):
    with pytest.raises(ConfigError, match="ftms.host"):
        cfg(host=host)


@pytest.mark.parametrize("port", [0, 65536, "47610", True, 1.5])
def test_bad_port(port):
    with pytest.raises(ConfigError, match="ftms.port"):
        cfg(port=port)


@pytest.mark.parametrize("key", ["max_rate_hz", "ack_timeout_s"])
@pytest.mark.parametrize("value", [0, -1, "2", True])
def test_bad_positive_numbers(key, value):
    with pytest.raises(ConfigError, match=key):
        cfg(**{key: value})


@pytest.mark.parametrize("value", [0, -5, 1.5, "300", True])
def test_bad_max_watts(value):
    with pytest.raises(ConfigError, match="ftms.max_watts"):
        cfg(max_watts=value)


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown"):
        parse_ftms_config({"device": {"name": "K"}, "measurements": ["power"]})
    with pytest.raises(ConfigError, match="unknown"):
        cfg(rate=2)


def test_device_section_shared_rules():
    with pytest.raises(ConfigError, match="device.mac"):
        parse_ftms_config({"device": {"name": "K", "mac": "nope"}})
    with pytest.raises(ConfigError, match="device"):
        parse_ftms_config({"ftms": {}})


def test_not_an_object():
    with pytest.raises(ConfigError):
        parse_ftms_config([])


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_ftms_config(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_ftms_config(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"device": {"name": "K"}}), encoding="utf-8")
    assert load_ftms_config(good).device.name == "K"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_config.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms.config'`.

- [ ] **Step 3: Write the implementation**

`src/wahoo_lsl/ftms/config.py`:

```python
"""FTMS controller config (spec §6). A separate file from the logger config."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    ConfigError,
    DeviceConfig,
    ReconnectConfig,
    parse_device,
    parse_reconnect,
    pos_number,
    section,
)
from . import DEFAULT_HOST, DEFAULT_PORT

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_TOP_KEYS = {"device", "reconnect", "ftms"}
_FTMS_KEYS = {"host", "port", "max_rate_hz", "ack_timeout_s", "max_watts"}


@dataclass(frozen=True)
class FtmsSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    max_rate_hz: float = 2.0
    ack_timeout_s: float = 1.0
    max_watts: int | None = None


@dataclass(frozen=True)
class FtmsConfig:
    device: DeviceConfig
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    ftms: FtmsSettings = field(default_factory=FtmsSettings)


def load_ftms_config(path: str | Path) -> FtmsConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from None
    return parse_ftms_config(obj)


def parse_ftms_config(obj: Any) -> FtmsConfig:
    if not isinstance(obj, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(obj) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")
    if "device" not in obj:
        raise ConfigError("missing required section 'device'")
    return FtmsConfig(
        device=parse_device(section(obj, "device", {"name", "mac", "uuid", "scan_timeout_s"})),
        reconnect=parse_reconnect(section(obj, "reconnect", {"enabled", "max_attempts", "backoff_s"})),
        ftms=_parse_ftms(section(obj, "ftms", _FTMS_KEYS)),
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_ftms(d: dict) -> FtmsSettings:
    host = d.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        raise ConfigError(f"ftms.host must be one of {sorted(LOOPBACK_HOSTS)} (localhost only), got {host!r}")
    port = d.get("port", DEFAULT_PORT)
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ConfigError(f"ftms.port must be an integer 1..65535, got {port!r}")
    max_watts = d.get("max_watts")
    if max_watts is not None and (not _is_int(max_watts) or max_watts <= 0):
        raise ConfigError(f"ftms.max_watts must be a positive integer or null, got {max_watts!r}")
    return FtmsSettings(
        host=host,
        port=port,
        max_rate_hz=pos_number(d.get("max_rate_hz", 2), "ftms.max_rate_hz"),
        ack_timeout_s=pos_number(d.get("ack_timeout_s", 1.0), "ftms.ack_timeout_s"),
        max_watts=max_watts,
    )
```

`ftms.example.json`:

```json
{
  "device": {
    "name": "KICKR",
    "mac": null,
    "uuid": null,
    "scan_timeout_s": 10
  },
  "reconnect": {
    "enabled": true,
    "max_attempts": 0,
    "backoff_s": [1, 2, 5, 10]
  },
  "ftms": {
    "host": "127.0.0.1",
    "port": 47610,
    "max_rate_hz": 2,
    "ack_timeout_s": 1.0,
    "max_watts": null
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_config.py -q -W error`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/ftms/config.py ftms.example.json tests/test_ftms_config.py
git commit -m "feat(ftms): controller config with loopback-only endpoint

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: FTMS BLE link

**Files:**
- Create: `src/wahoo_lsl/ftms/link.py`, `tests/ftms_fakes.py`
- Test: `tests/test_ftms_link.py`

**Interfaces:**
- Consumes: `wahoo_lsl.ble.{BleBackend, BleakBackend, BleSetupError, SHUTDOWN_TIMEOUT_S, _bounded, find_device}` (Task 1; `_bounded` is package-internal and reads `ble.SHUTDOWN_TIMEOUT_S` at call time); `wahoo_lsl.ftms.protocol` (Task 2); `FtmsConfig` (Task 3).
- Produces: `FtmsSetupError(BleSetupError)`, `FtmsUnsupported(FtmsSetupError)`, `LinkError(Exception)`, `CommandTimeout(LinkError)`, `CommandRejected(Exception)` with `.opcode`, `.result`; `FtmsLink(cfg, *, backend=None, platform=None)` with attributes `on_bike_data(data: BikeData, t_lsl: float)`, `on_status(event: StatusEvent, t_lsl: float)`, `on_link_lost(reason: str)`, `power_range: PowerRange`, `features: FtmsFeatures | None`, `device_label: str`; methods `async open()`, `async command(payload: bytes) -> CpResponse`, `async close()`, property `connected`.
- Produces (tests): `tests/ftms_fakes.py` with `FTMS_KICKR`, `POWER_TARGET_FEATURE`, `POWER_RANGE`, `FakeFtmsBackend`, `FakeFtmsClient` (used again in Task 8).

- [ ] **Step 1: Write the fakes**

`tests/ftms_fakes.py`:

```python
"""In-memory stand-in for a KICKR's FTMS over bleak (link and CLI tests)."""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

from bleak.exc import BleakError

from wahoo_lsl.ble import ScanResult
from wahoo_lsl.ftms import protocol as p

FTMS_KICKR = SimpleNamespace(name="KICKR V6 1234", address="AA:BB:CC:DD:EE:02")
POWER_TARGET_FEATURE = struct.pack("<II", 0, 1 << 3)
POWER_RANGE = struct.pack("<hhH", 0, 2000, 1)
ALL_CHARS = (p.FEATURE_UUID, p.INDOOR_BIKE_DATA_UUID, p.POWER_RANGE_UUID, p.CONTROL_POINT_UUID, p.STATUS_UUID)


class FakeFtmsService:
    def __init__(self, uuids) -> None:
        self._uuids = set(uuids)

    def get_characteristic(self, uuid: str):
        return SimpleNamespace(uuid=uuid) if uuid in self._uuids else None


class FakeFtmsServices:
    def __init__(self, backend: "FakeFtmsBackend") -> None:
        self._backend = backend

    def get_service(self, uuid: str):
        if uuid == p.FTMS_SERVICE_UUID and self._backend.has_ftms:
            return FakeFtmsService(self._backend.char_uuids)
        return None


class FakeFtmsClient:
    def __init__(self, backend: "FakeFtmsBackend", device, disconnected_callback) -> None:
        self.backend = backend
        self.device = device
        self._disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = FakeFtmsServices(backend)
        self.callbacks: dict = {}

    async def connect(self) -> None:
        if self.backend.connect_failures > 0:
            self.backend.connect_failures -= 1
            raise OSError("simulated connect failure")
        self.is_connected = True

    async def read_gatt_char(self, char) -> bytearray:
        if char.uuid == p.FEATURE_UUID:
            return bytearray(self.backend.feature_bytes)
        if char.uuid == p.POWER_RANGE_UUID:
            return bytearray(self.backend.power_range_bytes)
        raise AssertionError(f"unexpected read {char.uuid}")

    async def start_notify(self, char, callback) -> None:
        self.backend.log.append(("subscribe", char.uuid))
        self.callbacks[char.uuid] = callback

    async def stop_notify(self, char) -> None:
        self.backend.log.append(("unsubscribe", char.uuid))
        self.callbacks.pop(char.uuid, None)

    async def write_gatt_char(self, char, data, response: bool = False) -> None:
        assert char.uuid == p.CONTROL_POINT_UUID and response is True
        data = bytes(data)
        self.backend.log.append(("write", data))
        self.backend.writes.append(data)
        op = data[0]
        behaviour = self.backend.behaviour(op)  # "ok" | "early" | "drop" | "raise" | int result code
        if behaviour == "raise":
            raise BleakError("simulated write failure")
        if behaviour == "drop":
            return
        result = p.RESULT_SUCCESS if behaviour in ("ok", "early") else behaviour
        indication = bytearray([p.OP_RESPONSE, op, result])
        callback = self.callbacks.get(p.CONTROL_POINT_UUID)
        if callback is None:
            return  # not subscribed: a real device sends no indication either
        if behaviour == "early":
            callback(None, indication)  # indication arrives before the write call returns
        else:
            asyncio.get_running_loop().call_soon(callback, None, indication)

    async def disconnect(self) -> None:
        if self.backend.hang_disconnect:
            await asyncio.Event().wait()
        was_connected = self.is_connected
        self.is_connected = False
        if was_connected:
            self._disconnected_callback(self)  # bleak also calls it on intentional disconnects

    def drop(self) -> None:
        """Simulate link loss."""
        self.is_connected = False
        self._disconnected_callback(self)

    def notify(self, uuid: str, payload: bytes) -> None:
        self.callbacks[uuid](None, bytearray(payload))


class FakeFtmsBackend:
    def __init__(
        self,
        scan_results=None,
        *,
        has_ftms: bool = True,
        feature_bytes: bytes = POWER_TARGET_FEATURE,
        power_range_bytes: bytes = POWER_RANGE,
        has_power_range: bool = True,
        connect_failures: int = 0,
        hang_disconnect: bool = False,
        responses=None,
    ) -> None:
        if scan_results is None:
            scan_results = [
                ScanResult(FTMS_KICKR.name, FTMS_KICKR.address, -50, False, FTMS_KICKR, frozenset({p.FTMS_SERVICE_UUID}))
            ]
        self.scan_results = list(scan_results)
        self.has_ftms = has_ftms
        self.feature_bytes = feature_bytes
        self.power_range_bytes = power_range_bytes
        self.char_uuids = [u for u in ALL_CHARS if has_power_range or u != p.POWER_RANGE_UUID]
        self.connect_failures = connect_failures
        self.hang_disconnect = hang_disconnect
        # opcode -> behaviours consumed one per write, in order; "ok" once exhausted
        self.responses = {op: list(seq) for op, seq in (responses or {}).items()}
        self.log: list = []
        self.writes: list[bytes] = []
        self.clients: list[FakeFtmsClient] = []
        self.address_lookups: list[str] = []

    def behaviour(self, op: int):
        queue = self.responses.get(op)
        return queue.pop(0) if queue else "ok"

    async def discover(self, timeout: float):
        return list(self.scan_results)

    async def find_by_address(self, address: str, timeout: float):
        self.address_lookups.append(address)
        for r in self.scan_results:
            if r.address.lower() == address.lower():
                return r.device
        return None

    def make_client(self, device, disconnected_callback) -> FakeFtmsClient:
        client = FakeFtmsClient(self, device, disconnected_callback)
        self.clients.append(client)
        return client
```

- [ ] **Step 2: Write the failing tests**

`tests/test_ftms_link.py`:

```python
import asyncio
import logging
import struct
from types import SimpleNamespace

import pytest
from ftms_fakes import FTMS_KICKR, FakeFtmsBackend

import wahoo_lsl.ble as ble_mod
from wahoo_lsl.ble import ScanResult
from wahoo_lsl.ftms import protocol as p
from wahoo_lsl.ftms.config import parse_ftms_config
from wahoo_lsl.ftms.link import (
    CommandRejected,
    CommandTimeout,
    FtmsLink,
    FtmsSetupError,
    FtmsUnsupported,
    LinkError,
)


def make_cfg(ack_timeout_s=0.05):
    return parse_ftms_config(
        {"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01]}, "ftms": {"ack_timeout_s": ack_timeout_s}}
    )


def make_link(backend, ack_timeout_s=0.05):
    link = FtmsLink(make_cfg(ack_timeout_s), backend=backend, platform="linux")
    lost: list[str] = []
    link.on_link_lost = lost.append
    return link, lost


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def test_open_subscribes_then_takes_control_at_zero_watts():
    backend = FakeFtmsBackend()
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        assert link.connected
        await link.close()

    asyncio.run(scenario())
    kinds = [entry[0] for entry in backend.log]
    first_write = kinds.index("write")
    subscribed_before_write = [e[1] for e in backend.log[:first_write] if e[0] == "subscribe"]
    assert subscribed_before_write == [p.CONTROL_POINT_UUID, p.STATUS_UUID, p.INDOOR_BIKE_DATA_UUID]
    assert backend.writes == [b"\x00", b"\x07", b"\x05\x00\x00"]
    assert link.power_range == p.PowerRange(0, 2000, 1)
    assert link.features.power_target_supported


def test_open_prefers_device_advertising_ftms():
    cps_only = SimpleNamespace(name="KICKR V6 1234", address="AA:BB:CC:DD:EE:09")
    backend = FakeFtmsBackend(
        [
            ScanResult(cps_only.name, cps_only.address, -30, True, cps_only),
            ScanResult(FTMS_KICKR.name, FTMS_KICKR.address, -50, False, FTMS_KICKR, frozenset({p.FTMS_SERVICE_UUID})),
        ]
    )
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        await link.close()

    asyncio.run(scenario())
    assert backend.clients[0].device is FTMS_KICKR


def test_open_without_ftms_service_is_setup_error():
    backend = FakeFtmsBackend(has_ftms=False)
    link, lost = make_link(backend)
    with pytest.raises(FtmsSetupError, match="Fitness Machine Service"):
        asyncio.run(link.open())
    assert backend.clients[0].is_connected is False
    assert lost == []


def test_open_power_target_unsupported():
    backend = FakeFtmsBackend(feature_bytes=struct.pack("<II", 0xFF, 0))
    link, _ = make_link(backend)
    with pytest.raises(FtmsUnsupported):
        asyncio.run(link.open())
    assert backend.writes == []


def test_open_missing_power_range_uses_default(caplog):
    backend = FakeFtmsBackend(has_power_range=False)
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        await link.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert link.power_range == p.DEFAULT_POWER_RANGE
    assert any("Supported Power Range" in r.getMessage() for r in caplog.records)


def test_open_request_control_rejected_is_setup_error():
    backend = FakeFtmsBackend(responses={0x00: [0x05]})
    link, _ = make_link(backend)
    with pytest.raises(FtmsSetupError, match="control not permitted"):
        asyncio.run(link.open())
    assert backend.clients[0].is_connected is False


def test_open_connect_failure_is_setup_error():
    backend = FakeFtmsBackend(connect_failures=1)
    link, _ = make_link(backend)
    with pytest.raises(FtmsSetupError, match="simulated connect failure"):
        asyncio.run(link.open())


def test_command_success_and_early_indication():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "ok", "early"]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        first = await link.command(p.set_target_power(150))
        second = await link.command(p.set_target_power(160))
        await link.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == p.CpResponse(0x05, 0x01)
    assert second == p.CpResponse(0x05, 0x01)


def test_command_timeout_then_next_command_works():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "drop"]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        with pytest.raises(CommandTimeout):
            await link.command(p.set_target_power(150))
        assert (await link.command(p.set_target_power(150))).result == p.RESULT_SUCCESS
        await link.close()

    asyncio.run(scenario())


def test_command_rejected():
    backend = FakeFtmsBackend(responses={0x05: ["ok", 0x03]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        with pytest.raises(CommandRejected, match="invalid parameter") as info:
            await link.command(p.set_target_power(9999))
        assert (info.value.opcode, info.value.result) == (0x05, 0x03)
        await link.close()

    asyncio.run(scenario())


def test_command_write_failure_is_link_error_not_timeout():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "raise"]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        with pytest.raises(LinkError) as info:
            await link.command(p.set_target_power(150))
        assert not isinstance(info.value, CommandTimeout)
        await link.close()

    asyncio.run(scenario())


def test_command_when_not_connected():
    link, _ = make_link(FakeFtmsBackend())
    with pytest.raises(LinkError, match="not connected"):
        asyncio.run(link.command(p.set_target_power(100)))


def test_unsolicited_and_malformed_indications(caplog):
    backend = FakeFtmsBackend()
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        client = backend.clients[0]
        client.notify(p.CONTROL_POINT_UUID, b"\x80\x05\x01")  # nobody waiting: ignored
        client.notify(p.CONTROL_POINT_UUID, b"\x01")  # malformed: warning
        await link.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert any("control point" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_status_and_bike_data_forwarded_with_timestamps():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)
    statuses, bikes = [], []
    link.on_status = lambda ev, t: statuses.append((ev, t))
    link.on_bike_data = lambda data, t: bikes.append((data, t))

    async def scenario():
        await link.open()
        client = backend.clients[0]
        client.notify(p.STATUS_UUID, b"\x08\x96\x00")
        client.notify(p.INDOOR_BIKE_DATA_UUID, struct.pack("<Hh", 0x0041, 150))
        client.notify(p.INDOOR_BIKE_DATA_UUID, b"\x44")  # malformed: dropped with a warning
        await link.close()

    asyncio.run(scenario())
    assert statuses[0][0] == p.StatusEvent(0x08, "target power changed", 150)
    assert bikes[0][0] == p.BikeData(150, None, None)
    assert isinstance(statuses[0][1], float) and isinstance(bikes[0][1], float)
    assert len(bikes) == 1
    assert lost == []


def test_control_permission_lost_reports_link_lost():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)

    async def scenario():
        await link.open()
        backend.clients[0].notify(p.STATUS_UUID, b"\xff")
        await until(lambda: lost)
        await link.close()

    asyncio.run(scenario())
    assert lost == ["control permission lost"]


def test_unexpected_disconnect_reports_link_lost():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)

    async def scenario():
        await link.open()
        backend.clients[0].drop()
        await until(lambda: lost)
        await link.close()

    asyncio.run(scenario())
    assert lost == ["connection lost"]


def test_close_is_quiet_and_idempotent():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)

    async def scenario():
        await link.open()
        await link.close()
        await link.close()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert lost == []
    assert not link.connected
    assert ("unsubscribe", p.CONTROL_POINT_UUID) in backend.log


def test_close_fails_pending_command():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "drop"]})
    link, _ = make_link(backend, ack_timeout_s=5.0)

    async def scenario():
        await link.open()
        pending = asyncio.create_task(link.command(p.set_target_power(150)))
        await until(lambda: len(backend.writes) == 4)
        await link.close()
        with pytest.raises(LinkError, match="link closed"):
            await pending

    asyncio.run(scenario())


def test_close_bounded_when_disconnect_hangs(monkeypatch):
    monkeypatch.setattr(ble_mod, "SHUTDOWN_TIMEOUT_S", 0.05)
    backend = FakeFtmsBackend()
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        backend.hang_disconnect = True
        await asyncio.wait_for(link.close(), 1.0)

    asyncio.run(scenario())
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_link.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms.link'`.

- [ ] **Step 4: Write the implementation**

`src/wahoo_lsl/ftms/link.py`:

```python
"""FTMS BLE link (spec §7.2–7.3): own connection, control-point commands, notifications.

The only ftms module that touches bleak; discovery and bounded shutdown come from ble.py.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from bleak.exc import BleakError

from .. import clock
from ..ble import BleBackend, BleakBackend, BleSetupError, _bounded, find_device
from . import protocol as p
from .config import FtmsConfig

log = logging.getLogger(__name__)


class FtmsSetupError(BleSetupError):
    """Could not connect to / set up FTMS control. CLI exit code 3."""


class FtmsUnsupported(FtmsSetupError):
    """The device does not support power target setting."""


class LinkError(Exception):
    """Transport failure: not connected, write failed, or link closed while waiting."""


class CommandTimeout(LinkError):
    """No control-point response within ack_timeout_s."""


class CommandRejected(Exception):
    """The device answered a control-point command with a result other than success."""

    def __init__(self, opcode: int, result: int) -> None:
        super().__init__(f"{p.opcode_name(opcode)} rejected: {p.result_name(result)}")
        self.opcode = opcode
        self.result = result


class FtmsLink:
    """One BLE connection to the trainer's Fitness Machine Service."""

    def __init__(self, cfg: FtmsConfig, *, backend: BleBackend | None = None, platform: str | None = None) -> None:
        self._cfg = cfg
        self._backend = backend or BleakBackend()
        self._platform = platform or sys.platform
        self._client: Any = None  # None means "ignore disconnect callbacks"
        self._chars: dict[str, Any] = {}
        self._subscribed: list[Any] = []
        self._lock = asyncio.Lock()
        self._waiters: dict[int, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.power_range: p.PowerRange = p.DEFAULT_POWER_RANGE
        self.features: p.FtmsFeatures | None = None
        self.device_label = "device"
        # Handlers, assigned by the Controller. All are called on the event-loop thread.
        self.on_bike_data = lambda data, t_lsl: None
        self.on_status = lambda event, t_lsl: None
        self.on_link_lost = lambda reason: None

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "is_connected", False))

    # ---- setup ---------------------------------------------------------------

    async def open(self) -> None:
        """Connect, check features, subscribe, take control, start, set 0 W (spec §7.3 steps 1–4).

        Raises BleSetupError (FtmsSetupError for FTMS-specific failures); leaves nothing connected.
        """
        self._loop = asyncio.get_running_loop()
        try:
            await self._open()
        except BleSetupError:
            await self.close()
            raise
        except (p.ProtocolError, LinkError, CommandRejected, BleakError, OSError) as e:
            await self.close()
            raise FtmsSetupError(f"FTMS setup failed: {e}") from e
        except BaseException:
            await self.close()
            raise

    async def _open(self) -> None:
        device = await find_device(
            self._backend, self._cfg.device, self._platform, lambda r: p.FTMS_SERVICE_UUID in r.service_uuids
        )
        self.device_label = getattr(device, "name", None) or getattr(device, "address", str(device))
        client = self._backend.make_client(device, self._on_disconnect)
        self._client = client
        await client.connect()
        service = client.services.get_service(p.FTMS_SERVICE_UUID)
        if service is None:
            raise FtmsSetupError(f"{self.device_label} does not expose the Fitness Machine Service")
        # Resolve on the service, not the collection: other services may repeat a UUID.
        chars = {}
        for key, uuid in (
            ("feature", p.FEATURE_UUID),
            ("control_point", p.CONTROL_POINT_UUID),
            ("status", p.STATUS_UUID),
            ("bike_data", p.INDOOR_BIKE_DATA_UUID),
        ):
            char = service.get_characteristic(uuid)
            if char is None:
                raise FtmsSetupError(f"Fitness Machine Service has no {key} characteristic")
            chars[key] = char
        self._chars = chars
        self.features = p.parse_feature(await client.read_gatt_char(chars["feature"]))
        if not self.features.power_target_supported:
            raise FtmsUnsupported(
                f"{self.device_label} does not support power target setting "
                f"(target-setting features 0x{self.features.target:08x})"
            )
        range_char = service.get_characteristic(p.POWER_RANGE_UUID)
        if range_char is None:
            self.power_range = p.DEFAULT_POWER_RANGE
            log.warning("No Supported Power Range characteristic; assuming %s", self.power_range)
        else:
            self.power_range = p.parse_power_range(await client.read_gatt_char(range_char))
        # Control point first: indications must be enabled before the first write.
        for key, handler in (("control_point", self._on_cp), ("status", self._on_status), ("bike_data", self._on_bike_data)):
            await client.start_notify(chars[key], handler)
            self._subscribed.append(chars[key])
        for payload in (p.request_control(), p.start_or_resume(), p.set_target_power(0)):
            await self.command(payload)
        r = self.power_range
        log.info("FTMS control of %s at 0 W (power range %d..%d W, step %d)", self.device_label, r.min_w, r.max_w, r.step_w)

    # ---- commands ------------------------------------------------------------

    async def command(self, payload: bytes) -> p.CpResponse:
        """Write one control-point op and wait for its response (one op in flight at a time)."""
        opcode = payload[0]
        name = p.opcode_name(opcode)
        async with self._lock:
            client = self._client
            if client is None:
                raise LinkError(f"{name}: not connected")
            future = asyncio.get_running_loop().create_future()
            self._waiters[opcode] = future  # before writing: the indication may beat the write's return
            started = time.perf_counter()
            log.debug("CP write %s %s", name, payload.hex())
            timeout = self._cfg.ftms.ack_timeout_s
            try:
                response = await asyncio.wait_for(self._write_and_wait(client, payload, future), timeout)
            except asyncio.TimeoutError:  # before OSError: TimeoutError subclasses OSError on 3.11+
                raise CommandTimeout(f"{name}: no response within {timeout:g}s") from None
            except (BleakError, OSError) as e:
                raise LinkError(f"{name} write failed: {e}") from e
            finally:
                self._waiters.pop(opcode, None)
            log.debug(
                "CP %s -> %s in %.1f ms", name, p.result_name(response.result), (time.perf_counter() - started) * 1000
            )
            if response.result != p.RESULT_SUCCESS:
                raise CommandRejected(opcode, response.result)
            return response

    async def _write_and_wait(self, client: Any, payload: bytes, future: asyncio.Future) -> p.CpResponse:
        await client.write_gatt_char(self._chars["control_point"], payload, response=True)
        return await future

    # ---- callbacks (event-loop thread) ---------------------------------------

    def _on_cp(self, _sender: Any, data: bytearray) -> None:
        raw = bytes(data)
        try:
            response = p.parse_cp_response(raw)
        except p.ProtocolError as e:
            log.warning("Bad control point indication %s: %s", raw.hex(), e)
            return
        log.debug("CP indication %s", raw.hex())
        future = self._waiters.get(response.request_opcode)
        if future is None or future.done():
            log.debug("Unsolicited control point response %s ignored", raw.hex())
            return
        future.set_result(response)

    def _on_status(self, _sender: Any, data: bytearray) -> None:
        t_lsl = clock.now_lsl()
        raw = bytes(data)
        try:
            event = p.parse_status(raw)
        except p.ProtocolError as e:
            log.warning("Bad machine status %s: %s", raw.hex(), e)
            return
        log.debug("status %s -> %s", raw.hex(), event)
        if event.opcode == p.STATUS_CONTROL_PERMISSION_LOST:
            log.warning("Trainer reports control permission lost (another app took control?)")
            self.on_link_lost("control permission lost")
        self.on_status(event, t_lsl)

    def _on_bike_data(self, _sender: Any, data: bytearray) -> None:
        t_lsl = clock.now_lsl()
        raw = bytes(data)
        try:
            bike = p.parse_bike_data(raw)
        except p.ProtocolError as e:
            log.warning("Bad indoor bike data %s: %s", raw.hex(), e)
            return
        log.debug("bike data %s -> %s", raw.hex(), bike)
        self.on_bike_data(bike, t_lsl)

    def _on_disconnect(self, client: Any) -> None:
        if client is not self._client:
            return  # intentional close (we cleared _client first) or a stale client
        log.warning("FTMS connection to %s lost", self.device_label)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.on_link_lost, "connection lost")

    # ---- shutdown --------------------------------------------------------------

    async def close(self) -> None:
        """Fail pending commands, unsubscribe and disconnect; each step bounded. Idempotent."""
        client, self._client = self._client, None  # clear first so the callback ignores this disconnect
        for future in self._waiters.values():
            if not future.done():
                future.set_exception(LinkError("link closed"))
        subscribed, self._subscribed = self._subscribed, []
        if client is None:
            return
        if getattr(client, "is_connected", False):
            for char in subscribed:
                await _bounded(client.stop_notify(char), "stop_notify")
        await _bounded(client.disconnect(), "disconnect")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_link.py -q -W error`
Expected: PASS. Then the full suite: `conda run -n wahoo python -m pytest -q -W error` → all pass.

- [ ] **Step 6: Commit**

```bash
git add src/wahoo_lsl/ftms/link.py tests/ftms_fakes.py tests/test_ftms_link.py
git commit -m "feat(ftms): BLE link with verified control-point commands

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Controller (state, rate-limited sender, spin-down, reconnect)

**Files:**
- Create: `src/wahoo_lsl/ftms/controller.py`
- Test: `tests/test_ftms_controller.py`

**Interfaces:**
- Consumes: `FtmsLink` contract from Task 4 (`open`, `command`, `close`, `power_range`, `on_*` attributes; `LinkError`, `CommandRejected`); `wahoo_lsl.ble.{BleSetupError, ReconnectExhausted, ReconnectPolicy, _sleep_unless, _unless_stopped}`; `FtmsConfig`; `wahoo_lsl.clock.now_lsl`.
- Produces: `LinkDown(Exception)`; `State` dataclass; `SPIN_DOWN_TIMEOUT_S = 2.0`, `MAX_LINK_FAILURES = 3`, `MAX_REJECTIONS = 3`; `Controller(cfg, link, *, now=clock.now_lsl, sleep=asyncio.sleep)` with `state`, `pending`, `async start()`, `async run(stop)`, `async close(reason="shutdown")`, `request_target(watts) -> tuple[int, bool]`, `clamp(watts) -> int`, `async spin_down(reason) -> bool`, `snapshot() -> dict`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ftms_controller.py`:

```python
import asyncio
import struct

import pytest

from wahoo_lsl.ble import ReconnectExhausted
from wahoo_lsl.ftms.config import parse_ftms_config
from wahoo_lsl.ftms.controller import Controller, LinkDown
from wahoo_lsl.ftms.link import CommandRejected, FtmsSetupError, LinkError
from wahoo_lsl.ftms.protocol import BikeData, CpResponse, PowerRange, StatusEvent


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class FakeLink:
    """Controller-facing stand-in for FtmsLink. command_results entries: None (success),
    an exception instance (raised), or an asyncio.Future (awaited, then treated the same)."""

    def __init__(self, clock: FakeClock, power_range: PowerRange = PowerRange(0, 1500, 1)) -> None:
        self.clock = clock
        self.power_range = power_range
        self.sent: list[tuple[float, int]] = []
        self.opens = 0
        self.closes = 0
        self.open_errors: list[BaseException] = []
        self.command_results: list = []
        self.on_bike_data = self.on_status = self.on_link_lost = None

    async def open(self) -> None:
        self.opens += 1
        if self.open_errors:
            raise self.open_errors.pop(0)

    async def command(self, payload: bytes) -> CpResponse:
        watts = struct.unpack_from("<h", payload, 1)[0]
        self.sent.append((self.clock.t, watts))
        outcome = self.command_results.pop(0) if self.command_results else None
        if isinstance(outcome, asyncio.Future):
            outcome = await outcome
        if isinstance(outcome, BaseException):
            raise outcome
        await asyncio.sleep(0)
        return CpResponse(payload[0], 1)

    async def close(self) -> None:
        self.closes += 1


def make(ftms=None, reconnect=None, power_range=PowerRange(0, 1500, 1)):
    cfg = parse_ftms_config(
        {
            "device": {"name": "KICKR"},
            "reconnect": {"backoff_s": [0.01], **(reconnect or {})},
            "ftms": ftms or {},
        }
    )
    clock = FakeClock()
    link = FakeLink(clock, power_range)
    return Controller(cfg, link, now=clock.now, sleep=clock.sleep), link, clock


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def watts(link):
    return [w for _, w in link.sent]


def test_clamp_and_step():
    ctl, _, _ = make(ftms={"max_watts": 798}, power_range=PowerRange(0, 1000, 5))
    assert ctl.clamp(-5) == 0
    assert ctl.clamp(1203) == 795
    assert ctl.clamp(152) == 150
    assert ctl.clamp(153) == 155
    assert ctl.clamp(799) == 795
    ctl2, _, _ = make(power_range=PowerRange(50, 1000, 0))
    assert ctl2.clamp(0) == 50
    assert ctl2.clamp(77) == 77


def test_request_before_start_raises_link_down():
    ctl, _, _ = make()
    with pytest.raises(LinkDown):
        ctl.request_target(100)


def test_start_marks_ready_at_zero():
    ctl, link, clock = make()
    asyncio.run(ctl.start())
    s = ctl.state
    assert (s.link, s.link_epoch, s.requested_w, s.confirmed_w, s.confirmed_t_lsl) == ("ready", 1, 0, 0, clock.t)
    assert ctl.pending is False
    assert link.sent == []


def test_single_set_sent_immediately():
    ctl, link, clock = make()

    async def scenario():
        await ctl.start()
        clock.t = 200.0
        assert ctl.request_target(150) == (150, False)
        await until(lambda: ctl.state.confirmed_w == 150)
        await ctl.close()

    asyncio.run(scenario())
    assert link.sent[0] == (200.0, 150)
    assert ctl.state.confirmed_t_lsl is not None


def test_burst_coalesced_and_rate_limited():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        ctl.request_target(150)
        ctl.request_target(200)
        await until(lambda: ctl.state.confirmed_w == 200)
        await asyncio.sleep(0.01)
        sent = list(link.sent)
        await ctl.close()
        return sent

    sent = asyncio.run(scenario())
    assert [w for _, w in sent] == [100, 200]
    assert sent[1][0] - sent[0][0] == pytest.approx(0.5)


def test_set_equal_to_confirmed_is_not_sent():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        assert ctl.request_target(0) == (0, False)
        await asyncio.sleep(0.01)
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link) == [0]  # only the shutdown spin-down


def test_clamped_request_reports_clamped():
    ctl, _, _ = make(ftms={"max_watts": 400})

    async def scenario():
        await ctl.start()
        result = ctl.request_target(450)
        await ctl.close()
        return result

    assert asyncio.run(scenario()) == (400, True)


def test_rejection_retried_then_abandoned_until_new_request():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [CommandRejected(0x05, 0x04)] * 5
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 3)
        await asyncio.sleep(0.02)
        assert len(link.sent) == 3
        assert "rejected" in ctl.state.last_error
        assert ctl.pending
        link.command_results = []
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link)[:4] == [100, 100, 100, 100]


def test_link_failures_trigger_link_lost():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [LinkError("write failed")] * 3
        ctl.request_target(100)
        await until(lambda: ctl.state.link == "reconnecting")
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link) == [100, 100, 100]  # no spin-down send: the link is not ready
    assert "link lost: 3 consecutive control-point failures" in ctl.state.last_error


def test_status_target_power_changed_confirms():
    ctl, link, _ = make()
    asyncio.run(ctl.start())
    link.on_status(StatusEvent(0x08, "target power changed", 180), 5.0)
    assert (ctl.state.confirmed_w, ctl.state.confirmed_t_lsl) == (180, 5.0)
    link.on_status(StatusEvent(0x02, "stopped or paused by user", None), 6.0)
    assert ctl.state.confirmed_w == 180


def test_bike_data_cached_and_snapshot():
    ctl, link, clock = make()
    asyncio.run(ctl.start())
    link.on_bike_data(BikeData(143, 90.0, 30.0), 99.5)
    link.on_bike_data(BikeData(None, None, 30.0), 99.9)
    snap = ctl.snapshot()
    assert snap == {
        "link": "ready",
        "link_epoch": 1,
        "target": {"requested_w": 0, "confirmed_w": 0, "pending": False, "t_lsl": clock.t},
        "power": {"watts": 143, "t_lsl": 99.5, "age_s": 0.5},
        "last_error": None,
        "last_error_t_lsl": None,
    }


def test_snapshot_before_any_power():
    ctl, _, _ = make()
    assert ctl.snapshot()["power"] == {"watts": None, "t_lsl": None, "age_s": None}
    assert ctl.snapshot()["link"] == "connecting"


def test_spin_down_bypasses_rate_limit():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)
        t_first = link.sent[0][0]
        assert await ctl.spin_down("test") is True
        assert link.sent[-1] == (t_first, 0)
        await ctl.close()

    asyncio.run(scenario())
    assert ctl.state.requested_w == 0


@pytest.mark.parametrize("error", [LinkError("gone"), CommandRejected(0x05, 0x04), RuntimeError("bug")])
def test_spin_down_never_raises(error):
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [error]
        ok = await ctl.spin_down("test")
        await ctl.close()
        return ok

    assert asyncio.run(scenario()) is False
    assert "spin-down (test) failed" in ctl.state.last_error


def test_spin_down_when_not_ready_sends_nothing():
    ctl, link, _ = make()
    assert asyncio.run(ctl.spin_down("test")) is False
    assert link.sent == []


def test_link_lost_then_reconnect_resets_to_zero():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)
        link.on_link_lost("connection lost")
        assert ctl.state.link == "reconnecting"
        assert (ctl.state.requested_w, ctl.state.confirmed_w) == (0, None)
        with pytest.raises(LinkDown):
            ctl.request_target(120)
        await until(lambda: ctl.state.link_epoch == 2)
        stop.set()
        await task
        await ctl.close()

    asyncio.run(scenario())
    s = ctl.state
    assert link.opens == 2 and link.closes >= 1
    assert "link lost: connection lost" in s.last_error
    assert s.last_error_t_lsl is not None


def test_stale_confirmation_ignored_after_reconnect():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        gate = asyncio.get_running_loop().create_future()
        link.command_results = [gate]
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        link.on_link_lost("connection lost")
        await until(lambda: ctl.state.link_epoch == 2)
        gate.set_result(None)  # the old in-flight set "succeeds" after the reconnect
        await asyncio.sleep(0.01)
        confirmed = ctl.state.confirmed_w
        stop.set()
        await task
        await ctl.close()
        return confirmed

    assert asyncio.run(scenario()) == 0


def test_reconnect_exhausted():
    ctl, link, _ = make(reconnect={"max_attempts": 2})

    async def scenario():
        await ctl.start()
        link.open_errors = [FtmsSetupError("nope"), FtmsSetupError("nope")]
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        link.on_link_lost("connection lost")
        with pytest.raises(ReconnectExhausted):
            await task
        await ctl.close()

    asyncio.run(scenario())
    assert link.opens == 3


def test_reconnect_disabled():
    ctl, link, _ = make(reconnect={"enabled": False})

    async def scenario():
        await ctl.start()
        task = asyncio.create_task(ctl.run(asyncio.Event()))
        link.on_link_lost("connection lost")
        with pytest.raises(ReconnectExhausted, match="disabled"):
            await task
        await ctl.close()

    asyncio.run(scenario())


def test_stop_during_reconnect_returns():
    ctl, link, _ = make(reconnect={"backoff_s": [5]})

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        link.on_link_lost("connection lost")
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 1.0)
        await ctl.close()

    asyncio.run(scenario())
    assert link.opens == 1


def test_sender_crash_surfaces_from_run():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [RuntimeError("boom")]
        task = asyncio.create_task(ctl.run(asyncio.Event()))
        ctl.request_target(100)
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(task, 1.0)
        await ctl.close("internal error")

    asyncio.run(scenario())
    assert watts(link)[-1] == 0


def test_close_cancels_inflight_and_spins_down():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [asyncio.get_running_loop().create_future()]  # never resolves
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        await asyncio.wait_for(ctl.close(), 1.0)

    asyncio.run(scenario())
    assert watts(link) == [100, 0]
    assert link.closes == 1
    assert ctl.state.link == "closed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_controller.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms.controller'`.

- [ ] **Step 3: Write the implementation**

`src/wahoo_lsl/ftms/controller.py`:

```python
"""FTMS controller (spec §7.4–7.7): target state, rate-limited sender, spin-down, reconnect.

All methods run on the event loop; link callbacks also arrive there, so no locks are needed.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import clock
from ..ble import BleSetupError, ReconnectExhausted, ReconnectPolicy, _sleep_unless, _unless_stopped
from . import protocol as p
from .config import FtmsConfig
from .link import CommandRejected, LinkError

log = logging.getLogger(__name__)

SPIN_DOWN_TIMEOUT_S = 2.0
MAX_LINK_FAILURES = 3  # consecutive LinkErrors from the sender -> treat the link as lost
MAX_REJECTIONS = 3  # rejected sends of one requested target before giving up on it


class LinkDown(Exception):
    """A target was requested while the BLE link is not ready."""


@dataclass
class State:
    link: str  # "connecting" | "ready" | "reconnecting" | "closed"
    link_epoch: int
    requested_w: int
    confirmed_w: int | None
    confirmed_t_lsl: float | None
    power_w: int | None
    power_t_lsl: float | None
    last_error: str | None
    last_error_t_lsl: float | None


class Link(Protocol):
    power_range: p.PowerRange
    on_bike_data: Callable[[p.BikeData, float], None]
    on_status: Callable[[p.StatusEvent, float], None]
    on_link_lost: Callable[[str], None]

    async def open(self) -> None: ...

    async def command(self, payload: bytes) -> p.CpResponse: ...

    async def close(self) -> None: ...


class Controller:
    def __init__(
        self,
        cfg: FtmsConfig,
        link: Link,
        *,
        now: Callable[[], float] = clock.now_lsl,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._link = link
        self._now = now
        self._sleep = sleep
        self._min_interval = 1.0 / cfg.ftms.max_rate_hz
        self.state = State("connecting", 0, 0, None, None, None, None, None, None)
        self._wake = asyncio.Event()
        self._lost = asyncio.Event()
        self._sender_task: asyncio.Task | None = None
        self._last_send = -math.inf
        self._link_failures = 0
        self._rejections = 0
        self._requests_since_send = 0
        link.on_bike_data = self._on_bike_data
        link.on_status = self._on_status
        link.on_link_lost = self._on_link_lost

    @property
    def pending(self) -> bool:
        return self.state.link == "ready" and self.state.requested_w != self.state.confirmed_w

    # ---- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """First connection (spec §7.3). Raises BleSetupError; starts the sender on success."""
        await self._link.open()
        self._mark_ready()
        self._sender_task = asyncio.create_task(self._sender(), name="ftms-sender")

    async def run(self, stop: asyncio.Event) -> None:
        """Supervise until ``stop`` is set, reconnecting on link loss.

        Raises ReconnectExhausted, or re-raises an unexpected exception from the sender.
        """
        assert self._sender_task is not None, "start() must succeed before run()"
        policy = ReconnectPolicy(self._cfg.reconnect.backoff_s, self._cfg.reconnect.max_attempts)
        while True:
            waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._lost.wait())]
            try:
                await asyncio.wait([*waits, self._sender_task], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
            if self._sender_task.done():
                self._sender_task.result()  # re-raises the sender's exception
                raise RuntimeError("FTMS sender stopped unexpectedly")
            if stop.is_set():
                return
            await self._reconnect(policy, stop)

    async def close(self, reason: str = "shutdown") -> None:
        """Stop the sender, spin down to 0 W, release the link. Bounded; never raises."""
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
        await self.spin_down(reason)
        self.state.link = "closed"
        try:
            await self._link.close()
        except Exception as e:
            log.warning("Closing the FTMS link failed: %s", e)

    async def _reconnect(self, policy: ReconnectPolicy, stop: asyncio.Event) -> None:
        if not self._cfg.reconnect.enabled:
            raise ReconnectExhausted("FTMS link lost and reconnect is disabled")
        await self._link.close()  # release the dead client
        failures = 0
        while True:
            delay = policy.delay(failures)
            if await _sleep_unless(stop, delay):
                return
            attempt = failures + 1
            log.warning("FTMS reconnect attempt %d (after %.1fs)", attempt, delay)
            try:
                await _unless_stopped(self._link.open(), stop)
            except BleSetupError as e:
                failures += 1
                log.warning("FTMS reconnect attempt %d failed: %s", attempt, e)
                if policy.exhausted(failures):
                    raise ReconnectExhausted(f"gave up after {failures} failed FTMS reconnect attempt(s)") from e
                continue
            if stop.is_set():
                return
            self._mark_ready()
            log.warning(
                "FTMS reconnected; target is 0 W until the client sends a new one (link_epoch %d)",
                self.state.link_epoch,
            )
            return

    # ---- client-facing ---------------------------------------------------------

    def request_target(self, watts: int) -> tuple[int, bool]:
        """Store a new target (clamped) and wake the sender. No BLE I/O; returns at once."""
        if self.state.link != "ready":
            raise LinkDown(f"BLE link is {self.state.link}")
        accepted = self.clamp(watts)
        self.state.requested_w = accepted
        self._rejections = 0
        self._requests_since_send += 1
        self._wake.set()
        return accepted, accepted != watts

    def clamp(self, watts: int) -> int:
        r = self._link.power_range
        lo = max(0, r.min_w)
        hi = r.max_w if self._cfg.ftms.max_watts is None else min(r.max_w, self._cfg.ftms.max_watts)
        hi = max(hi, lo)
        step = max(1, r.step_w)
        w = min(max(watts, lo), hi)
        w = lo + (w - lo + step // 2) // step * step
        return w if w <= hi else w - step

    async def spin_down(self, reason: str) -> bool:
        """Send 0 W now (bypassing the rate limit). Returns success; never raises."""
        self.state.requested_w = 0
        if self.state.link != "ready":
            level = logging.WARNING if self.state.link == "reconnecting" else logging.DEBUG
            log.log(level, "spin-down (%s): link is %s, cannot send 0 W", reason, self.state.link)
            return False
        epoch = self.state.link_epoch
        try:
            await asyncio.wait_for(self._link.command(p.set_target_power(0)), SPIN_DOWN_TIMEOUT_S)
        except Exception as e:
            self._set_error(f"spin-down ({reason}) failed: {e or type(e).__name__}")
            return False
        self._confirm(0, epoch)
        log.info("spin-down (%s): 0 W confirmed", reason)
        return True

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        age = None if s.power_t_lsl is None else round(self._now() - s.power_t_lsl, 3)
        return {
            "link": s.link,
            "link_epoch": s.link_epoch,
            "target": {
                "requested_w": s.requested_w,
                "confirmed_w": s.confirmed_w,
                "pending": self.pending,
                "t_lsl": s.confirmed_t_lsl,
            },
            "power": {"watts": s.power_w, "t_lsl": s.power_t_lsl, "age_s": age},
            "last_error": s.last_error,
            "last_error_t_lsl": s.last_error_t_lsl,
        }

    # ---- sender ------------------------------------------------------------------

    async def _sender(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self.pending:
                continue
            wait = self._last_send + self._min_interval - self._now()
            if wait > 0:
                await self._sleep(wait)
            if not self.pending:
                continue  # link lost, or the latest request equals what is confirmed
            target = self.state.requested_w  # read after the wait: latest wins
            if self._requests_since_send > 1:
                log.debug("coalesced %d target(s); sending %d W", self._requests_since_send - 1, target)
            self._requests_since_send = 0
            self._last_send = self._now()
            await self._send(target)

    async def _send(self, target: int) -> None:
        epoch = self.state.link_epoch
        try:
            await self._link.command(p.set_target_power(target))
        except CommandRejected as e:
            self._rejections += 1
            self._set_error(f"set {target} W rejected: {e}")
            if self._rejections < MAX_REJECTIONS:
                self._wake.set()
            else:
                log.warning("Giving up on %d W after %d rejections; waiting for a new target", target, self._rejections)
            return
        except LinkError as e:
            self._link_failures += 1
            self._set_error(f"set {target} W failed: {e}")
            if self._link_failures >= MAX_LINK_FAILURES:
                self._on_link_lost(f"{self._link_failures} consecutive control-point failures")
            else:
                self._wake.set()
            return
        self._link_failures = 0
        self._confirm(target, epoch)
        log.debug("target %d W confirmed", target)

    # ---- state helpers and link callbacks ----------------------------------------

    def _confirm(self, watts: int, epoch: int) -> None:
        # A success from before a reconnect must not mark a stale value confirmed.
        if self.state.link == "ready" and self.state.link_epoch == epoch:
            self.state.confirmed_w = watts
            self.state.confirmed_t_lsl = self._now()

    def _mark_ready(self) -> None:
        s = self.state
        s.link = "ready"
        s.link_epoch += 1
        s.requested_w = 0
        s.confirmed_w = 0
        s.confirmed_t_lsl = self._now()
        self._link_failures = 0
        self._rejections = 0
        self._requests_since_send = 0
        self._lost.clear()
        log.info("FTMS control ready at 0 W (link_epoch %d)", s.link_epoch)

    def _set_error(self, message: str) -> None:
        self.state.last_error = message
        self.state.last_error_t_lsl = self._now()
        log.warning("%s", message)

    def _on_link_lost(self, reason: str) -> None:
        if self.state.link != "ready":
            return  # already reconnecting or closed
        s = self.state
        s.link = "reconnecting"
        s.requested_w = 0
        s.confirmed_w = None
        s.confirmed_t_lsl = None
        self._set_error(f"link lost: {reason}; target reset to 0 W")
        self._lost.set()

    def _on_status(self, event: p.StatusEvent, t_lsl: float) -> None:
        if event.opcode == p.STATUS_TARGET_POWER_CHANGED and event.target_power_w is not None:
            if self.state.link == "ready":
                self.state.confirmed_w = event.target_power_w
                self.state.confirmed_t_lsl = t_lsl
        elif event.opcode != p.STATUS_CONTROL_PERMISSION_LOST:
            log.info("Trainer status: %s", event.name)

    def _on_bike_data(self, data: p.BikeData, t_lsl: float) -> None:
        if data.power_w is not None:
            self.state.power_w = data.power_w
            self.state.power_t_lsl = t_lsl
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_controller.py -q -W error`
Expected: PASS. Then `conda run -n wahoo python -m pytest -q -W error` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/ftms/controller.py tests/test_ftms_controller.py
git commit -m "feat(ftms): controller with latest-wins rate limiting, spin-down and reconnect

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Localhost TCP server

**Files:**
- Create: `src/wahoo_lsl/ftms/server.py`
- Test: `tests/test_ftms_server.py`

**Interfaces:**
- Consumes: controller duck type — `request_target(watts) -> (int, bool)` (raises `LinkDown`), `snapshot() -> dict`, `async spin_down(reason) -> bool` (Task 5).
- Produces: `MAX_LINE_BYTES = 4096`; `error_reply(code, detail) -> dict`; `ControlServer(controller, host, port)` with `async bind()` (binds, raises `OSError`, does not accept), `async start_serving()`, `port` (actual bound port), `async close()`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ftms_server.py`:

```python
import asyncio
import json

import pytest

from wahoo_lsl.ftms.controller import LinkDown
from wahoo_lsl.ftms.server import MAX_LINE_BYTES, ControlServer

SNAPSHOT = {
    "link": "ready",
    "link_epoch": 1,
    "target": {"requested_w": 0, "confirmed_w": 0, "pending": False, "t_lsl": 1.0},
    "power": {"watts": None, "t_lsl": None, "age_s": None},
    "last_error": None,
    "last_error_t_lsl": None,
}


class StubController:
    def __init__(self) -> None:
        self.requests: list[int] = []
        self.spin_downs: list[str] = []
        self.link_down = False

    def request_target(self, watts: int):
        if self.link_down:
            raise LinkDown("BLE link is reconnecting")
        self.requests.append(watts)
        accepted = min(watts, 1000)
        return accepted, accepted != watts

    def snapshot(self):
        return SNAPSHOT

    async def spin_down(self, reason: str) -> bool:
        self.spin_downs.append(reason)
        return True


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


async def serving(ctl):
    server = ControlServer(ctl, "127.0.0.1", 0)
    await server.bind()
    await server.start_serving()
    return server


async def ask(reader, writer, request):
    raw = request if isinstance(request, bytes) else json.dumps(request).encode()
    writer.write(raw + b"\n")
    await writer.drain()
    return json.loads(await reader.readline())


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 5.0))


def test_set_and_read_round_trip():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await ask(reader, writer, {"cmd": "set", "watts": 150}) == {"ok": True, "target_w": 150, "clamped": False}
        assert await ask(reader, writer, {"cmd": "set", "watts": 1200}) == {"ok": True, "target_w": 1000, "clamped": True}
        assert await ask(reader, writer, {"cmd": "read"}) == {"ok": True, **SNAPSHOT}
        writer.close()
        await server.close()

    run(scenario)
    assert ctl.requests == [150, 1200]


@pytest.mark.parametrize(
    "request_bytes, code",
    [
        (b"not json", "bad_request"),
        (b"[1, 2]", "bad_request"),
        (b'{"cmd": "dance"}', "bad_request"),
        (b'{"cmd": "set"}', "invalid_watts"),
        (b'{"cmd": "set", "watts": -1}', "invalid_watts"),
        (b'{"cmd": "set", "watts": 1.5}', "invalid_watts"),
        (b'{"cmd": "set", "watts": true}', "invalid_watts"),
        (b'{"cmd": "set", "watts": "100"}', "invalid_watts"),
    ],
)
def test_errors_keep_connection_usable(request_bytes, code):
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, request_bytes)
        assert reply["ok"] is False and reply["error"] == code and reply["detail"]
        assert (await ask(reader, writer, {"cmd": "read"}))["ok"] is True
        writer.close()
        await server.close()

    run(scenario)
    assert ctl.requests == []


def test_link_down_reply():
    ctl = StubController()
    ctl.link_down = True

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, {"cmd": "set", "watts": 100})
        assert reply == {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}
        writer.close()
        await server.close()

    run(scenario)


def test_blank_lines_ignored():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"\n  \n")
        assert (await ask(reader, writer, {"cmd": "read"}))["ok"] is True
        writer.close()
        await server.close()

    run(scenario)


def test_too_long_line_rejected_and_closed():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert reply["error"] == "bad_request"
        assert await reader.read() == b""
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        writer.close()
        await server.close()

    run(scenario)


def test_second_client_busy_first_unaffected():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        assert (await ask(r1, w1, {"cmd": "read"}))["ok"]
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
        reply = json.loads(await r2.readline())
        assert reply["error"] == "busy"
        assert await r2.read() == b""
        assert (await ask(r1, w1, {"cmd": "set", "watts": 100}))["ok"]
        assert ctl.spin_downs == []  # rejecting the second client must not spin down
        w2.close()
        w1.close()
        await server.close()

    run(scenario)


def test_client_eof_triggers_spin_down_and_next_client_accepted():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(r1, w1, {"cmd": "set", "watts": 100})
        w1.close()
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
        assert (await ask(r2, w2, {"cmd": "read"}))["ok"]
        w2.close()
        await server.close()

    run(scenario)


def test_client_reset_triggers_spin_down():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(r1, w1, {"cmd": "set", "watts": 100})
        w1.transport.abort()  # drop without a clean shutdown, like a killed process
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        await server.close()

    run(scenario)


def test_bind_on_used_port_raises_oserror():
    ctl = StubController()

    async def scenario():
        first = await serving(ctl)
        second = ControlServer(ctl, "127.0.0.1", first.port)
        with pytest.raises(OSError):
            await second.bind()
        await first.close()

    run(scenario)


def test_close_with_active_client_does_not_spin_down_from_handler():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(reader, writer, {"cmd": "read"})
        await server.close()
        assert await reader.read() == b""
        writer.close()

    run(scenario)
    assert ctl.spin_downs == []  # shutdown spins down via Controller.close, not the handler
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_server.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms.server'`.

- [ ] **Step 3: Write the implementation**

`src/wahoo_lsl/ftms/server.py`:

```python
"""Localhost JSON-lines TCP endpoint (spec §8). One client at a time."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Protocol

from .controller import LinkDown

log = logging.getLogger(__name__)

MAX_LINE_BYTES = 4096
CLOSE_TIMEOUT_S = 5.0


class ControllerApi(Protocol):
    def request_target(self, watts: int) -> tuple[int, bool]: ...

    def snapshot(self) -> dict[str, Any]: ...

    async def spin_down(self, reason: str) -> bool: ...


def error_reply(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}


class ControlServer:
    def __init__(self, controller: ControllerApi, host: str, port: int) -> None:
        self._controller = controller
        self._host = host
        self._port = port
        self._server: asyncio.base_events.Server | None = None
        self._active: asyncio.StreamWriter | None = None
        self._closing = False

    async def bind(self) -> None:
        """Bind the port now (fails fast with OSError) but accept no clients yet."""
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, limit=MAX_LINE_BYTES, start_serving=False
        )

    async def start_serving(self) -> None:
        assert self._server is not None, "bind() first"
        await self._server.start_serving()
        log.info("Accepting client commands on %s:%d", self._host, self.port)

    @property
    def port(self) -> int:
        assert self._server is not None, "bind() first"
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        """Stop accepting, drop the active client (no handler spin-down: shutdown does it)."""
        self._closing = True
        if self._server is None:
            return
        self._server.close()
        if self._active is not None:
            self._active.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("TCP server did not close within %.1fs; continuing", CLOSE_TIMEOUT_S)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self._active is not None:
            log.warning("Rejecting second client %s: busy", peer)
            with suppress(ConnectionError):
                await _send(writer, error_reply("busy", "another client is connected"))
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            return
        self._active = writer
        log.info("Client connected: %s", peer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # line longer than the stream limit
                    await _send(writer, error_reply("bad_request", f"request line longer than {MAX_LINE_BYTES} bytes"))
                    break
                if not line:
                    break  # EOF
                if not line.strip():
                    continue
                await _send(writer, self._dispatch(line))
        except ConnectionError as e:
            log.info("Client %s connection error: %s", peer, e)
        except Exception:
            log.exception("Client handler error")
        finally:
            self._active = None
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            log.info("Client disconnected: %s", peer)
            if not self._closing:
                await self._controller.spin_down("client disconnected")

    def _dispatch(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_reply("bad_request", "request is not valid JSON")
        if not isinstance(request, dict):
            return error_reply("bad_request", "request must be a JSON object")
        cmd = request.get("cmd")
        if cmd == "read":
            return {"ok": True, **self._controller.snapshot()}
        if cmd == "set":
            watts = request.get("watts")
            if not isinstance(watts, int) or isinstance(watts, bool) or watts < 0:
                return error_reply("invalid_watts", f"watts must be a non-negative integer, got {watts!r}")
            try:
                accepted, clamped = self._controller.request_target(watts)
            except LinkDown as e:
                return error_reply("link_down", str(e))
            return {"ok": True, "target_w": accepted, "clamped": clamped}
        return error_reply("bad_request", f"unknown cmd {cmd!r}; expected 'set' or 'read'")


async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(json.dumps(obj).encode("utf-8") + b"\n")
    await writer.drain()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_server.py -q -W error`
Expected: PASS. Then `conda run -n wahoo python -m pytest -q -W error` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/wahoo_lsl/ftms/server.py tests/test_ftms_server.py
git commit -m "feat(ftms): localhost JSON-lines control server with spin-down on disconnect

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Python client API (stdlib only)

**Files:**
- Create: `src/wahoo_lsl/ftms/client.py`, `tests/ftms_stub.py`
- Test: `tests/test_ftms_client.py`

**Interfaces:**
- Consumes: wire protocol from spec §8 (Task 6); `wahoo_lsl.ftms.{DEFAULT_HOST, DEFAULT_PORT}`.
- Produces: `KickrError(code, detail)`, `TargetState(requested_w, confirmed_w, pending, t_lsl)`, `PowerReading(watts, t_lsl, age_s)`, `Status(link, link_epoch, target, power, last_error, last_error_t_lsl)`, `KickrClient(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout_s=2.0)` with `set_target_power(watts) -> int`, `read() -> Status`, `read_target_power() -> TargetState`, `read_power() -> PowerReading`, `close()`, context manager.
- Produces (tests): `tests/ftms_stub.py` with `StubService(reply)` — a threaded JSON-lines server for client and CLI tests; `snapshot(...)` helper.

- [ ] **Step 1: Write the stub service**

`tests/ftms_stub.py`:

```python
"""Threaded JSON-lines stand-in for the FTMS service (client and CLI tests)."""
from __future__ import annotations

import json
import socketserver
import threading


def snapshot(requested=0, confirmed=0, pending=False, watts=None, link="ready", epoch=1, last_error=None):
    return {
        "ok": True,
        "link": link,
        "link_epoch": epoch,
        "target": {"requested_w": requested, "confirmed_w": confirmed, "pending": pending, "t_lsl": 10.0},
        "power": {"watts": watts, "t_lsl": None if watts is None else 11.0, "age_s": None if watts is None else 0.25},
        "last_error": last_error,
        "last_error_t_lsl": None if last_error is None else 12.0,
    }


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for line in self.rfile:
            request = json.loads(line)
            self.server.requests.append(request)
            reply = self.server.reply(request)
            if reply is None:
                return  # close the connection without replying
            self.wfile.write((json.dumps(reply) + "\n").encode())
        # falls through on client EOF
        self.server.disconnects += 1


class StubService:
    """``reply(request) -> dict | None`` decides each answer (None closes the connection)."""

    def __init__(self, reply) -> None:
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.reply = reply
        self._server.requests = []
        self._server.disconnects = 0
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def requests(self) -> list:
        return self._server.requests

    @property
    def disconnects(self) -> int:
        return self._server.disconnects

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "StubService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_ftms_client.py`:

```python
import socket
import subprocess
import sys
import time

import pytest
from ftms_stub import StubService, snapshot

from wahoo_lsl.ftms.client import KickrClient, KickrError, PowerReading, Status, TargetState


def wait_for(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def default_reply(request):
    if request["cmd"] == "set":
        return {"ok": True, "target_w": request["watts"], "clamped": False}
    return snapshot(requested=150, confirmed=140, pending=True, watts=143)


def test_set_and_reads():
    with StubService(default_reply) as svc, KickrClient(port=svc.port) as client:
        assert client.set_target_power(150) == 150
        status = client.read()
        assert status == Status(
            link="ready",
            link_epoch=1,
            target=TargetState(150, 140, True, 10.0),
            power=PowerReading(143, 11.0, 0.25),
            last_error=None,
            last_error_t_lsl=None,
        )
        assert client.read_target_power() == TargetState(150, 140, True, 10.0)
        assert client.read_power() == PowerReading(143, 11.0, 0.25)
        assert svc.requests[0] == {"cmd": "set", "watts": 150}


def test_clamped_value_returned():
    with StubService(lambda r: {"ok": True, "target_w": 400, "clamped": True}) as svc:
        with KickrClient(port=svc.port) as client:
            assert client.set_target_power(450) == 400


def test_error_reply_raises_kickr_error():
    with StubService(lambda r: {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}) as svc:
        with KickrClient(port=svc.port) as client:
            with pytest.raises(KickrError) as info:
                client.set_target_power(100)
    assert (info.value.code, info.value.detail) == ("link_down", "BLE link is reconnecting")


def test_close_disconnects_and_is_idempotent():
    with StubService(default_reply) as svc:
        client = KickrClient(port=svc.port)
        client.read()
        client.close()
        client.close()
        wait_for(lambda: svc.disconnects == 1)


def test_service_closing_connection_raises_connection_error():
    with StubService(lambda r: None) as svc, KickrClient(port=svc.port) as client:
        with pytest.raises(ConnectionError):
            client.read()


def test_connection_refused_is_oserror():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(OSError):
        KickrClient(port=port)


def test_client_module_imports_only_stdlib():
    code = (
        "import sys, wahoo_lsl.ftms.client; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in {'bleak', 'pylsl'}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_client.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms.client'`.

- [ ] **Step 4: Write the implementation**

`src/wahoo_lsl/ftms/client.py`:

```python
"""Python client for the FTMS control service (spec §9.1). Standard library only.

Each call is one localhost round trip (well under a millisecond); BLE latency is hidden by
the service. Not thread-safe: use one KickrClient per thread. Closing the client (or the
process exiting) makes the service spin the trainer down to 0 W.
"""
from __future__ import annotations

import json
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
    requested_w: int
    confirmed_w: int | None
    pending: bool
    t_lsl: float | None  # pylsl.local_clock() time of the last confirmation


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


class KickrClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout_s: float = 2.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rb")
        self._closed = False

    def set_target_power(self, watts: int) -> int:
        """Request a target; returns the accepted (possibly clamped) value immediately."""
        return int(self._request({"cmd": "set", "watts": watts})["target_w"])

    def read(self) -> Status:
        r = self._request({"cmd": "read"})
        t, pw = r["target"], r["power"]
        return Status(
            link=r["link"],
            link_epoch=r["link_epoch"],
            target=TargetState(t["requested_w"], t["confirmed_w"], t["pending"], t["t_lsl"]),
            power=PowerReading(pw["watts"], pw["t_lsl"], pw["age_s"]),
            last_error=r.get("last_error"),
            last_error_t_lsl=r.get("last_error_t_lsl"),
        )

    def read_target_power(self) -> TargetState:
        return self.read().target

    def read_power(self) -> PowerReading:
        return self.read().power

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._file.close()
        self._sock.close()

    def __enter__(self) -> "KickrClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, obj: dict[str, Any]) -> dict[str, Any]:
        self._sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        line = self._file.readline()
        if not line:
            raise ConnectionError("the FTMS service closed the connection")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise KickrError(reply.get("error", "unknown"), reply.get("detail", ""))
        return reply
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_client.py -q -W error`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/wahoo_lsl/ftms/client.py tests/ftms_stub.py tests/test_ftms_client.py
git commit -m "feat(ftms): stdlib-only KickrClient API

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: `wahoo-ftms` CLI, entry point, README, end-to-end test

**Files:**
- Create: `src/wahoo_lsl/ftms/cli.py`
- Modify: `pyproject.toml` (console script), `README.md` (new section)
- Test: `tests/test_ftms_cli.py`

**Interfaces:**
- Consumes: everything above; `wahoo_lsl.cli.setup_logging(debug, diag_log)`; `wahoo_lsl.ble.{BleSetupError, ReconnectExhausted, _unless_stopped}`; `tests/ftms_fakes.FakeFtmsBackend`; `tests/ftms_stub.{StubService, snapshot}`.
- Produces: `build_parser()`, `format_status(status) -> str`, `main(argv=None, *, backend=None) -> int`, `serve(config_path, *, debug=False, diag_log=None, backend=None) -> int`, `async _serve(cfg, backend=None, stop=None) -> int`, `run_client_command(args) -> int`; exit-code constants.

- [ ] **Step 1: Write the failing tests**

`tests/test_ftms_cli.py`:

```python
import asyncio
import json
import socket
from importlib.metadata import entry_points

import pytest
from ftms_fakes import FakeFtmsBackend
from ftms_stub import StubService, snapshot

from wahoo_lsl.ftms.cli import _serve, build_parser, format_status, main
from wahoo_lsl.ftms.client import PowerReading, Status, TargetState
from wahoo_lsl.ftms.config import parse_ftms_config


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_cfg(tmp_path, port, **extra):
    path = tmp_path / "ftms.json"
    path.write_text(
        json.dumps({"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01]}, "ftms": {"port": port, **extra}}),
        encoding="utf-8",
    )
    return path


def test_console_script_registered():
    names = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert names.get("wahoo-ftms") == "wahoo_lsl.ftms.cli:main"


def test_parser():
    args = build_parser().parse_args(["set", "150", "--hold", "2"])
    assert (args.command, args.watts, args.hold, args.timeout, args.port) == ("set", 150, 2.0, 5.0, 47610)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["set", "abc"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve"])  # --config required


def test_format_status():
    st = Status("ready", 2, TargetState(150, 100, True, 1.0), PowerReading(143, 2.0, 0.42), "boom", 3.0)
    assert format_status(st) == (
        "link=ready epoch=2 target=150 W (confirmed 100 W, pending) power=143 W (age 0.4 s) last_error=boom"
    )
    st2 = Status("reconnecting", 1, TargetState(0, None, False, None), PowerReading(None, None, None), None, None)
    assert format_status(st2) == "link=reconnecting epoch=1 target=0 W (confirmed n/a) power=n/a last_error=-"
    st3 = Status("ready", 1, TargetState(0, 0, False, 1.0), PowerReading(None, None, None), None, None)
    assert format_status(st3) == "link=ready epoch=1 target=0 W power=n/a last_error=-"


def test_read(capsys):
    with StubService(lambda r: snapshot(watts=143)) as svc:
        assert main(["read", "--port", str(svc.port)]) == 0
    assert "link=ready" in capsys.readouterr().out


def confirming_service():
    state = {"target": 0}

    def reply(request):
        if request["cmd"] == "set":
            state["target"] = min(request["watts"], 400)
            return {"ok": True, "target_w": state["target"], "clamped": state["target"] != request["watts"]}
        return snapshot(requested=state["target"], confirmed=state["target"])

    return StubService(reply)


def test_set_confirmed(capsys):
    with confirming_service() as svc:
        assert main(["set", "150", "--port", str(svc.port)]) == 0
        assert [r["cmd"] for r in svc.requests] == ["read", "set", "read"]
    out = capsys.readouterr().out
    assert "before:" in out and "after:" in out and "target=150 W" in out


def test_set_clamped_note_and_hold(capsys):
    with confirming_service() as svc:
        assert main(["set", "450", "--port", str(svc.port), "--hold", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "clamped to 400 W" in out
    assert "holding 400 W" in out


def test_set_not_confirmed_times_out(capsys):
    def reply(request):
        if request["cmd"] == "set":
            return {"ok": True, "target_w": request["watts"], "clamped": False}
        return snapshot(requested=150, confirmed=0, pending=True)

    with StubService(reply) as svc:
        assert main(["set", "150", "--port", str(svc.port), "--timeout", "0.2"]) == 3
    assert "not confirmed" in capsys.readouterr().err


def test_set_service_error(capsys):
    def reply(request):
        if request["cmd"] == "set":
            return {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}
        return snapshot(link="reconnecting")

    with StubService(reply) as svc:
        assert main(["set", "150", "--port", str(svc.port)]) == 1
    assert "link_down" in capsys.readouterr().err


def test_unreachable_service(capsys):
    assert main(["read", "--port", str(free_port())]) == 2
    assert "cannot reach" in capsys.readouterr().err


def test_serve_bad_config(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"ftms": {}}), encoding="utf-8")
    assert main(["serve", "-c", str(path)], backend=FakeFtmsBackend()) == 2


def test_serve_port_in_use(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        backend = FakeFtmsBackend()
        assert main(["serve", "-c", str(write_cfg(tmp_path, busy.getsockname()[1]))], backend=backend) == 2
    assert backend.clients == []  # failed before touching BLE


def test_serve_setup_failure(tmp_path):
    backend = FakeFtmsBackend(has_ftms=False)
    assert main(["serve", "-c", str(write_cfg(tmp_path, free_port()))], backend=backend) == 3


def test_end_to_end_set_read_disconnect_shutdown():
    port = free_port()
    cfg = parse_ftms_config({"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01]}, "ftms": {"port": port}})
    backend = FakeFtmsBackend()

    async def until(cond, timeout=3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not cond():
            if loop.time() > deadline:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(0.005)

    async def connect():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while True:
            try:
                return await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.01)

    async def ask(reader, writer, obj):
        writer.write(json.dumps(obj).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(_serve(cfg, backend, stop))
        reader, writer = await connect()
        assert await ask(reader, writer, {"cmd": "set", "watts": 150}) == {"ok": True, "target_w": 150, "clamped": False}

        for _ in range(300):
            if (await ask(reader, writer, {"cmd": "read"}))["target"]["confirmed_w"] == 150:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("150 W never confirmed")
        writer.close()  # client disconnect -> spin-down
        await until(lambda: backend.writes[-1] == b"\x05\x00\x00" and b"\x05\x96\x00" in backend.writes)
        stop.set()
        return await asyncio.wait_for(task, 5.0)

    assert asyncio.run(scenario()) == 0
    writes = backend.writes
    assert writes[:3] == [b"\x00", b"\x07", b"\x05\x00\x00"]
    after_150 = writes[writes.index(b"\x05\x96\x00") + 1 :]
    assert after_150 and all(w == b"\x05\x00\x00" for w in after_150)
    assert backend.clients[0].is_connected is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n wahoo python -m pytest tests/test_ftms_cli.py -q -W error`
Expected: FAIL — `ModuleNotFoundError: No module named 'wahoo_lsl.ftms.cli'`.

- [ ] **Step 3: Write the implementation**

`src/wahoo_lsl/ftms/cli.py`:

```python
"""wahoo-ftms command line (spec §9.2).

`read` and `set` use only the stdlib client; the `serve` path imports BLE code lazily.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import DEFAULT_HOST, DEFAULT_PORT
from .client import KickrClient, KickrError, Status

log = logging.getLogger("wahoo_lsl.ftms")

# serve
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SETUP = 3
EXIT_RECONNECT = 4
# read / set
EXIT_SERVICE_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_NOT_CONFIRMED = 3
EXIT_INTERRUPTED = 130

POLL_INTERVAL_S = 0.1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wahoo-ftms", description="Control a Wahoo KICKR's ERG target over FTMS.")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_p = sub.add_parser("serve", help="connect to the trainer and accept local client commands")
    serve_p.add_argument("-c", "--config", required=True, type=Path, help="path to FTMS JSON config")
    serve_p.add_argument("--debug", action="store_true", help="log every BLE command, response and notification")
    serve_p.add_argument("--diag-log", type=Path, help="also write diagnostic log lines to this file")
    read_p = sub.add_parser("read", help="print the service's current target and power")
    set_p = sub.add_parser("set", help="read, set a target power, wait for confirmation, read back")
    set_p.add_argument("watts", type=int, help="target power in W")
    set_p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for confirmation (default 5)")
    set_p.add_argument(
        "--hold", type=float, default=0.0, help="keep the target for this many seconds before exiting (default 0)"
    )
    for p in (read_p, set_p):
        p.add_argument("--host", default=DEFAULT_HOST, help=f"service host (default {DEFAULT_HOST})")
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"service port (default {DEFAULT_PORT})")
    return parser


def format_status(st: Status) -> str:
    t = st.target
    target = f"{t.requested_w} W"
    if t.confirmed_w != t.requested_w:
        confirmed = "n/a" if t.confirmed_w is None else f"{t.confirmed_w} W"
        target += f" (confirmed {confirmed}{', pending' if t.pending else ''})"
    if st.power.watts is None:
        power = "n/a"
    elif st.power.age_s is None:
        power = f"{st.power.watts} W"
    else:
        power = f"{st.power.watts} W (age {st.power.age_s:.1f} s)"
    return f"link={st.link} epoch={st.link_epoch} target={target} power={power} last_error={st.last_error or '-'}"


def main(argv: Sequence[str] | None = None, *, backend: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve(args.config, debug=args.debug, diag_log=args.diag_log, backend=backend)
    return run_client_command(args)


# ---- client commands -------------------------------------------------------------


def run_client_command(args: argparse.Namespace) -> int:
    try:
        with KickrClient(args.host, args.port) as client:
            if args.command == "read":
                print(format_status(client.read()))
                return EXIT_OK
            return _set(client, args.watts, args.timeout, args.hold)
    except KickrError as e:
        print(f"wahoo-ftms: service error {e.code}: {e.detail}", file=sys.stderr)
        return EXIT_SERVICE_ERROR
    except OSError as e:
        print(f"wahoo-ftms: cannot reach the service at {args.host}:{args.port}: {e}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


def _set(client: KickrClient, watts: int, timeout: float, hold: float) -> int:
    print("before: " + format_status(client.read()))
    accepted = client.set_target_power(watts)
    if accepted != watts:
        print(f"note: {watts} W was clamped to {accepted} W")
    deadline = time.monotonic() + timeout
    while True:
        status = client.read()
        if status.target.confirmed_w == accepted and not status.target.pending:
            break
        if time.monotonic() >= deadline:
            print("after:  " + format_status(status))
            print(f"wahoo-ftms: {accepted} W not confirmed within {timeout:g} s", file=sys.stderr)
            return EXIT_NOT_CONFIRMED
        time.sleep(POLL_INTERVAL_S)
    print("after:  " + format_status(status))
    if hold > 0:
        print(f"holding {accepted} W for {hold:g} s (Ctrl-C to release; the trainer drops to 0 W on exit)")
        try:
            time.sleep(hold)
        except KeyboardInterrupt:
            pass
    return EXIT_OK


# ---- service -------------------------------------------------------------------


def serve(config_path: Path, *, debug: bool = False, diag_log: Path | None = None, backend: Any = None) -> int:
    from ..cli import setup_logging
    from ..config import ConfigError
    from .config import load_ftms_config

    try:
        setup_logging(debug, diag_log)
    except OSError as e:
        print(f"wahoo-ftms: cannot write --diag-log {diag_log}: {e}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load_ftms_config(config_path)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return EXIT_CONFIG
    try:
        return asyncio.run(_serve(cfg, backend))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return EXIT_OK  # Windows Ctrl-C or a second POSIX signal; _serve's finally has already spun down


async def _serve(cfg: Any, backend: Any = None, stop: asyncio.Event | None = None) -> int:
    from ..ble import BleSetupError, ReconnectExhausted, _unless_stopped
    from .controller import Controller
    from .link import FtmsLink
    from .server import ControlServer

    link = FtmsLink(cfg, backend=backend)
    controller = Controller(cfg, link)
    server = ControlServer(controller, cfg.ftms.host, cfg.ftms.port)
    try:
        await server.bind()  # before BLE, so "port in use" fails immediately
    except OSError as e:
        log.error("Cannot listen on %s:%d: %s", cfg.ftms.host, cfg.ftms.port, e)
        return EXIT_CONFIG
    if stop is None:
        stop = asyncio.Event()
        _install_signal_handlers(stop)
    code, reason = EXIT_OK, "shutdown"
    try:
        await _unless_stopped(controller.start(), stop)
        if not stop.is_set():
            await server.start_serving()
            await controller.run(stop)
    except BleSetupError as e:
        log.error("%s", e)
        code = EXIT_SETUP
    except ReconnectExhausted as e:
        log.error("%s", e)
        code = EXIT_RECONNECT
    except Exception:
        log.exception("Unexpected error")
        code, reason = EXIT_ERROR, "internal error"
    finally:
        await server.close()
        await controller.close(reason)
    log.info("Stopped")
    return code


def _install_signal_handlers(stop: asyncio.Event) -> None:
    if sys.platform == "win32":  # add_signal_handler is not implemented on Windows event loops
        return
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set():
            main_task.cancel()  # second signal: stop waiting for a graceful shutdown
        else:
            log.info("Stopping: spinning down to 0 W ...")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register the console script and reinstall**

In `pyproject.toml`, under `[project.scripts]`, add after the `wahoo-lsl` line:

```toml
wahoo-ftms = "wahoo_lsl.ftms.cli:main"
```

Also change the `description` to `"Stream CPS data from and control (FTMS) a Wahoo KICKR over BLE"`.

Run: `conda run -n wahoo python -m pip install -e . -q`

- [ ] **Step 5: Add the README section**

Append to `README.md`:

````markdown
## FTMS power control (`wahoo-ftms`)

A separate service that takes FTMS control of the trainer and holds ERG targets requested by
one local client. It uses its own BLE connection, so it can run on a different machine from
the CPS logger (or alongside it). Design: `docs/superpowers/specs/2026-10-21-kickr-ftms-control-design.md`.

```bash
cp ftms.example.json ftms.json          # edit device name / mac / uuid as for config.json
conda activate wahoo
wahoo-ftms serve -c ftms.json           # connect, take control, ERG at 0 W; Ctrl-C spins down and exits
wahoo-ftms serve -c ftms.json --debug   # log every control-point write/response and notification
wahoo-ftms read                         # one status line
wahoo-ftms set 150 --hold 30            # read, set 150 W, wait for confirmation, read back, hold 30 s
```

The trainer goes back to 0 W whenever the client disconnects (including when `wahoo-ftms set`
exits), on any service error, and on shutdown. After a BLE drop the service reconnects and sets
0 W; `link_epoch` increases and the client must send its target again.

From Python (standard library only; no bleak/pylsl needed in the client's environment):

```python
from wahoo_lsl.ftms.client import KickrClient

with KickrClient() as kickr:            # 127.0.0.1:47610
    kickr.set_target_power(150)         # returns immediately with the accepted value
    print(kickr.read_target_power())    # TargetState(requested_w, confirmed_w, pending, t_lsl)
    print(kickr.read_power())           # PowerReading(watts, t_lsl, age_s)
# leaving the block disconnects -> service spins down to 0 W
```

Wire protocol (newline-delimited JSON on 127.0.0.1:47610): `{"cmd":"set","watts":150}` and
`{"cmd":"read"}`; errors are `{"ok":false,"error":"bad_request|invalid_watts|link_down|busy","detail":...}`.

### Manual hardware checklist (FTMS)

1. `wahoo-ftms serve -c ftms.json --debug`: log shows request_control, start_or_resume and set 0 W each answered with success.
2. `wahoo-ftms read`: `link=ready epoch=1 target=0 W` and power updating about once per second while pedalling.
3. `wahoo-ftms set 100 --hold 10` while pedalling: resistance rises within ~2 s; after the hold the trainer returns to 0 W.
4. Run a Python client that sets 150 W, then kill it (`kill -9`): the trainer returns to 0 W.
5. Switch the trainer off for ~10 s mid-session: warnings, reconnect, `epoch=2`, target 0 W.
6. Open the Wahoo app or Zwift and let it take control: "control permission lost" warning, reconnect, 0 W.
7. Run `wahoo-lsl run -c config.json` at the same time: both connect and work (the KICKR accepts multiple BLE centrals).
8. Ctrl-C the service while holding a target: "spin-down (shutdown): 0 W confirmed", exit 0.
````

- [ ] **Step 6: Run the full suite**

Run: `conda run -n wahoo python -m pytest -q -W error`
Expected: PASS (all tests, including Task 8's).

- [ ] **Step 7: Commit**

```bash
git add src/wahoo_lsl/ftms/cli.py pyproject.toml README.md tests/test_ftms_cli.py
git commit -m "feat(ftms): wahoo-ftms CLI (serve/read/set), docs and end-to-end test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
