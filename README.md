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
conda activate wahoo
wahoo-lsl scan                                   # find nearby CPS devices
wahoo-lsl list-features -c config.json           # print supported features and measurements
wahoo-lsl run -c config.json                     # stream + log until Ctrl-C
wahoo-lsl run -c config.json --debug             # also print each sample, verbose logging
```

Do not use `conda run` for these: it buffers all output until the command exits (no live warnings or `--debug` lines),
and Ctrl-C under it is reported as a conda error with exit code 1 instead of a clean stop.

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
5. Ctrl-C (in an activated `wahoo` shell, not via `conda run`): clean exit (code 0, check with `echo $?`), last line of the log is a `stopped` event.
6. `run --debug`: each sample printed to stdout.

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

A second Ctrl-C while `serve` is shutting down forces an immediate exit; the spin-down to 0 W
may then not complete.

Exit codes: `serve` 0 clean · 1 unexpected error · 2 config / port in use / unwritable diagnostic log ·
3 BLE setup failed · 4 reconnect exhausted. `read` / `set` 0 ok · 1 service error ·
2 cannot reach the service · 3 target not confirmed in time · 130 Ctrl-C.

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
