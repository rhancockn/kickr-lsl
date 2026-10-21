# Wahoo KICKR CPS Logger — Design Spec

Date: 2026-10-20
Status: Draft for review

## 1. Purpose

A lightweight, modular, cross-platform (macOS, Windows, Linux) Python application that
connects to the Bluetooth LE Cycling Power Service (CPS) of a Wahoo KICKR V6, subscribes to
Cycling Power Measurement notifications, and timestamps, parses, buffers, and logs every
payload.

The MVP logs to file. A later iteration will stream the same samples over Lab Streaming
Layer (LSL) as an **irregular-rate stream** (`nominal_srate = 0`, one LSL sample per BLE
notification) with minimal timing error and no data loss. The MVP architecture
(producer → shared queue → dispatcher → per-sink queues) is designed so that LSL output is
added as one more sink without changing the BLE or parsing code.

### MVP requirements

1. Connect to the CPS on the configured device.
2. Read and enumerate the available cycling power features (CP Feature characteristic).
3. Read a JSON config listing the measurements to monitor.
4. Subscribe to CP Measurement notifications.
5. Timestamp, parse, buffer, and log each payload. If a configured measurement is not
   available, warn once and continue.
6. Warn on connection drops and other comms errors.
7. Provide a debug mode and a mode that only prints available measurements.

### Non-goals (MVP)

- LSL output (designed for, not implemented).
- Resampling to a fixed rate.
- Control-point writes (calibration, crank length, ERG/resistance control).
- Other services (FTMS, Wahoo proprietary).
- GUI.

## 2. Platform & dependencies

- Python ≥ 3.10, existing `wahoo` conda environment (captured in `environment.yml` + `requirements*.txt`), `src/` layout, console script `wahoo-lsl`.
- Runtime deps: `bleak` (BLE, all three platforms), `pylsl` (clock now, LSL later).
- Dev deps: `pytest`.
- **No OS-specific code paths except where explicitly listed** (section 9). Every OS
  branch uses `sys.platform` and is confined to `config.py` (identifier selection) and
  `cli.py` (signal handling).

## 3. Module layout

```
pyproject.toml
config.example.json
src/wahoo_lsl/
  __init__.py
  __main__.py        # -> cli.main()
  clock.py           # the ONLY place clocks are read
  config.py          # load + validate JSON -> Config dataclasses; platform identifier choice
  cps.py             # pure CPS constants, Feature decode, Measurement parser (no I/O)
  ble.py             # only module importing bleak: scan, connect, read, subscribe, reconnect
  pipeline.py        # RawSample / Sample / Event, Dispatcher thread, Sink base, fan-out
  warn.py            # WarnOnce helper (keyed, thread-safe)
  sinks/
    __init__.py
    log_sink.py      # JSONL data writer
    console_sink.py  # debug printing of parsed samples
    # lsl_sink.py    # future
  cli.py             # argparse, logging setup, wiring, lifecycle
tests/
  test_cps.py
  test_config.py
  test_pipeline.py
  test_ble_reconnect.py
  test_log_sink.py
```

Unit boundaries:

| Module | Depends on | Must not depend on |
|---|---|---|
| `cps.py` | stdlib `struct` | bleak, pylsl, threading, I/O |
| `clock.py` | `pylsl`, `time` | everything else |
| `config.py` | stdlib, `cps` (measurement names) | bleak |
| `pipeline.py` | `cps`, `warn`, `clock` | bleak |
| `sinks/*` | `pipeline` types | bleak |
| `ble.py` | `bleak`, `cps`, `clock`, `pipeline` types | sinks |
| `cli.py` | all | — |

## 4. Clocks

Every timestamp names its clock. No silent fallbacks.

| Field | Clock | Captured where | Use |
|---|---|---|---|
| `t_lsl` | `pylsl.local_clock()` — liblsl monotonic steady clock, float seconds | **First statement** of the notify callback | Sole timing reference: ordering, gaps, future LSL sample timestamp |
| `t_wall` | `time.time_ns()` — system realtime clock, UTC; serialized as ISO-8601 with µs | Second statement of the same callback | Human reference / cross-file alignment only. May jump (NTP). Never used for timing math |
| `wheel_event_t`, `crank_event_t` | Sensor's own clock: 1/2048 s and 1/1024 s ticks, uint16, wraps at 65536 | Inside the payload | Raw device event timing; logged unconverted |
| Diagnostic log line timestamps | Python `logging` (system clock) | stderr / optional diag file | Human reading only |

`clock.py` API:

```python
def now_lsl() -> float: ...       # pylsl.local_clock()
def now_wall_ns() -> int: ...     # time.time_ns()
def wall_iso(ns: int) -> str: ... # "2026-10-20T18:01:02.123456+00:00"
```

At startup (before connecting) the app emits a `clock_sync` event containing `t_lsl` and
`t_wall` read back-to-back, anchoring the two time bases in the data log. Every `Event`
carries both `t_lsl` and `t_wall`.

Known, documented limitation: `t_lsl` is host arrival time. It includes BLE
connection-interval latency (typically 7.5–50 ms) and OS stack latency; it is not the
sensor's measurement time. The sensor event times are logged so downstream analysis can
refine cadence.

## 5. Config

`config.json`:

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

Field rules:

- `device.name` (str, required): case-insensitive substring match against advertised name.
- `device.mac` (str|null): used **only** on Windows/Linux (`sys.platform` in
  `win32`, `linux`). Format `AA:BB:CC:DD:EE:FF`, case-insensitive.
- `device.uuid` (str|null): used **only** on macOS (`sys.platform == "darwin"`).
  CoreBluetooth peripheral UUID.
- Identifier selection (`config.select_identifier(cfg, platform=sys.platform)`):
  returns `("mac", value)`, `("uuid", value)`, or `("name", name)` if the
  platform-relevant field is null. The other platform's field is ignored (logged at DEBUG).
  Any other `sys.platform` value → treated like Linux (`mac`).
- `device.scan_timeout_s` (number > 0, default 10).
- `measurements` (non-empty list of unique names from the vocabulary in section 6.3).
  Unknown names → config error. In the MVP the list drives **availability warnings only**
  (connect-time Feature check and runtime missing-field warn-once); it does **not** filter
  output. Every field parsed from a payload is logged. Output filtering by config is a
  planned later feature.
- `log.path` (str): `{start:...}` is formatted with the session start as a local
  `datetime` (strftime spec). Parent directories are created.
- `log.flush_every_n` (int ≥ 1, default 10): flush file after N lines; always flush on
  events and on stop.
- `reconnect.enabled` (bool, default true); `max_attempts` (int ≥ 0, 0 = unlimited);
  `backoff_s` (non-empty list of positive numbers; attempt *k* waits
  `backoff_s[min(k, len-1)]`).
- `queue_warn_depth` (int ≥ 1, default 100).
- Defaults apply for any omitted optional key. Unknown top-level keys → config error.

For `list-features` and `scan`, only `device` is required.

## 6. CPS protocol details (`cps.py`)

All multi-byte values are little-endian.

### 6.1 UUIDs

- Cycling Power Service: `00001818-0000-1000-8000-00805f9b34fb`
- CP Measurement (notify): `00002a63-0000-1000-8000-00805f9b34fb`
- CP Feature (read): `00002a65-0000-1000-8000-00805f9b34fb`
- Sensor Location (read, optional): `00002a5d-0000-1000-8000-00805f9b34fb`

### 6.2 CP Measurement layout

`flags: uint16`, then `instantaneous_power: sint16` (W, always present), then optional
fields **in this order**, each present iff its flag bit is set:

| Bit | Field | Encoding | Output keys |
|---|---|---|---|
| 0 | Pedal power balance | uint8, unit 1/2 % | `pedal_power_balance_pct` (float) |
| 1 | Balance reference (flag only) | — | `pedal_power_balance_ref`: `"left"` if set else `"unknown"` (only when bit 0 set) |
| 2 | Accumulated torque | uint16, unit 1/32 N·m | `accumulated_torque_nm` (float) |
| 3 | Torque source (flag only) | — | `torque_source`: `"crank"` if set else `"wheel"` (only when bit 2 set) |
| 4 | Wheel revolution data | uint32 cumulative revs, uint16 last event time (1/2048 s) | `wheel_revs` (int), `wheel_event_t` (int ticks) |
| 5 | Crank revolution data | uint16 cumulative revs, uint16 last event time (1/1024 s) | `crank_revs` (int), `crank_event_t` (int ticks) |
| 6 | Extreme force magnitudes | sint16 max, sint16 min (N) | `max_force_n`, `min_force_n` (int) |
| 7 | Extreme torque magnitudes | sint16 max, sint16 min (1/32 N·m) | `max_torque_nm`, `min_torque_nm` (float) |
| 8 | Extreme angles | 3 bytes = 24-bit LE; max = bits 0–11, min = bits 12–23 (deg) | `max_angle_deg`, `min_angle_deg` (int) |
| 9 | Top dead spot angle | uint16 deg | `top_dead_spot_deg` (int) |
| 10 | Bottom dead spot angle | uint16 deg | `bottom_dead_spot_deg` (int) |
| 11 | Accumulated energy | uint16 kJ | `accumulated_energy_kj` (int) |
| 12 | Offset compensation indicator (flag only) | — | `offset_compensation` (bool) — always in `values`; measurement `offset_compensation` is always counted as present |

Bits 13–15 are reserved: ignored.

Parser API:

```python
@dataclass(frozen=True)
class Measurement:
    flags: int
    values: dict[str, object]   # all output keys present in this payload
    present: frozenset[str]     # measurement names present in this payload

class ParseError(ValueError): ...

def parse_measurement(data: bytes) -> Measurement   # raises ParseError on short/invalid payload
```

Truncated payload (fewer bytes than flags demand) → `ParseError` with a message naming the
field that could not be read. Trailing extra bytes → not an error; `values` gets
`"_trailing_bytes": n`.

### 6.3 Measurement vocabulary (config names → output keys, feature bit)

| Name | Output keys | Measurement flag bit(s) | Feature bit |
|---|---|---|---|
| `power` | `power_w` | always | always supported |
| `pedal_power_balance` | `pedal_power_balance_pct`, `pedal_power_balance_ref` | 0 | 0 |
| `accumulated_torque` | `accumulated_torque_nm`, `torque_source` | 2 | 1 |
| `wheel_revolutions` | `wheel_revs`, `wheel_event_t` | 4 | 2 |
| `crank_revolutions` | `crank_revs`, `crank_event_t` | 5 | 3 |
| `extreme_force` | `max_force_n`, `min_force_n` | 6 | 4 (and context bit 16 = 0) |
| `extreme_torque` | `max_torque_nm`, `min_torque_nm` | 7 | 4 (and context bit 16 = 1) |
| `extreme_angles` | `max_angle_deg`, `min_angle_deg` | 8 | 5 |
| `dead_spot_angles` | `top_dead_spot_deg`, `bottom_dead_spot_deg` | 9 and 10 (present if either) | 6 |
| `accumulated_energy` | `accumulated_energy_kj` | 11 | 7 |
| `offset_compensation` | `offset_compensation` | 12 | 8 |

`cps.MEASUREMENTS` is the single source of truth for this table (a dict of name →
spec dataclass) and is what `config.py` validates against.

### 6.4 CP Feature decode

```python
@dataclass(frozen=True)
class Features:
    raw: int                      # uint32
    flags: dict[str, bool | int]  # every named bit below; distributed_system_support is int 0–3
    supported: frozenset[str]     # measurement names from 6.3 (always includes "power")

def parse_features(data: bytes) -> Features   # 1-4+ bytes; short payloads zero-padded (KICKR V6 sends 2); empty -> ParseError
```

Named feature bits (for printing in `list-features`): 0 pedal_power_balance,
1 accumulated_torque, 2 wheel_revolution_data, 3 crank_revolution_data,
4 extreme_magnitudes, 5 extreme_angles, 6 dead_spot_angles, 7 accumulated_energy,
8 offset_compensation_indicator, 9 offset_compensation, 10 content_masking,
11 multiple_sensor_locations, 12 crank_length_adjustment, 13 chain_length_adjustment,
14 chain_weight_adjustment, 15 span_length_adjustment, 16 sensor_measurement_context
(0 = force-based, 1 = torque-based), 17 instantaneous_measurement_direction,
18 factory_calibration_date, 19 enhanced_offset_compensation,
20–21 distributed_system_support (2-bit value: 0 unspecified, 1 not supported,
2 supported, 3 RFU — reported as an int).

### 6.5 Sensor Location

uint8 enum (0 Other, 1 Top of shoe, 2 In shoe, 3 Hip, 4 Front wheel, 5 Left crank,
6 Right crank, 7 Left pedal, 8 Right pedal, 9 Front hub, 10 Rear dropout,
11 Chainstay, 12 Rear wheel, 13 Rear hub, 14 Chest, 15 Spider, 16 Chain ring).
`cps.sensor_location_name(code) -> str` returns `"Unknown(<code>)"` for others.

## 7. Data flow & concurrency

### 7.1 Threads

| Thread | Runs | Owns |
|---|---|---|
| Main thread | `asyncio.run(...)` — bleak client, scan/connect/reconnect, notify callback | `ingest_q` producer side |
| `dispatcher` | `Dispatcher.run()` | `ingest_q` consumer, per-sink queues producer |
| `sink-<name>` (one each) | `Sink` loop | one sink queue consumer, the sink's output |

The asyncio loop runs on the main thread (portable for bleak on all three backends and
required for signal handling). The notify callback is invoked on the loop thread.

### 7.2 Types (`pipeline.py`)

```python
@dataclass(frozen=True, slots=True)
class RawSample:
    seq: int            # session-monotonic, starts at 0, never reset on reconnect
    t_lsl: float
    t_wall_ns: int
    data: bytes

@dataclass(frozen=True, slots=True)
class Event:
    kind: str           # "clock_sync" | "connected" | "disconnected" | "reconnect_attempt"
                        # | "reconnected" | "features" | "stopped"
    t_lsl: float
    t_wall_ns: int
    detail: dict

@dataclass(frozen=True, slots=True)
class Sample:
    seq: int
    t_lsl: float
    t_wall_ns: int
    raw_hex: str
    flags: int | None            # None if parse failed before flags were read
    fields: dict[str, object]    # ALL output keys parsed from the payload (no config filtering in MVP)
    parse_error: str | None

STOP = object()   # sentinel
```

### 7.3 Producer (notify callback in `ble.py`)

```python
def _on_notify(self, _sender, data: bytearray) -> None:
    t_lsl = clock.now_lsl()          # FIRST statement
    t_wall_ns = clock.now_wall_ns()  # SECOND statement
    seq = self._seq; self._seq += 1
    self._ingest_q.put_nowait(RawSample(seq, t_lsl, t_wall_ns, bytes(data)))
```

No parsing, logging, or allocation beyond the above inside the callback. `ingest_q` is an
unbounded `queue.Queue`. Events from `ble.py` (connect/disconnect/etc.) are put on the same
queue so ordering relative to samples is preserved.

### 7.4 Dispatcher

- Blocks on `ingest_q.get()`.
- `RawSample` → `cps.parse_measurement` → build `Sample`:
  - `fields` = `Measurement.values` in full (every parsed key, unfiltered).
  - For each configured, feature-supported measurement absent from `present`:
    `WarnOnce` key `("missing_field", name)` → `WARNING "Measurement '<name>' not present
    in notification (flags=0x....); further occurrences suppressed"`.
  - Configured measurements that were already reported unsupported at connect time are not
    warned again here.
  - On `ParseError`: `Sample(fields={}, flags=<if read>, parse_error=str(e))`;
    `WarnOnce` key `("parse_error", str(e))`; keep a counter; counter reported in the
    `stopped` event detail.
- `Event` → forwarded unchanged.
- Fan-out: put the same immutable object on every sink queue (unbounded `queue.Queue`).
- Backlog check after each item: if `ingest_q.qsize()` or any sink queue size
  > `queue_warn_depth`, emit `WARNING` at most once per 10 s (measured with
  `clock.now_lsl()`), naming the queue and depth.
- `STOP` → forward `STOP` to every sink queue, then return.
- The dispatcher receives the unsupported-measurement set via an `Event("features", ...)`
  whose `detail` contains `supported` and `unavailable` lists — so it learns it in-band,
  in order.

### 7.5 Sinks

```python
class Sink(ABC):
    name: str
    def start(self) -> None: ...                 # open resources (called in sink thread)
    def handle(self, item: Sample | Event) -> None: ...
    def stop(self) -> None: ...                  # flush + close (called in sink thread)

class SinkRunner(threading.Thread):              # one per sink; loops queue.get() until STOP
```

- An exception in `handle` → `ERROR` log with traceback, `WarnOnce`-suppressed per
  exception type afterwards; the sink keeps consuming (no data piles up). An exception in
  `start` → `ERROR`, the runner drains and discards its queue until `STOP` so the
  dispatcher is never blocked; other sinks unaffected.

**LogSink** (`log_sink.py`): one JSON object per line, UTF-8, `\n`.

```json
{"type":"sample","seq":42,"t_lsl":12345.678901,"t_wall":"2026-10-20T18:01:02.123456+00:00","raw":"3400a500...","flags":52,"fields":{"power_w":165,"wheel_revs":1203,"wheel_event_t":40321}}
{"type":"sample","seq":43,"t_lsl":12345.928011,"t_wall":"...","raw":"3400","flags":52,"fields":{},"parse_error":"truncated payload reading wheel_revolutions"}
{"type":"event","kind":"disconnected","t_lsl":12350.1,"t_wall":"...","detail":{"address":"...","reason":"..."}}
```

`t_lsl` is serialized with full float precision (`repr`). `parse_error` key appears only
when non-null. Flush every `flush_every_n` samples, on every event, and on stop.

**ConsoleSink** (`console_sink.py`, `--debug` only): one line per sample to stdout, e.g.
`[  42] t_lsl=12345.678901  power_w=165 wheel_revs=1203 wheel_event_t=40321`; events as
`[event] disconnected {...}`.

**Future LslSink**: `StreamOutlet` with `nominal_srate=0`, `channel_format=double`,
one channel per configured output key (+ `seq`), `push_sample(values, timestamp=t_lsl)`;
a second string marker outlet for `Event`s. Not implemented in MVP; no code stubs.

### 7.6 Shutdown

1. Ctrl-C / SIGTERM sets an `asyncio.Event` `stop_requested`.
2. `ble.py` stops notify (best effort), disconnects (best effort), puts
   `Event("stopped", detail={...})` then `STOP` on `ingest_q`.
3. Main thread joins dispatcher, then all sink runners (timeout 5 s each; WARNING if a
   thread fails to join).
4. Exit code 0.

## 8. BLE layer (`ble.py`)

```python
class CpsClient:
    def __init__(self, cfg: Config, ingest_q: queue.Queue, platform: str = sys.platform): ...
    async def find_device(self) -> BLEDevice            # raises DeviceNotFound / AmbiguousDevice
    async def connect_and_read(self) -> DeviceInfo      # connect, verify CPS, read Feature + Sensor Location
    async def run(self, stop_requested: asyncio.Event) -> None  # subscribe, wait, reconnect loop

async def scan_cps(timeout: float) -> list[ScanResult]   # devices advertising 0x1818 (or name contains "KICKR")
```

- **Discovery**: `config.select_identifier` → for `mac`/`uuid`:
  `BleakScanner.find_device_by_address(value, timeout)`; for `name`:
  `BleakScanner.discover(timeout, return_adv=True)`, filter by name substring; prefer those
  whose advertised service UUIDs include CPS; if >1 candidate remains → `AmbiguousDevice`
  listing name, address, RSSI of each.
- **Connect**: `BleakClient(device, disconnected_callback=self._on_disconnect)`. Verify
  CPS service present (`client.services.get_service(CPS_UUID)`) else `NoCpsService`.
- **DeviceInfo**: `address`, `address_kind` (`"uuid"` on darwin else `"mac"`), `name`,
  `features: Features`, `sensor_location: str | None`.
- **Unavailable measurements**: `configured − features.supported` → one `WARNING`
  per name at connect time, and a `features` Event with `supported`/`unavailable`.
- **Subscribe**: `client.start_notify(CP_MEASUREMENT_UUID, self._on_notify)`.
- **Disconnect callback**: called on the loop thread; emits `WARNING` and an
  `Event("disconnected")`, sets an internal `asyncio.Event` `disconnected`. Ignored if
  shutdown is in progress (no warning on intentional disconnect).
- **Reconnect loop** (if enabled): attempt *k* (0-based) waits `backoff_s[min(k,len-1)]`
  (interruptible by `stop_requested`), emits `Event("reconnect_attempt", {"attempt": k+1})`
  and `WARNING`, re-runs `find_device` + `connect_and_read` + subscribe. On success:
  `Event("reconnected")`, `INFO`. Stops after `max_attempts` failures (if > 0) → `ERROR`,
  begin shutdown, exit code 4. Feature-unavailable warnings are **not** repeated on
  reconnect (WarnOnce keys persist for the session).
- **Errors**: `BleakError`, `asyncio.TimeoutError`, `OSError` during setup → exit 3 with
  message; during run → logged `WARNING` and treated as a disconnect (reconnect path).
- The reconnect policy (which attempt, wait duration, give-up) is a small pure helper
  (`ReconnectPolicy`) so it is unit-testable without bleak.

## 9. Platform-specific behaviour (complete list)

| Where | What | Guard |
|---|---|---|
| `config.select_identifier` | `uuid` on darwin, `mac` elsewhere | `sys.platform` (injectable param for tests) |
| `ble.DeviceInfo.address_kind` | `"uuid"` vs `"mac"` label | same helper |
| `cli` signal handling | `loop.add_signal_handler(SIGINT/SIGTERM)` on non-Windows; on `win32` rely on `KeyboardInterrupt` around `asyncio.run` and translate to the same graceful shutdown | `sys.platform == "win32"` |

No other OS checks are permitted. Linux note (documented in README, not code): BlueZ must
be running and the user needs permission to use it.

## 10. Warnings (`warn.py`)

```python
class WarnOnce:
    def __init__(self, logger: logging.Logger): ...
    def warn(self, key: Hashable, msg: str, *args) -> bool   # True if emitted; thread-safe; counts suppressed
    def counts(self) -> dict[Hashable, int]
```

Single shared instance per session (passed explicitly; no globals).

## 11. CLI (`cli.py`)

```
wahoo-lsl scan [--timeout 10] [--debug]
wahoo-lsl list-features -c CONFIG [--debug]
wahoo-lsl run -c CONFIG [--debug] [--diag-log PATH]
```

- Logging: `logging` to stderr, format
  `%(asctime)s %(levelname)-7s %(name)s: %(message)s`. Default level INFO for `wahoo_lsl`,
  WARNING for `bleak`. `--debug` → DEBUG for both. `--diag-log PATH` additionally writes
  the same diagnostics to a file.
- `run`: load config → emit `clock_sync` → start dispatcher + sink runners (LogSink;
  ConsoleSink if `--debug`) → `CpsClient.run` → shutdown.
- `list-features`: connect, print to stdout:
  ```
  Device:  KICKR CORE 1234   (uuid: 1A2B...)        # or (mac: AA:BB:...)
  Sensor location: Rear hub
  Feature bits (0x0000000C):
    [x] wheel_revolution_data
    [x] crank_revolution_data
    [ ] pedal_power_balance
    ...
  Configured measurements:
    power              available
    accumulated_torque UNAVAILABLE
  ```
  then disconnect, exit 0. Only `device` section required; `measurements` optional (section
  omitted if absent).
- `scan`: print `name  address  rssi  cps_advertised` per device, no config.
- Exit codes: 0 ok, 2 config error, 3 device/connection setup error, 4 reconnect attempts
  exhausted, 1 unexpected error.

## 12. Testing

pytest, no hardware required for the automated suite.

- `test_cps.py`: one hand-built byte fixture per flag bit (0–12) checking exact decoded
  values/units; a combined-flags fixture; extreme-angles bit packing; sint16 negative
  values; truncated payload for each optional field → `ParseError` naming the field;
  trailing bytes; Feature decode incl. bits 16 and 20–21; `supported` mapping incl.
  extreme force vs torque by context bit.
- `test_config.py`: example config loads; each validation rule rejects bad input; defaults
  applied; `select_identifier` for `darwin`, `win32`, `linux`, other, with/without
  `mac`/`uuid`.
- `test_pipeline.py`: feed `RawSample`/`Event`/`STOP` into ingest queue with a recording
  fake sink; assert order preserved, same objects reach every sink, all parsed fields present (no filtering),
  missing-field warn-once (caplog), parse errors forwarded with raw, backlog warning
  rate-limited, faulty sink doesn't block others, clean shutdown/join.
- `test_ble_reconnect.py`: `ReconnectPolicy` waits/give-up; `CpsClient` against a fake
  client/scanner (injected factory) for: unavailable-measurement warnings once,
  disconnect → events + reconnect, no warning on intentional disconnect.
- `test_log_sink.py`: JSONL line shape, `t_wall` format, `parse_error` presence rule,
  flush behaviour, path templating and directory creation (tmp_path).

Manual hardware checklist (README): `scan` finds KICKR; `list-features` output matches
expectation; `run` for 2 min logs samples at the device rate with monotonic `t_lsl` and
contiguous `seq`; power-cycle the KICKR mid-run → disconnect warning, reconnect, events in
log; Ctrl-C → clean exit, file flushed; repeat on Windows and Linux with the same config
(only `mac` filled in vs `uuid`).

## 13. Future: LSL & multi-worker synchronisation notes

- The ingest queue is the single serialisation point; `seq` + `t_lsl` are assigned there,
  so any number of downstream workers see identical, totally ordered data.
- Sinks never share mutable state; each gets the same immutable objects.
- Adding LSL = new `LslSink` registered in `cli.py`. No changes to `ble.py`, `cps.py`,
  `pipeline.py`.
- If multiple BLE devices are added later, each `CpsClient` gets its own `seq` space and a
  `source_id`; they may share one ingest queue (ordering by arrival) or each have their own
  dispatcher. Out of scope for MVP.
