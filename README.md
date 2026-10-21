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
