# Wahoo KICKR FTMS Power Control — Design Spec

Date: 2026-10-21
Status: Draft for review
Related: `2026-10-20-wahoo-cps-logger-design.md` (the CPS logger; shares clock, config and BLE helpers)

## 1. Purpose

A lightweight Python service that controls a Wahoo KICKR V6 in ERG mode over the Bluetooth
LE Fitness Machine Service (FTMS) on behalf of a local client program. The client issues
target-power commands and reads power through a localhost TCP endpoint or a small Python
API. Client calls return in well under a millisecond: the service owns all BLE traffic, rate
limiting and verification, and hides BLE latency behind a cached state.

### MVP requirements

1. Connect to FTMS on the configured device over its **own** BLE connection.
2. Take control (FTMS Request Control).
3. Start ERG at 0 W and read back (verify) the 0 W target.
4. Expose a localhost TCP endpoint accepting `set` (target power) and `read` commands.
5. Accept target-power changes, rate limited to `max_rate_hz` (default 2 Hz), latest wins.
6. Send each FTMS Set Target Power and verify it (control-point success response, plus the
   Machine Status "target power changed" echo when the device sends it).
7. Keep the latest measured power (from Indoor Bike Data notifications) cached.
8. Reply to `read` with the cached target and measured power.
9. Spin down to 0 W on client disconnect, on any unexpected exception, and on service shutdown.
10. Debug mode for communications.
11. Python API: `set_target_power`, `read_target_power` (plus `read_power`, `read`).
12. CLI client: read power, set power to an argument value, read back.

### Constraints

- Module of the existing `wahoo_lsl` package; existing `wahoo` conda env; no new runtime deps.
- **FTMS control and CPS logging never share a BLE connection or process.** They may run on
  different machines. On one machine they run as two processes, each with its own
  `BleakClient`. (The KICKR accepts more than one BLE central; on macOS two processes
  connecting to the same peripheral share the OS-level link, which is transparent to both.)
- Cross-platform (macOS, Windows, Linux), same rules as the logger: OS checks only via
  `sys.platform`, only where listed in §10.

### Non-goals (MVP)

- Resistance, simulation (grade/wind) or slope modes; any FTMS op other than those in §5.3.
- Multiple simultaneous clients; remote (non-localhost) clients; authentication.
- Logging control/power data to file or LSL (the CPS logger covers measured data; the
  debug log covers comms).
- Restoring the previous target automatically after a BLE reconnect (deliberately excluded,
  §7.4).

## 2. Decisions taken during design

| Topic | Decision |
|---|---|
| Architecture | One process, one asyncio event loop: BLE link, TCP server and the rate-limited sender are tasks on the same loop. No threads. |
| Transport | TCP on `127.0.0.1`, newline-delimited JSON, one request → one reply, in order. TCP close/reset = client disconnect. |
| Clients | One at a time; a second connection gets `busy` and is closed. |
| Read semantics | `read` returns both the target state (requested, confirmed, pending) and the latest measured power, each with a `t_lsl` timestamp. |
| Rate | Occasional steps; at most `max_rate_hz` = 2 sends/s to the device; intermediate targets are coalesced (latest wins), never rejected. |
| BLE drop | Reconnect with backoff, re-take control, set 0 W, bump `link_epoch`, report `last_error`. `set` while the link is down → `link_down`. The client must resend its target. |
| Client disconnect | Spin down to 0 W, **keep** the BLE link and control, wait for the next client. |
| Measured power | Indoor Bike Data is notify-only, so "polling" is implemented as subscribe-and-cache. |

## 3. Module layout

```
src/wahoo_lsl/
  ble.py                 # MODIFIED: ScanResult gains service_uuids; shared find_device()
  config.py              # MODIFIED: device/reconnect section parsers made public
  ftms/
    __init__.py          # exports nothing that imports bleak
    protocol.py          # pure: UUIDs, opcodes, encoders, decoders (no I/O)
    config.py            # FtmsConfig + load_ftms_config (reuses config.py parsers)
    link.py              # FtmsLink: the only ftms module importing bleak (via ble.py)
    controller.py        # Controller: state, rate-limited sender, spin_down
    server.py            # asyncio TCP server, JSON-lines protocol
    client.py            # KickrClient — stdlib only (socket, json)
    cli.py               # wahoo-ftms: serve | read | set
ftms.example.json
tests/
  ftms_fakes.py          # FakeFtmsBackend / FakeFtmsClient (BLE side)
  test_ftms_protocol.py, test_ftms_config.py, test_ftms_link.py,
  test_ftms_controller.py, test_ftms_server.py, test_ftms_client.py, test_ftms_cli.py
```

Console script: `wahoo-ftms = "wahoo_lsl.ftms.cli:main"`.

**Import rule:** `wahoo_lsl.ftms.client` imports only the standard library, so the client
can be used from an experiment environment that lacks `bleak`/`pylsl`. `ftms/cli.py` imports
`link`, `controller` and `server` lazily, inside the `serve` path only.

### Changes to existing modules (behaviour-preserving)

- `ble.ScanResult` gains `service_uuids: frozenset[str] = frozenset()` (lower-case 128-bit
  strings), filled by `BleakBackend.discover`. `cps_advertised` stays.
- `ble.find_device(backend, device_cfg, platform, advertises) -> device` is extracted from
  `CpsClient.find_device` (same rules: mac/uuid → `find_by_address`; else name match,
  prefer results for which `advertises(scan_result)` is true, `AmbiguousDevice` on >1).
  `CpsClient.find_device` passes `lambda r: r.cps_advertised`; FTMS passes
  `lambda r: FTMS_SERVICE_UUID in r.service_uuids`. Existing tests must still pass unchanged.
- `config.py`: `_section`, `_parse_device`, `_parse_reconnect`, `_pos_number` are renamed to
  public `section`, `parse_device`, `parse_reconnect`, `pos_number` (call sites updated);
  `ConfigError` reused.

## 4. Clocks

Same rules as the logger spec §4: `t_lsl` = `clock.now_lsl()` (`pylsl.local_clock()`,
seconds, monotonic) is the only timing reference and is what the client sees. Controller and
CPS logger timestamps on the same machine are directly comparable. Wall time is not used by
this service except in log-record timestamps from `logging`.

## 5. FTMS protocol (`ftms/protocol.py`, pure)

All multi-byte values little-endian.

### 5.1 UUIDs (full 128-bit, lower-case)

| Name | Short | Use |
|---|---|---|
| Fitness Machine Service | 0x1826 | service |
| Fitness Machine Feature | 0x2ACC | read |
| Indoor Bike Data | 0x2AD2 | notify |
| Supported Power Range | 0x2AD8 | read |
| Fitness Machine Control Point | 0x2AD9 | write-with-response + indicate |
| Fitness Machine Status | 0x2ADA | notify |

All characteristics are resolved on the FTMS service object (never collection-wide), as in
the logger.

### 5.2 Fitness Machine Feature (2ACC)

Two uint32: `machine_features`, `target_setting_features`. Required:
`target_setting_features` bit 3 (Power Target Setting Supported). Missing → setup error
`FtmsUnsupported`. Payloads shorter than 8 bytes are zero-padded (lesson from the KICKR's
2-byte CP Feature); empty → `ProtocolError`.

`parse_feature(data) -> FtmsFeatures(machine: int, target: int, power_target_supported: bool)`.

### 5.3 Control Point (2AD9)

Encoders (return `bytes`):

| Function | Bytes |
|---|---|
| `request_control()` | `00` |
| `start_or_resume()` | `07` |
| `set_target_power(watts: int)` | `05` + sint16 W; `ValueError` outside −32768..32767 |

Response indication: `80 <request_opcode> <result>`, optionally followed by parameters.
`parse_cp_response(data) -> CpResponse(request_opcode: int, result: int)`; raises
`ProtocolError` if shorter than 3 bytes or byte 0 ≠ 0x80.

Result codes: `0x01 success`, `0x02 op code not supported`, `0x03 invalid parameter`,
`0x04 operation failed`, `0x05 control not permitted`. `RESULT_NAMES` maps them for
messages; unknown codes render as `0xNN`.

### 5.4 Fitness Machine Status (2ADA)

`parse_status(data) -> StatusEvent(opcode: int, name: str, target_power_w: int | None)`.
Interpreted opcodes: `0x08` target power changed (sint16 W → `target_power_w`), `0xFF`
control permission lost. All others are returned with their name (`0x01` reset, `0x02`
stopped/paused by user, `0x04` started/resumed, else `0xNN`) and are only logged.
Empty → `ProtocolError`.

### 5.5 Indoor Bike Data (2AD2)

uint16 flags, then fields present per flags, in this order:

| Bit | Field | Size | Presence |
|---|---|---|---|
| 0 | Instantaneous speed (0.01 km/h) | uint16 | present when bit 0 **= 0** ("More Data" inverted) |
| 1 | Average speed | uint16 | bit set |
| 2 | Instantaneous cadence (0.5 rpm) | uint16 | bit set |
| 3 | Average cadence | uint16 | bit set |
| 4 | Total distance (m) | uint24 | bit set |
| 5 | Resistance level | sint16 | bit set |
| 6 | Instantaneous power (W) | sint16 | bit set |
| 7 | Average power | sint16 | bit set |
| 8 | Expended energy | uint16+uint16+uint8 | bit set |
| 9 | Heart rate | uint8 | bit set |
| 10 | Metabolic equivalent | uint8 | bit set |
| 11 | Elapsed time | uint16 | bit set |
| 12 | Remaining time | uint16 | bit set |

`parse_bike_data(data) -> BikeData(power_w: int | None, cadence_rpm: float | None, speed_kmh: float | None)`.
Walks fields in order up to and including power; fields after power are not parsed.
A message without bit 6 yields `power_w=None` (not an error — devices may split data across
notifications). Truncated payload for a flagged field → `ProtocolError`.

### 5.6 Supported Power Range (2AD8)

sint16 min, sint16 max, uint16 increment → `PowerRange(min_w, max_w, step_w)`. Shorter than
6 bytes → `ProtocolError`. If the characteristic is absent, the range is
`PowerRange(0, 2000, 1)` and a warning is logged.

## 6. Config (`ftms.example.json`, `ftms/config.py`)

A separate file from the logger's config (the two may run on different machines).

```json
{
  "device": { "name": "KICKR", "mac": null, "uuid": null, "scan_timeout_s": 10 },
  "reconnect": { "enabled": true, "max_attempts": 0, "backoff_s": [1, 2, 5, 10] },
  "ftms": {
    "host": "127.0.0.1",
    "port": 47610,
    "max_rate_hz": 2,
    "ack_timeout_s": 1.0,
    "max_watts": null
  }
}
```

- `device`, `reconnect`: parsed by the shared `config.parse_device` / `parse_reconnect`
  (identical semantics, including platform mac/uuid selection).
- `ftms.host`: must be a loopback address (`127.0.0.1`, `::1` or `localhost`); anything else →
  `ConfigError`. Default `127.0.0.1`.
- `ftms.port`: int 1..65535, default 47610.
- `ftms.max_rate_hz`: positive number, default 2. Minimum interval = `1 / max_rate_hz`.
- `ftms.ack_timeout_s`: positive number, default 1.0.
- `ftms.max_watts`: null or positive int. Effective cap = min(device range max, `max_watts`).
- Unknown keys at any level → `ConfigError` (same strictness as the logger).

`load_ftms_config(path) -> FtmsConfig(device, reconnect, ftms: FtmsSettings)`.

## 7. Behaviour

### 7.1 Tasks (all on one asyncio loop)

- **Link** (`FtmsLink`): connection, notifications/indications, `command()`, reconnect loop.
- **Sender** (`Controller._sender`): pushes the latest requested target to the device.
- **Server** (`asyncio.start_server`): one handler per connection; at most one active client.
- **Main** (`cli._serve`): wiring, signal handling, ordered shutdown.

BLE callbacks run on the loop thread (bleak, all backends), so shared state needs no locks.
The only serialisation is `FtmsLink`'s `asyncio.Lock` around control-point commands.

### 7.2 `FtmsLink` (`ftms/link.py`)

```python
class FtmsLink:
    def __init__(self, cfg: FtmsConfig, *, backend: BleBackend | None = None,
                 platform: str | None = None) -> None
    # Handlers, assigned by the Controller (default: no-op). All called on the loop thread.
    on_bike_data: Callable[[BikeData, float], None]     # (data, t_lsl)
    on_status: Callable[[StatusEvent, float], None]      # (event, t_lsl)
    on_link_lost: Callable[[str], None]                  # (reason)
    power_range: PowerRange          # valid after open()
    async def open(self) -> None     # §7.3 steps 1–4; raises FtmsSetupError (exit 3)
    async def command(self, payload: bytes) -> CpResponse   # raises LinkError / CommandRejected
    async def close(self) -> None    # bounded (SHUTDOWN_TIMEOUT_S) disconnect; idempotent
    @property
    def connected(self) -> bool
```

Errors: `LinkError` = transport failure (not connected, write failed, link closed while
waiting); `CommandTimeout(LinkError)` = no response within `ack_timeout_s`;
`CommandRejected(opcode, result)` = the device answered with a result other than success.
`FtmsSetupError(BleSetupError)`, `FtmsUnsupported(FtmsSetupError)`.

`command()`:
1. Acquire the lock (one outstanding control-point op).
2. Create a future keyed by the request opcode **before** writing (the indication may arrive
   before the write call returns).
3. `write_gatt_char(cp_char, payload, response=True)`.
4. Steps 3 and 4 together are bounded by `ack_timeout_s`. Timeout → `CommandTimeout`.
   Response result ≠ success → `CommandRejected(opcode, result)`. Responses for an opcode
   with no waiter are logged at DEBUG and dropped.
5. Comms errors (`BleakError`, `OSError`) → `LinkError` with the exception text. `close()`
   fails any pending waiter with `LinkError("link closed")`.

Debug logging (logger `wahoo_lsl.ftms.link`, DEBUG): every write (hex, opcode name), every
indication (hex, decoded, round-trip ms from write), every status and bike-data notification
(hex + decoded).

A BLE disconnect callback (unexpected only; intentional `close()` clears the client first,
as in `CpsClient`) or a status `0xFF` (control permission lost) calls `on_link_lost(reason)`.

### 7.3 Startup sequence (also used on every reconnect)

1. `find_device(..., service_uuid=FTMS)`; connect.
2. Resolve characteristics on the FTMS service; read Feature (require power target),
   read Supported Power Range.
3. `start_notify` on Control Point (indications) **first**, then Machine Status, then
   Indoor Bike Data.
4. `command(request_control())` → `command(start_or_resume())` → `command(set_target_power(0))`.
   Any failure → `FtmsSetupError` (nothing left connected).
5. Controller marks `link = "ready"`, `confirmed_w = 0`, `requested_w = 0`, `link_epoch += 1`.

The TCP port is **bound** before the first BLE startup (so "port in use" fails in
milliseconds with exit 2) but **accepts** connections only after the first successful
startup; it stays up through later reconnects. Failure of the first startup exits with code 3.

### 7.4 Controller state & reconnect (`ftms/controller.py`)

```python
@dataclass
class State:
    link: str                 # "connecting" | "ready" | "reconnecting" | "closed"
    link_epoch: int           # +1 each time step 5 of §7.3 completes
    requested_w: int          # latest accepted target (after clamping)
    confirmed_w: int | None   # last value the device confirmed
    confirmed_t_lsl: float | None
    power_w: int | None       # latest measured instantaneous power
    power_t_lsl: float | None
    last_error: str | None    # most recent error message; never cleared automatically
    last_error_t_lsl: float | None
```

`pending` (reported) = `link == "ready" and requested_w != confirmed_w`.

On `on_link_lost(reason)` (or 3 consecutive `LinkError`s from the sender):
`link = "reconnecting"`, `last_error = "link lost: <reason>; target reset to 0 W"`,
`requested_w = 0`, `confirmed_w = None`; close the BLE client (bounded), then retry §7.3
with `ReconnectPolicy` from config. On success → step 5 (epoch bump, 0 W confirmed). If
`reconnect.enabled` is false or attempts are exhausted → `ReconnectExhausted`, service shuts
down (§7.7) with exit code 4.

Measured power: `on_bike_data` stores `power_w`/`power_t_lsl` when `power_w` is not None.
Status `0x08` sets `confirmed_w`/`confirmed_t_lsl` (second verification source).

### 7.5 `set` handling and the rate-limited sender

`Controller.request_target(watts: int) -> tuple[int, bool]` (accepted value, clamped):
- `link != "ready"` → `LinkDown`.
- Clamp to `[max(0, range.min_w), cap]` where `cap = min(range.max_w, max_watts or ∞)`, then
  round to the device `step_w`. `clamped = (result != watts)`.
- Store `requested_w`, set the sender's wake event, return immediately. No BLE I/O.

Sender loop (rate limiting **without** adding latency to occasional steps):
```
min_interval = 1 / max_rate_hz
loop:
    wait until wake event set or retry due
    if link != "ready" or requested_w == confirmed_w and not retry: clear & continue
    sleep until last_send_t + min_interval  (no sleep if already past)
    target = requested_w            # read AFTER the sleep → latest wins
    last_send_t = now
    try command(set_target_power(target)):
        success → confirmed_w = target, confirmed_t_lsl = now; consecutive_timeouts = 0
    LinkError       → last_error, consecutive_failures += 1, retry after min_interval;
                      3 in a row → treat as link lost (§7.4)
    CommandRejected → last_error, retry after min_interval; after 3 rejections stop retrying
                      until the next request_target() call
    success only counts if link_epoch is unchanged since the send (a reconnect in between
    must not mark a stale value confirmed)
```
A single `set` after an idle period is sent immediately. Intermediate values requested
during the sleep are never sent (DEBUG log: "coalesced N targets").

### 7.6 Spin-down

`Controller.spin_down(reason: str) -> bool`: sets `requested_w = 0` and, if the link is
ready, sends `set_target_power(0)` directly (bypassing the rate limiter, still through the
link lock), waiting at most 2 s. Logs at INFO `spin-down (<reason>): 0 W confirmed` or
WARNING on failure. Returns success. Never raises.

Triggers:
- client disconnect (EOF, connection reset, handler exception) → `spin_down("client disconnected")`;
  service keeps running and accepts the next client.
- unexpected exception in any task → shutdown with `Controller.close(reason="internal error")`
  (which spins down), exit 1.
- SIGINT/SIGTERM (handlers on non-win32; KeyboardInterrupt on win32) → shutdown §7.7, exit 0.
  A second signal forces immediate exit (as in the logger).

### 7.7 Shutdown order

1. Stop accepting connections; close the active client connection.
2. `spin_down("shutdown")`, up to 3 attempts 0.2 s apart (nothing retries after shutdown); if all
   fail, log an ERROR that the trainer may still hold its last target.
3. `link.close()` (stop notifications, disconnect; each step bounded by `SHUTDOWN_TIMEOUT_S`).
   Disconnecting releases FTMS control.
Shutdown is bounded: at most 3 × 2 s for spin-down plus 5 s per unsubscribe/disconnect step.

## 8. TCP protocol (`ftms/server.py`)

Newline-delimited UTF-8 JSON. Max request line 4096 bytes (longer → `bad_request`, connection
closed). One reply per request, in order.

### 8.1 Requests and replies

`{"cmd":"set","watts":150}` →
```json
{"ok": true, "target_w": 150, "clamped": false}
```

`{"cmd":"read"}` →
```json
{"ok": true, "link": "ready", "link_epoch": 1,
 "target": {"requested_w": 150, "confirmed_w": 150, "pending": false, "t_lsl": 43810.71},
 "power":  {"watts": 143, "t_lsl": 43811.70, "age_s": 0.42},
 "last_error": null, "last_error_t_lsl": null}
```
`target.t_lsl` = `confirmed_t_lsl`; `power.age_s` = now − `power_t_lsl`; null fields when
unknown.

Errors: `{"ok": false, "error": "<code>", "detail": "<text>"}` with codes:

| Code | When | Connection |
|---|---|---|
| `bad_request` | not JSON, not an object, unknown `cmd`, line too long | kept (closed if too long) |
| `invalid_watts` | `watts` missing, bool, non-integer or < 0 | kept |
| `link_down` | `set` while `link != "ready"` | kept |
| `busy` | a client is already connected | closed after reply |

Every connection ending (clean EOF or error) of the **active** client triggers spin-down.

## 9. Client API & CLI

### 9.1 `KickrClient` (`ftms/client.py`, stdlib only)

```python
class KickrError(Exception):            # .code, .detail
class TargetState(NamedTuple): requested_w: int; confirmed_w: int | None; pending: bool; t_lsl: float | None
class PowerReading(NamedTuple): watts: int | None; t_lsl: float | None; age_s: float | None
class Status(NamedTuple): link: str; link_epoch: int; target: TargetState; power: PowerReading;
                          last_error: str | None; last_error_t_lsl: float | None

class KickrClient:
    def __init__(self, host="127.0.0.1", port=47610, timeout_s=2.0)  # connects (TCP_NODELAY)
    def set_target_power(self, watts: float) -> int   # any real number, rounded to whole W; bool/non-numbers
                                                      # -> TypeError, NaN/inf -> ValueError (nothing sent);
                                                      # returns accepted (clamped) value
    def read_target_power(self) -> TargetState
    def read_power(self) -> PowerReading
    def read(self) -> Status
    def close(self) -> None                           # also via `with`; triggers spin-down
```
`ok:false` → `KickrError(code, detail)`. Socket errors propagate as `OSError`.
Not thread-safe (document it); one client per process.

### 9.2 CLI (`wahoo-ftms`)

```
wahoo-ftms serve --config ftms.json [--debug] [--diag-log PATH]
wahoo-ftms read  [--host H] [--port P]
wahoo-ftms set WATTS [--host H] [--port P] [--timeout 5] [--hold SECONDS]
```

- `serve`: runs the service. `--debug` = DEBUG for `wahoo_lsl.*`; `--diag-log` adds bleak
  DEBUG to a file (same as the logger).
- `read`: prints one status line (link, epoch, requested/confirmed target, power, age,
  last_error).
- `set`: prints status, sends the target, polls `read` every 100 ms until
  `confirmed_w == accepted and not pending` or `--timeout`, prints status again. Then holds the
  connection for `--hold` seconds (default 0; Ctrl-C ends the hold early) and exits.
  **Closing the connection spins the trainer down to 0 W** — this is the required
  client-disconnect behaviour; `--hold` exists so the target can be observed on the trainer.

Exit codes:

| Command | 0 | 1 | 2 | 3 | 4 | 130 |
|---|---|---|---|---|---|---|
| `serve` | clean shutdown | unexpected error | bad config / cannot bind port | BLE setup failed | reconnect exhausted | — |
| `read`/`set` | success | service replied `ok:false` | cannot connect to service | `set` not confirmed within timeout | — | Ctrl-C before completion |

## 10. Platform-specific behaviour (complete list)

- Device identifier selection: inherited from `config.select_identifier` (darwin → uuid).
- Signal handlers: `loop.add_signal_handler` on non-win32 only (as in the logger).
No other OS checks.

## 11. Testing

All tests run with `-W error`, no hardware, in the `wahoo` env.

- `test_ftms_protocol.py`: encoders; CP response (success, each error code, short, wrong
  first byte); status (0x08 with value, 0xFF, unknown); Indoor Bike Data with flag
  combinations (speed present/absent via bit 0, cadence, distance uint24, power, power
  absent → None, truncated → error); feature (8 bytes, short padded, empty error, power
  target bit off); power range.
- `test_ftms_config.py`: defaults, non-loopback host rejected, bad port/rate/timeout,
  unknown keys, shared device parser behaviour.
- `test_ftms_link.py` (fake backend in `tests/ftms_fakes.py` modelling write-with-response +
  indication, configurable delay/drop/error per opcode, unexpected disconnect, status 0xFF):
  startup order (CP subscribe before any write), indication before write returns, timeout,
  error result, unknown-opcode response ignored, close idempotent and bounded.
- `test_ftms_controller.py`: clamp/step rounding; single set sent immediately; burst of sets
  → only first and last sent, ≥ `1/max_rate_hz` apart (use a fake clock/sleep, no wall-clock
  timing); retry on error then stop after 3; 3 timeouts → link lost; link lost → reconnect →
  0 W, epoch bump, `last_error` set, `set` during reconnect → `LinkDown`; status 0x08 updates
  confirmed; spin_down bypasses rate limit and never raises.
- `test_ftms_server.py` (real localhost socket, port 0): set/read round trip, every error
  code, `busy` second client, client EOF → spin-down called, server survives client reset.
- `test_ftms_client.py`: against a stub JSON-lines server: return types, `KickrError`,
  `with` closes. Assert the module imports no non-stdlib modules.
- `test_ftms_cli.py`: `set` polling/timeout exit codes and `read` output against a stub
  server; `serve` exit codes 2 (bad config, port in use) and 3 (setup failure via fake
  backend).
- Existing logger tests must pass unchanged after the `ble.py`/`config.py` refactor.
- README: manual hardware checklist (serve; read; set 100 --hold 10 → trainer resists, then
  drops to 0 W on exit; kill client mid-hold → 0 W; switch trainer off mid-session →
  reconnect, epoch bump, 0 W; run the CPS logger alongside and confirm both work).
