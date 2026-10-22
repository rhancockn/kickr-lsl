# CPS LSL Outlet — Design

Date: 2026-10-21
Status: approved in conversation; awaiting written-spec review
Branch: `feat/lsl` (off `main`)

## Purpose

Broadcast the KICKR's Cycling Power Service data as a regular Lab Streaming
Layer stream from `wahoo-lsl run`, alongside the existing JSONL log. Derived
speed and cadence are computed once, in one shared routine, so the log, the
LSL outlet and any later outlet carry identical values. LSL is a core output:
it is on by default, but an LSL failure never stops the JSONL log.

A later iteration adds an irregular outlet for FTMS control events; the
generic helpers in `wahoo_lsl/lsl.py` are written to be reused by it.

## Constraints

- Simple, clear, minimal. No new dependencies (`pylsl` and `bleak` are
  already dependencies).
- Existing code may be refactored where LSL needs it; the logger's tested
  behaviour (JSONL records, exit codes, shutdown) is otherwise unchanged.
- Samples reach LSL through the existing pipeline: never from the BLE
  notify callback, the dispatcher or the log sink.
- Python ≥ 3.10; macOS, Linux, Windows; `wahoo` conda env.
- Tests: `conda run -n wahoo python -m pytest -q -W error`.

## Data flow

```
BLE notify → ingest_q → Dispatcher: parse → derive (CpsDeriver) → Sample(fields, derived)
                                        ├─ log queue  → LogSink   (record gains "derived")
                                        ├─ lsl queue  → LslSink   (drain → push_chunk)
                                        └─ console    → ConsoleSink (--debug)
```

Each sink already has its own queue and thread (`SinkRunner`). The LSL
sink's queue is the data buffer; its thread drains whatever is waiting and
pushes it as one chunk.

## Components

### `wahoo_lsl/derive.py` (new, pure)

Ported from the dashboard's `CpsDeriver` (branch `feat/dashboard`,
`dashboard/metrics.py`), same rules:

- `CpsDeriver(circumference_m: float = 2.105)`; `ValueError` unless finite
  and > 0 (bool rejected).
- `update(t_lsl: float, fields: dict) -> dict` returns
  `{"speed_kmh": float | None, "cadence_rpm": float | None}`.
- Speed from `wheel_revs` (uint32, wraps at 2³²) and `wheel_event_t`
  (1/2048 s, wraps at 65536):
  `speed_kmh = Δrevs × circumference_m / (Δticks / 2048) × 3.6`.
- Cadence from `crank_revs` (wraps at 65536) and `crank_event_t`
  (1/1024 s, wraps at 65536): `rpm = Δrevs / (Δticks / 1024) × 60`.
- New event only when both Δrevs ≠ 0 and Δticks ≠ 0; otherwise the previous
  value is held, and once revs have not advanced for **3 s** of `t_lsl` the
  value becomes 0.0.
- First sample (or first after reset / after a missing field) only primes
  state → None. Missing or non-int field → that metric None and its state
  cleared.
- `reset()` clears both counters.
- Constants exported for metadata: `DEFAULT_CIRCUMFERENCE_M = 2.105`,
  `ZERO_AFTER_S = 3.0`, wrap and tick constants.

The dashboard keeps its own copy for now; whichever of `feat/dashboard` /
`feat/lsl` merges second switches the dashboard to import `derive.py`.

### `cps.py` — channel vocabulary (extended)

A table `FIELD_INFO: dict[str, FieldInfo]` with
`FieldInfo(unit: str, description: str, encoding: dict[str, float] | None)`
for every numeric/encodable output key of `MEASUREMENTS`:

| key | unit | notes |
|---|---|---|
| `power_w` | W | instantaneous power |
| `pedal_power_balance_pct` | % | |
| `pedal_power_balance_ref` | — | encoding `unknown=0, left=1` |
| `accumulated_torque_nm` | N·m | cumulative, wraps at 2048 N·m |
| `torque_source` | — | encoding `wheel=0, crank=1` |
| `wheel_revs` | rev | cumulative uint32 |
| `wheel_event_t` | 1/2048 s | last wheel event time, wraps at 65536 |
| `crank_revs` | rev | cumulative uint16, wraps at 65536 |
| `crank_event_t` | 1/1024 s | last crank event time, wraps at 65536 |
| `max_force_n`, `min_force_n` | N | |
| `max_torque_nm`, `min_torque_nm` | N·m | |
| `max_angle_deg`, `min_angle_deg` | deg | |
| `top_dead_spot_deg`, `bottom_dead_spot_deg` | deg | |
| `accumulated_energy_kj` | kJ | cumulative |
| `offset_compensation` | — | encoding `false=0, true=1` (bool) |

Plus derived: `speed_kmh` (km/h), `cadence_rpm` (rpm). A test asserts every
key in every `MeasurementSpec.keys` has a `FIELD_INFO` entry.

### `pipeline.py` (refactored)

- `Sample` gains `derived: dict = field(default_factory=dict)` (last field,
  so existing constructors keep working).
- `Dispatcher` takes `deriver: CpsDeriver`; for each successfully parsed
  sample sets `derived = deriver.update(t_lsl, fields)`. Parse-error samples
  get `derived = {}` and do not touch the deriver. On a `connected` event the
  deriver is `reset()` (that event always precedes the first notification of
  a connection, including reconnects, so a counter jump across a link drop is
  never read as speed).
- `Sink.handle_batch(items: list[Sample | Event])`: default calls `handle`
  for each item in order. `SinkRunner` blocks on `get()`, then drains with
  `get_nowait()` until the queue is empty or `STOP` is seen, and hands the
  batch to `handle_batch`; items before `STOP` are processed, then the runner
  stops. Per-batch exceptions are handled exactly as per-item ones are today
  (warn once per sink and exception type). `LogSink`/`ConsoleSink` are
  unchanged.
- `Pipeline` / `Dispatcher` constructors gain the `deriver` argument
  (default `CpsDeriver()`).

### `sinks/log_sink.py`

`sample_record` adds `"derived": s.derived` (e.g.
`{"speed_kmh": 31.2, "cadence_rpm": 88.0}`, `{}` on parse errors). Older logs
simply lack the key.

### `ble.py` — Device Information Service

- `DeviceInfo` gains `device_info: dict[str, str]` (default empty).
- In `connect_and_read`, after the CPS reads, if service `0x180A` exists,
  each present characteristic is read with a **2 s** timeout and decoded
  UTF-8 (errors replaced, NULs/whitespace stripped, empty → skipped):
  `0x2A29 manufacturer`, `0x2A24 model_number`, `0x2A25 serial_number`,
  `0x2A27 hardware_revision`, `0x2A26 firmware_revision`,
  `0x2A28 software_revision`. Any failure of a single read (`COMMS_ERRORS`)
  is logged at debug and skipped; a missing service is not an error.
- The `connected` event detail gains `device_info: {...}`.
- `list-features` prints one line per field read (`Model number: …`), so the
  hardware values can be checked before streaming.

### `config.py`

Two optional top-level sections (strict unknown-key checking kept):

```json
"lsl":    {"enabled": true, "nominal_srate": 1.0},
"derive": {"wheel_circumference_m": 2.105}
```

- `lsl.enabled`: bool, default `true`.
- `lsl.nominal_srate`: number ≥ 0, default `1.0`; `0` declares the stream
  irregular (`pylsl.IRREGULAR_RATE`).
- `derive.wheel_circumference_m`: positive number, default `2.105`.
- `Config` gains `lsl: LslConfig` and `derive: DeriveConfig`.

### `wahoo_lsl/lsl.py` (new; generic helpers, reusable for FTMS)

- `Channel(label: str, unit: str, type: str, description: str = "")`.
- `stream_info(name, type, source_id, channels, nominal_srate, fmt="double64") -> pylsl.StreamInfo`
  and `add_acquisition(desc)` / `add_channels(desc, channels)` /
  `add_dict(parent, name, mapping)` (nested `str()`-valued children).
  `desc/acquisition`: `software=wahoo_lsl`, `version=wahoo_lsl.__version__`.
  `desc/channels/channel/{label,unit,type,description}` (XDF convention).
- `sanitize(text) -> str`: keep `[A-Za-z0-9]`.

### `sinks/lsl_sink.py` (new)

`LslSink(cfg: Config, outlet_factory=pylsl.StreamOutlet)`; `name = "lsl"`.

**Identity** (computed on `features`, from the latest `connected` detail):

- `MODEL` = `sanitize(device_info.model_number)` if non-empty; else the
  advertised name with a leading `Wahoo` word and a trailing 4-hex-digit
  word removed, sanitized (`"Wahoo KICKR 58EC"` → `KICKR`); if that is
  empty too, `DEVICE`.
- `id6` = last 6 hex digits of the address with separators removed, lower
  case (`CF46355D-…-9F2DB2F24BFA` → `f24bfa`; `AA:BB:CC:DD:EE:01` → `ddee01`).
- `source_id` = `name` = `f"{MODEL}-CPS-{id6}"`, e.g. `KICKR-CPS-f24bfa`.
- `type` = `"CPS"`. Format `double64` (keeps the uint32 wheel counter exact).
- `nominal_srate` = `cfg.lsl.nominal_srate` (0 → irregular).

macOS caveat (documented): CoreBluetooth UUIDs are per host, so the
`source_id` is stable on one machine but differs between machines; on
Windows/Linux the MAC makes it globally stable.

**Channel layout** (on `features`): for each configured measurement in
config order that is in `features.supported`, each of its keys that has a
`FIELD_INFO` entry, `type="measured"`; then `speed_kmh` if
`wheel_revolutions` is supported and `cadence_rpm` if `crank_revolutions`
is supported, `type="derived"`. Encoded keys put their encoding in the
channel description (`wheel=0, crank=1`).

**Metadata** (`desc`):

- `acquisition`: software, version, `clock=pylsl.local_clock at BLE notify
  entry`, `timestamps=notification arrival time (BLE delivery), not device
  measurement time`, `nominal_srate` as declared.
- `device`: `name`, `address`, `address_kind`, `sensor_location`,
  `features_raw`, `supported` (comma list), and every Device Information
  field read.
- `derivation`: `wheel_circumference_m`, `zero_after_s=3`,
  `method=delta revolutions / delta event time between successive
  notifications`, wheel/crank tick rates and wrap values.

**Outlet lifecycle:**

- Created on a `features` event. If an outlet exists with the same
  `source_id` and channel layout it is kept (BLE reconnect → resolvers see a
  gap, not a drop); otherwise the old outlet is closed (dropped) and a new
  one created, logged at INFO.
- Creation failure: logged once (error, with exception); samples are
  dropped until the next `features` event retries.
- Shutdown (`stop()`): push pending rows, drop the outlet.

**Samples** (`handle_batch`): items are processed in order; rows accumulate
and are pushed with one `push_chunk(rows, timestamps)` per batch, flushing
before any outlet change. Per sample:

- Parse-error sample → skipped (no row).
- Value = `float(fields[key])`, through the encoding if one exists; absent
  key → NaN; derived None → NaN.
- Timestamp = `sample.t_lsl`. Gaps are never filled.
- No outlet → sample dropped (cannot happen in practice: `connected` and
  `features` precede notifications).

### `cli.py`

`run_session` builds `Pipeline(..., deriver=CpsDeriver(cfg.derive.wheel_circumference_m))`
and appends `LslSink(cfg)` when `cfg.lsl.enabled`; logs
`"LSL outlet enabled (nominal %s Hz)"` once.

## Timing and the nominal rate

Measured on today's hardware logs (~12.9k samples): median interval
0.990 s, mean 1.0001–1.0003 s, p5–p95 0.90–1.17 s. Hence:

- Default declared rate 1.0 Hz; configurable, 0 = irregular (for other
  devices that notify per crank event or at 2–4 Hz).
- Every sample carries its real timestamp; no filler samples for gaps
  (disconnects, parse errors).
- The true rate is ≈ 0.9998 Hz: consumers that time by sample index ÷ rate
  drift ≈ 0.7 s/h and misalign after gaps. README: use timestamps.
- pyxdf's default `dejitter_timestamps=True` replaces timestamps of regular
  streams with a linear fit (segmented at gaps > 1 s); use `False` for raw
  arrival times.

## Error handling

| Condition | Behaviour |
|---|---|
| `lsl.enabled: false` | no `LslSink`; logging unchanged |
| Outlet creation fails | error logged once; samples dropped until next `features`; logging continues |
| `push_chunk` raises | warn once per exception type; next batch tried |
| Configured measurement unsupported | no channel (existing warn-once) |
| Parse-error sample | not pushed; logged with `derived: {}` |
| Field absent in one notification | NaN |
| DIS absent / characteristic absent / read fails | skipped, debug log; connect not delayed > 2 s per read |
| Reconnect, same address and features | same outlet |
| Reconnect, different address or features | outlet replaced, INFO log |
| Session end | pending rows pushed, outlet destroyed |
| Same device streamed twice on one host | duplicate `source_id`; documented, not prevented |

## Testing

- `derive`: wrap, zero-Δ hold, 3 s decay to 0, priming, missing fields,
  reset, invalid circumference (ported dashboard cases).
- `cps`: `FIELD_INFO` covers every measurement key.
- Dispatcher: `derived` filled; reset on `connected`; parse error → `{}`.
  Log record contains `derived`.
- `SinkRunner`: queued items arrive as one batch; `STOP` mid-drain processes
  preceding items then stops; default `handle_batch` preserves per-item
  behaviour (existing sink tests pass unchanged).
- `lsl.py`: build a `StreamInfo`, parse `as_xml()`; check source_id, name,
  type, rate, format, channels, acquisition.
- `LslSink` with a fake outlet factory: identity derivation (DIS model,
  name fallback, MAC and UUID addresses); layout from features (unsupported
  dropped, derived conditional); encodings; NaN; parse-error skip; chunk
  rows/timestamps; outlet kept on same reconnect, replaced on change;
  irregular rate; recovery after a creation failure; pending rows pushed at
  stop.
- Loopback integration: real outlet, `resolve_byprop("source_id", …)` with
  a 5 s timeout, inlet `pull_chunk` gets the pushed values; skipped if the
  stream cannot be resolved (multicast blocked).
- BLE: `FakeBackend` serves DIS — all present, service absent, one read
  failing; `connected` detail carries `device_info`; `list-features` prints it.
- Config: `lsl`/`derive` defaults, valid values, invalid types/ranges,
  unknown keys.
- CLI end-to-end with fake backend and fake outlet factory: chunks pushed,
  log has `derived`; `lsl.enabled: false` → no LSL sink.
- Manual (user, on hardware): `wahoo-lsl list-features` shows DIS fields;
  `wahoo-lsl run` plus the README's resolver snippet prints stream info and
  live samples.

## Documentation

README section "LSL stream": identity rule and macOS caveat, channel table,
metadata, config keys, timing notes (use timestamps; pyxdf dejitter), and a
~10-line `pylsl` resolver snippet that prints the info XML and samples.

## Out of scope

- FTMS control-event outlet (future; reuses `lsl.py`).
- Switching the dashboard to `derive.py` (at second merge).
- Detecting duplicate `source_id`s on the network.
