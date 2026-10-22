# FTMS Target Slew — Design

Date: 2026-10-22
Status: approved in conversation; awaiting written-spec review
Branch: `feat/ftms-slew` (off `main`, which includes the dashboard)
Extends: `docs/superpowers/specs/2026-10-21-kickr-ftms-control-design.md`

## Purpose

Make the FTMS control service move to each new target power gradually, at a
configurable slew rate (W/s), instead of stepping straight to it. Intermediate
setpoints are sent to the trainer at a configurable interval (default 10 s) so
the KICKR's own ERG ramp smooths each step. The rate is set per client at
runtime (`set_slew_rate`) and shown/editable in the dashboard. A slew rate of
0 keeps today's behaviour exactly.

## Behaviour

### Ramp

- Each `set` stores the final target T (clamped as today) and starts a ramp
  from the current setpoint S to T at rate R (W/s), starting at t0 = now:
  duration `D = |T − S| / R`.
- The current setpoint S is the interpolated ramp value at the moment of the
  request (so a new target mid-ramp continues smoothly from where the ramp
  was); with no ramp running, S is the last value sent to the trainer (the
  confirmed target after `_mark_ready`/spin-down: 0 W).
- An update is due I (the update interval) after the previous update, or at
  `t0 + D` if that is sooner. The cadence is anchored at the ramp start only
  when starting from idle; a new target or rate change mid-ramp keeps it, so
  frequent retargets cannot postpone updates. Each update is the interpolated value `S + (T − S)·(t − t0)/D` rounded
  to whole watts and passed through `Controller.clamp` (trainer step and
  limits); the final update is sent at `t0 + D` and is exactly T. A ramp
  shorter than I therefore sends one update at `t0 + D`.
- Example, 100 W → 250 W at 10 W/s, I = 10 s (D = 15 s): sends 200 W at
  t0+10 s and 250 W at t0+15 s. Nothing is sent in the first 10 s.
- An update whose rounded/clamped value equals the last value sent is
  skipped (no redundant writes).
- `max_rate_hz` still sets the minimum gap between any two sends (a slider
  drag cannot flood the trainer); a due update waits for that gap.
- Changing R mid-ramp restarts the ramp from the current interpolated value
  with the new rate (new t0; the update cadence is kept). Changing R to 0 sends T next (subject to the
  `max_rate_hz` gap), as today.
- R = 0: today's code path unchanged — each `set` goes straight to the
  sender, rate-limited by `max_rate_hz`, latest wins.

### Cancelling the ramp (immediate 0 W)

Unchanged safety paths bypass the ramp and cancel it:

- client disconnect → immediate spin-down to 0 W, **and R resets to the
  configured default**;
- BLE link loss / reconnect → target 0 W, no ramp (R kept: the client is
  still connected);
- shutdown → spin-down.

### Retries

Rejections and link failures of a ramp update are handled by the existing
sender rules (`MAX_REJECTIONS`, `MAX_LINK_FAILURES`); after a rejected
update the ramp continues with the next scheduled value.

## Components

### `ftms/slew.py` (new, pure)

```
@dataclass(frozen=True)
class Ramp:
    start_w: float; target_w: int; rate_w_per_s: float; t0: float; interval_s: float
    end_t -> float                       # t0 + |target − start| / rate
    value_at(t) -> float                 # linear, clamped to [t0, end_t]
```

No I/O, no clocks, no update grid (the controller schedules updates); rounding/clamping to trainer limits stays in the
controller (it owns `clamp`). `rate_w_per_s > 0` and `interval_s > 0`
required (`ValueError` otherwise).

### `ftms/config.py`

`FtmsSettings` gains `slew_w_per_s: float = 10.0` (≥ 0, finite, ≤ 1000) and
`slew_update_interval_s: float = 10.0` (> 0, finite). `_FTMS_KEYS` gains both.
`ftms.example.json` gains both keys. Note: the user's existing `ftms.json`
lacks them, so the service slews at 10 W/s after this change; README says to
add `"slew_w_per_s": 0` for the old behaviour.

### `ftms/controller.py`

- New state: `slew_w_per_s` (current rate; config default at start and on
  client disconnect), `_ramp: Ramp | None`, `_last_sent_w` (last value
  sent/confirmed, the ramp origin when idle).
- `request_target(watts)`: unchanged signature/return. With R > 0, builds a
  new `Ramp` from the current setpoint to the accepted target and wakes the
  sender. With R = 0, today's behaviour.
- `set_slew_rate(w_per_s) -> float`: validates (finite, 0 ≤ R ≤ 1000;
  `ValueError` otherwise), stores, restarts any running ramp from its current
  value (or drops it when R = 0, leaving T pending for the sender), wakes the
  sender. Allowed in any link state.
- `reset_slew()`: R back to the config default, ramp cancelled (called by
  the server on client disconnect, before the spin-down).
- `_sender`: when a ramp is active, sleeps until
  `max(min(last_update_t + I, ramp.end_t), last_send + 1/max_rate_hz)`
  (waking early on any new request/rate change), then sends the clamped
  interpolated value; the final update sends T and ends the ramp. Without a
  ramp, today's loop.
- `spin_down`, `_mark_ready`, `_on_link_lost`: cancel the ramp; setpoint 0.
- `snapshot()`: `target` gains `setpoint_w` (current interpolated value,
  rounded+clamped; equals `requested_w` when not ramping) and
  `ramp_end_t_lsl` (`t0 + D` on the controller clock, `None` when not
  ramping); top level gains `slew: {"w_per_s": R, "update_interval_s": I}`.
  `requested_w` stays the final target, `confirmed_w` the last confirmed
  step, `pending` true until T is confirmed.

### `ftms/server.py`

- New command `{"cmd":"slew","w_per_s":<number>}` → `{"ok":true,
  "slew_w_per_s":<float>}`. Non-number/bool/non-finite/negative/> 1000 →
  `{"ok":false,"error":"invalid_slew",...}`. Works whatever the link state.
- On client disconnect: `controller.reset_slew()` then the existing
  spin-down.
- `ControllerApi` protocol gains `set_slew_rate` and `reset_slew`. The
  unknown-command message lists `'set', 'slew' or 'read'`.

### `ftms/client.py`

- `KickrClient.set_slew_rate(w_per_s: float) -> float` (accepted rate).
  Client-side checks like `set_target_power`: bool/non-number → `TypeError`,
  NaN/inf → `ValueError`, without sending; range checking is the server's.
- `TargetState` gains trailing fields `setpoint_w: int | None = None`,
  `ramp_end_t_lsl: float | None = None`; `Status` gains trailing
  `slew: SlewState | None = None` with `SlewState(w_per_s, update_interval_s)`.
  Defaults keep construction/unpacking by older code working; `_parse_status`
  tolerates replies without the new keys.

### `ftms/cli.py`

- `read` output adds `setpoint` (when ramping) and `slew N W/s`.
- `set N` gains `--slew R` (sets the rate for this connection before the
  target); the confirmation wait becomes `--timeout` plus the remaining ramp
  time (from `ramp_end_t_lsl` minus `local_clock()` at the first read).

### Dashboard

- `PowerWorker` gains `request_slew(w_per_s)` (latest-value mailbox like
  `request`); sends `set_slew_rate` before any pending target; after every
  (re)connect re-sends the last requested slew rate (the server reset it on
  the previous disconnect) and posts `slew_ok` / `slew_error`.
- `Dashboard`: a "Slew W/s" `Spinbox` (0–1000, step 1, initial value from
  `--slew` CLI option, default 10) plus an "Apply" button →
  `worker.request_slew`. Target panel text:
  `requested T W · setpoint S W · confirmed C W[ · pending][ · ramp Ns]`.
  Graph unchanged (confirmed target). The slider and 0 W button go through
  the ramp; only closing the dashboard (disconnect) drops to 0 W at once.
- `launcher.confirm_zero`: calls `set_slew_rate(0)` before `set_target_power(0)`
  so the shutdown drop stays immediate. `launcher` passes `--slew` through to
  the dashboard (default: the FTMS config's `slew_w_per_s`).

## Error handling

| Condition | Behaviour |
|---|---|
| `slew` with bad value | `invalid_slew`; rate unchanged |
| `slew` while link down | accepted; applies to the next target |
| `set` while link down | `link_down` (unchanged) |
| Ramp update rejected / link error | existing retry/give-up rules; ramp continues with later values |
| Link lost mid-ramp | ramp cancelled, target 0 W after reconnect (unchanged) |
| Client disconnects mid-ramp | rate reset to default, immediate spin-down |
| Shutdown mid-ramp | immediate spin-down |

## Testing

- `slew.py`: value_at (up, down, before t0, after end), end_t,
  invalid rate/interval.
- Controller (fake clock/sleep/link, as existing tests): 100→250 W example
  sends exactly (200 @ +10 s, 250 @ +15 s); short ramp single send at +D;
  new target mid-ramp starts from interpolated value; rate change mid-ramp
  (incl. to 0); skip of unchanged rounded value; `max_rate_hz` floor;
  clamp/step on intermediate values; spin-down/link loss/reset_slew cancel
  the ramp; reset_slew restores default; snapshot fields. All existing
  controller tests run with `slew_w_per_s = 0` and stay unchanged.
- Server: `slew` command ok/invalid cases, disconnect calls `reset_slew`
  before spin-down, `read` fields.
- Client: `set_slew_rate` checks and reply; `_parse_status` with and without
  new keys.
- Config: defaults, valid, invalid values.
- CLI: `set --slew` order of calls; extended confirmation wait; `read` text.
- Dashboard: worker sends slew before target, re-sends slew after reconnect;
  spinbox Apply calls `request_slew`; target panel text; launcher
  `confirm_zero` sets slew 0 before 0 W; launcher passes `--slew`.
- Manual (README checklist): 100→250 W at 10 W/s steps at +10 s / +15 s;
  `slew_w_per_s: 0` behaves as before; closing the dashboard drops to 0 W
  immediately.

## Out of scope

- Slew for spin-down paths.
- Persisting a client-set rate across clients or restarts.
- LSL outlet for control events (separate future work).
