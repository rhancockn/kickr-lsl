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
{"type":"sample","seq":42,"t_lsl":12345.678901,"t_wall":"2026-10-20T18:01:02.123456+00:00","raw":"1000a500...","flags":16,"fields":{"power_w":165,"wheel_revs":1203,"wheel_event_t":40321,"offset_compensation":false},"derived":{"speed_kmh":31.2,"cadence_rpm":88.0}}
{"type":"event","kind":"disconnected","t_lsl":12350.1,"t_wall":"...","detail":{"name":"KICKR CORE","address":"..."}}
```

Event kinds: `clock_sync`, `connected`, `features`, `disconnected`, `reconnect_attempt`, `reconnected`, `stopped`.
Samples that fail to parse keep `raw` and add `parse_error`.
`derived` holds speed and cadence computed once from successive wheel/crank counters (see LSL stream); it is `{}` on parse errors.

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
- **Metadata:** `desc/acquisition` (software, version, clock, timestamp meaning, nominal rate), `desc/device` (name, address, sensor location,
  CP feature bits, Device Information fields), `desc/derivation` (`wheel_circumference_m` from
  `derive.wheel_circumference_m`, default 2.105 m, and the derivation constants).
- The stream appears once the device is connected and stays up across BLE reconnects to the same device.
- liblsl prints its own INFO lines (e.g. `common.cpp ... INFO| git:...`) to stderr; they are harmless. On Windows the first run may show a firewall prompt for python.exe — allow it on private networks or LSL consumers on other machines cannot see the stream.

**Timing.** Each sample's timestamp is its `t_lsl` (BLE notification arrival, same clock as the log). The KICKR
notifies at ≈ 0.9998 Hz with ±0.1 s delivery jitter, and gaps (disconnects, parse errors) are not filled. Use the
timestamps, not sample index ÷ rate (that drifts ≈ 0.7 s/h and misaligns after gaps). pyxdf's default
`dejitter_timestamps=True` replaces timestamps of regular streams with a linear fit; pass `False` for raw arrival times.

If the wheel or cranks stop for longer than the sensor's event-time wrap (32 s wheel, 64 s crank), the first revolution afterwards only re-primes the calculation, so speed/cadence resume one notification later.

Quick check while `run` is going (save as `lsl_check.py`, run `python lsl_check.py KICKR-CPS-f24bfa`):

```python
import sys
import pylsl
source_id = sys.argv[1]  # from the "LSL outlet <source_id>: ..." log line, e.g. KICKR-CPS-f24bfa
streams = pylsl.resolve_byprop("source_id", source_id, timeout=10)
if not streams:
    sys.exit(f"no LSL stream with source_id {source_id}")
inlet = pylsl.StreamInlet(streams[0])
print(inlet.info().as_xml())
while True:
    sample, ts = inlet.pull_sample()
    print(f"{ts:.3f}", sample)
```

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
7. `list-features` prints the Device Information fields (model number etc.); `run` logs "LSL outlet KICKR…-CPS-…" and the resolver snippet under "LSL stream" shows live samples.

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
wahoo-ftms set 250 --slew 20           # ramp at 20 W/s (0 = immediate) and wait for the final target
```

The trainer goes back to 0 W whenever the client disconnects (including when `wahoo-ftms set`
exits), on any service error, and on shutdown. After a BLE drop the service reconnects and sets
0 W; `link_epoch` increases and the client must send its target again.

**Slew.** Only power increases are slewed; a decrease (including to 0 W) goes straight to the
trainer. By default the service ramps up to each higher target at `ftms.slew_w_per_s` (10 W/s), sending
an interpolated setpoint every `ftms.slew_update_interval_s` (10 s) so the KICKR's own ERG ramp smooths
each step: 100 → 250 W sends 200 W after 10 s and 250 W after 15 s; a change smaller than one
interval's worth is sent once, when its ramp ends. While a ramp runs, updates go out every
`slew_update_interval_s`; a new target or rate change mid-ramp continues from the current setpoint
without postponing the next update. `max_rate_hz` still limits how often anything is sent. Set `"slew_w_per_s": 0` in
`ftms.json` (or call `set_slew_rate(0)`) for the old behaviour: each target goes straight to the
trainer. A client's rate lasts until it disconnects. Disconnects, BLE drops and shutdown always go to
0 W immediately, without a ramp. A decrease sent mid-ramp cancels the ramp.

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
    kickr.set_slew_rate(20)             # W/s for this connection; 0 = immediate
    print(kickr.read_target_power())    # TargetState(requested_w, confirmed_w, pending, t_lsl; also setpoint_w, ramp_end_t_lsl)
    print(kickr.read_power())           # PowerReading(watts, t_lsl, age_s)
# leaving the block disconnects -> service spins down to 0 W
```

### Remote clients (allowed hosts)

By default the service listens on `127.0.0.1` only. To let other lab PCs connect over plain TCP,
list them (DNS names or IP literals, case-insensitive) in `ftms.json`:

```json
"ftms": { "host": "127.0.0.1", "port": 47610, "allowed_hosts": ["lab-pc-3.lab.local", "10.0.0.23"] }
```

With a non-empty `allowed_hosts` the service listens on `ftms.listen_host` (default `"0.0.0.0"`; set
it to one interface's IPv4 address to limit it) and accepts loopback plus the listed machines; anyone
else gets `not_allowed` and is closed. `ftms.host` must stay `127.0.0.1` in this mode, and
`listen_host` needs `allowed_hosts`. Names are re-resolved on every connection (2 s limit). Accepted
risk: the check is by source address only, with no authentication, so anyone who can spoof or share a
listed address can set trainer power. Local tools must dial `127.0.0.1`: the PC's own LAN address is
refused unless it is listed. Firewall: Windows installs already open the port on the private profile;
macOS and Linux may prompt or need a rule for the port.

Remote PCs use `KickrClient("server-pc.lab.local", 47610)` or a plain socket.

### Wire protocol (plain-socket clients)

- TCP, UTF-8, one JSON object per line (`\n`), requests and replies alternate. Request lines are
  limited to 4096 bytes. One client at a time; the trainer returns to 0 W when it disconnects.
- If the connection is refused the server sends one error line immediately after connect, without
  waiting for a request, then closes: `busy` (another client is connected) or `not_allowed`
  (your address is not in `ftms.allowed_hosts`). Read a line before your first request.
- `{"cmd":"set","watts":150}` -> `{"ok":true,"target_w":150,"clamped":false}` (non-negative integer;
  clamped to `max_watts`; returns before the trainer confirms).
- `{"cmd":"slew","w_per_s":20}` -> `{"ok":true,"slew_w_per_s":20.0}` (0..1000, 0 = immediate; applies to
  increases and lasts until you disconnect).
- `{"cmd":"read"}` -> `{"ok":true,"link":"ready","link_epoch":1,"target":{...},"power":{...},...}`:
  - `link`, `link_epoch`: BLE link state and a counter that increases after each reconnect.
  - `target`: `requested_w`, `confirmed_w`, `pending`, `t_lsl`, `setpoint_w`, `ramp_end_t_lsl`.
  - `power`: `watts`, `t_lsl`, `age_s` (null until the first notification).
  - `last_error`, `last_error_t_lsl`; `slew`: `w_per_s`, `update_interval_s`.
- Errors: `{"ok":false,"error":CODE,"detail":"..."}` with CODE one of
  `bad_request|invalid_watts|invalid_slew|link_down|busy|not_allowed`.

### Manual hardware checklist (FTMS)

1. `wahoo-ftms serve -c ftms.json --debug`: log shows request_control, start_or_resume and set 0 W each answered with success.
2. `wahoo-ftms read`: `link=ready epoch=1 target=0 W` and power updating about once per second while pedalling.
3. `wahoo-ftms set 100 --slew 0 --hold 10` while pedalling: resistance rises within ~2 s; after the hold the trainer returns to 0 W.
4. Run a Python client that sets 150 W, then kill it (`kill -9`): the trainer returns to 0 W.
5. Switch the trainer off for ~10 s mid-session: warnings, reconnect, `epoch=2`, target 0 W.
6. Open the Wahoo app or Zwift and let it take control: "control permission lost" warning, reconnect, 0 W.
7. Run `wahoo-lsl run -c config.json` at the same time: both connect and work (the KICKR accepts multiple BLE centrals).
8. Ctrl-C the service while holding a target: "spin-down (shutdown): 0 W confirmed", exit 0.
9. With the default slew: `wahoo-ftms set 250 --hold 20` from 100 W while pedalling: `--debug` service log shows 200 W at ~+10 s and 250 W at ~+15 s.
10. `"slew_w_per_s": 0` in ftms.json: `wahoo-ftms set 150` is applied within ~2 s as before.
11. With the default slew: `wahoo-ftms set 100 --hold 20` from 250 W: the decrease is sent at once (no ramp).

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
the waits). Before the FTMS service is signalled, the launcher connects, sets 0 W and
waits up to 3 s for confirmation. The port wait is the FTMS config's scan timeout + 20 s.
The exit code is the first non-zero exit among the three (a child that fails during
start-up returns its own code), or 2 for a config problem, a missing script or the FTMS
port not opening.

The **Slew W/s** box sets the FTMS slew rate for the dashboard's connection (Apply); it
starts at the FTMS config's `slew_w_per_s` (override with `wahoo-dashboard --slew W_PER_S`)
and is re-sent whenever the dashboard reconnects. Moving the slider up ramps at
that rate; moving it down and the 0 W button are immediate; closing the dashboard (or stopping `wahoo-dashboard`) drops to 0 W immediately.
Check: with slew 10 W/s, move the slider from 100 to 250 W: the target panel shows
`setpoint` and `ramp N s`, and the confirmed-target graph steps at ~10 s and ~15 s.

To open only the window against a service and logger that are already running:

```bash
python -m wahoo_lsl.dashboard.app --log-dir logs [--host 127.0.0.1] [--port 47610] [--max-watts 1000] [--circumference-m 2.105]
```

- **Slider:** sends the target as you drag (the service applies at most 2 changes per
  second, latest wins). Moving it up ramps at the current slew rate; moving it down and "0 W" are sent immediately (closing the
  dashboard also drops to 0 W immediately). When the dashboard (re)connects
  the slider resets to 0, matching the service's spin-down.
- **Target panel:** requested and confirmed target read back from the service, link state
  and last error, with a graph of the confirmed target.
- **CPS panels:** power, speed and cadence with graphs. Speed and cadence are calculated
  from the wheel and crank revolution counters; speed assumes a 2.105 m wheel unless you
  pass `--circumference-m`.
  The CPS values show "—" after 15 s without samples.
- **Graphs:** fill left to right over 5 minutes, then all clear and start again.
- **Lag:** the logger writes to disk every `log.flush_every_n` samples, so CPS values can
  arrive several seconds late (points still land at the time they were measured). Set
  `"flush_every_n": 1` in the logger config for a livelier display.
