# KICKR Test Dashboard — Design

Date: 2026-10-21
Status: approved in conversation; awaiting written-spec review

## Purpose

A minimal desktop dashboard for **testing** the existing FTMS control API and
the CPS logger end to end. It drives target power through `KickrClient`,
shows the target read back from the service, and shows CPS power, speed and
cadence parsed from the logger's JSONL files. A wrapper launches the FTMS
service, the CPS logger and the dashboard as one session.

## Constraints

- **No changes to existing code.** New code lives in `src/wahoo_lsl/dashboard/`.
  The only edit outside it is one new `[project.scripts]` entry in `pyproject.toml`.
- Uses only public surfaces: `wahoo_lsl.ftms.client` (`KickrClient`,
  `KickrError`, `Status`, `DEFAULT_HOST`/`DEFAULT_PORT`) and the documented CPS
  JSONL log format.
- UI: **Tkinter** (stdlib). No new dependencies. `pylsl` (already a project
  dependency) is used only for `pylsl.local_clock()`.
- Cross-platform: macOS, Linux, Windows. Python ≥ the project's floor.
- Runs in the `wahoo` conda env.
- Latency is not critical: graph redraw 1 Hz, value refresh ≤ 0.5 s.

## Components

```
src/wahoo_lsl/dashboard/
  __init__.py
  metrics.py    pure: CpsDeriver, History
  logtail.py    LogFollower (newest cps_*.jsonl, complete lines only)
  power.py      PowerWorker (thread owning KickrClient)
  graph.py      Graph (Tk canvas line chart)
  app.py        Dashboard window; `python -m wahoo_lsl.dashboard.app`
  launcher.py   wrapper; console script `wahoo-dashboard`
```

### metrics.py (pure, no I/O)

`CpsDeriver.update(sample: dict) -> CpsMetrics | None` takes one parsed
`"type":"sample"` record and returns
`CpsMetrics(t_lsl, power_w, speed_kmh, cadence_rpm)`:

- `power_w`: `fields["power_w"]` as-is (None if absent).
- Speed from consecutive samples' `wheel_revs` (uint32, wraps at 2³²) and
  `wheel_event_t` (uint16, 1/2048 s, wraps at 65536):
  `speed_kmh = Δrevs × circumference_m / (Δt_event / 2048) × 3.6`.
- Cadence from `crank_revs` (uint16, wraps at 65536) and `crank_event_t`
  (uint16, 1/1024 s, wraps at 65536): `rpm = Δrevs / (Δt_event / 1024) × 60`.
- When Δrevs = 0 (event time unchanged) the previous value is held; once
  revs have not advanced for **3 s** of `t_lsl`, the value becomes 0.
- Δrevs = 0 with Δt_event ≠ 0, or Δt_event = 0 with Δrevs ≠ 0, is treated as
  no new event (hold). The first sample only primes state (speed/cadence None).
- Missing fields → that metric is None.
- `circumference_m` default **2.105** (700×25c), CLI-overridable.

`History(window_s=300.0)`: `add(t, value)`; `points()` returns
`[(x, value)]` with `x = t − cycle_start` in [0, 300). The cycle starts at
construction time `t0`; when a `t ≥ cycle_start + 300` arrives, all points are
cleared and `cycle_start` advances by whole multiples of 300 so `t` falls in
the new cycle. Points with `t < cycle_start` are dropped. `None` values are
stored as gaps (break the line). All four graphs share one `SweepClock`
(`cycle_start`) so they clear together.

### logtail.py

`LogFollower(log_dir: Path, pattern="cps_*.jsonl", poll_s=0.25)`:

- Every poll, picks the newest matching file by modification time.
- A file that already existed at follower start is opened **at end of file**
  (old sessions are never plotted); files appearing later are read from the
  start. On switch to a newer file the old one is closed.
- Reads only complete `\n`-terminated lines; a partial trailing line is kept
  in a buffer until completed.
- Each line is `json.loads`ed; non-JSON or non-dict lines increment
  `bad_lines` and are skipped. Records with `type == "sample"` are emitted;
  events with `kind` in `connected`/`disconnected` are emitted as status.
- Runs in its own daemon thread, posting to the UI queue. No file → status
  "waiting for CPS log in <dir>".

### power.py

`PowerWorker(host, port, poll_s=0.5, retry_s=1.0)`: one daemon thread owning
one `KickrClient`.

- `request(watts)`: put into a latest-value mailbox (lock + slot); a fast
  slider drag only sends the most recent value. Never blocks the UI.
- Loop: if not connected, try to connect every 1 s (status
  "service unreachable"). When connected: send a pending set if any, then
  `read()` every 0.5 s and post the `Status` to the UI queue.
- A failed set (`KickrError` or `OSError`) posts the error and is **dropped**,
  not retried. `KickrError` from read is posted and the loop continues.
  `OSError`/`ConnectionError` (or any other failure) closes the client and goes
  back to reconnecting. A `busy` reply (another client holds the service)
  also closes the client and retries every 1 s.
- On (re)connect the worker posts `reconnected`; the UI moves the slider to 0
  (the service spun down when the previous connection dropped).
- `stop()`: sends 0 W best-effort (1 s timeout), closes the client, joins.

### graph.py

`Graph(parent, title, unit, height)`: Tk `Canvas` line chart. Fixed x axis
0–300 s with ticks every 60 s; y axis from 0 to an auto max (≥ a per-graph
floor, rounded up to a nice step, grows only within a cycle, resets on clear).
`draw(points)` redraws everything; gaps (None) break the polyline.

### app.py

Window, top to bottom:

1. **Target control**: `Scale` 0 … `--max-watts` (default 1000), resolution
   1 W, sends on every change via `PowerWorker.request`; a **0 W** button.
2. **Target panel**: requested / confirmed target (W), pending flag, link
   state, last error; graph of confirmed target.
3. **CPS panels**: power (W), speed (km/h), cadence (rpm) — value + graph each.
   If no sample has arrived for **15 s** (longer than the logger's 10-sample
   flush batches), values show "—" and a "stale" marker.
4. **Status bar**: service state, followed log file name, bad-line count.

Threads post `(kind, payload)` tuples to one `queue.Queue`; the Tk loop drains
it every 100 ms and redraws graphs every 1 s. Timestamps: target points are
stamped with `pylsl.local_clock()` at read-back; CPS points use the sample's
`t_lsl` (same clock on the same machine).

Closing the window: `PowerWorker.stop()` (0 W, close), stop the follower,
destroy. CLI: `--host`, `--port`, `--log-dir` (default `logs`),
`--max-watts`, `--circumference-m`.

### launcher.py — `wahoo-dashboard`

Arguments: `--ftms-config` (default `ftms.json`), `--cps-config` (default
`config.json`), `--log-dir` (default `logs`), `--debug` (forwarded to both
services), plus pass-through dashboard options (`--max-watts`,
`--circumference-m`). The FTMS host and port are read from the FTMS config
with the existing `load_ftms_config` (read-only use), so they cannot drift
from what the service binds; a config error exits **2** before anything starts.

The services are started through their installed console scripts, resolved
from the wrapper's own environment — `sysconfig.get_path("scripts")`
(`bin/` on POSIX, `Scripts\` on Windows), never a `PATH` search — so the
same env is used and the real entry points are exercised:

- FTMS: `<scripts>/wahoo-ftms serve -c <ftms-config>`
- CPS: `<scripts>/wahoo-lsl run -c <cps-config>`
- Dashboard: `sys.executable -m wahoo_lsl.dashboard.app --port … --log-dir …`

If either script is missing from the scripts directory, the wrapper starts
nothing and exits **2** with a message to run `pip install -e .` in that env.
(On Windows the scripts are `.exe` launchers that run Python as a child; the
group-wide `CTRL_BREAK_EVENT` reaches both, and the launcher ends its child
when terminated.)

Start-up order:

1. Start FTMS; wait up to **15 s** for its TCP port to accept connections
   (the probe is a brief client connection the service logs and ignores).
   If it exits first, stop and exit with its code; on timeout stop it, exit 2.
2. Start CPS logger. If it exits within **2 s**, stop FTMS and exit with its code.
3. Start dashboard.

Each child's stdout/stderr is merged and relayed line by line with a
`[ftms] ` / `[cps] ` / `[dash] ` prefix (one reader thread per child).

Shutdown triggers: dashboard window closed, Ctrl-C/SIGTERM in the wrapper,
or any child exiting. Order: **dashboard → FTMS → CPS**. Each child gets an
interrupt (POSIX: SIGINT, children started in their own session so a
terminal Ctrl-C reaches only the wrapper; Windows: `CTRL_BREAK_EVENT`, children started with
`CREATE_NEW_PROCESS_GROUP`) and **20 s** to exit, then `terminate()`, then
`kill()` after 5 s more. Because the dashboard closes first, its client
disconnect makes the server spin down to 0 W before the server is signalled
(this is the safety path on Windows, where the existing CLI installs no
signal handlers). Exit code: the first non-zero child exit code observed,
else 0; a second Ctrl-C skips the grace periods.

## Error handling summary

| Condition | Behaviour |
|---|---|
| FTMS service unreachable | "service unreachable", retry every 1 s |
| `KickrError` on read | shown (code + detail), polling continues |
| Set fails | error shown, set dropped (never replayed) |
| Dashboard reconnects to service | slider reset to 0 |
| No CPS log yet | "waiting for CPS log in <dir>" |
| No CPS sample for > 15 s | value "—", stale marker; graph gap (> 5 s between samples breaks the line) |
| Corrupt log line | skipped, counted in status bar |
| Child fails at start-up | others stopped, wrapper exits with its code |

## Known limitation

The logger flushes every `flush_every_n` samples (10 in the current
`config.json`), so CPS values arrive up to ~10 s late; points are still
placed at their measured times. README suggests `flush_every_n: 1` for a
livelier display. The dashboard does not change logger behaviour.

## Testing

- `metrics`: derivation incl. counter wrap, zero-Δ hold, 3 s decay, missing
  fields, first-sample priming; `History` cycle clear, multi-cycle skip,
  gaps, shared clock.
- `logtail`: existing file starts at EOF, new file from start, partial-line
  buffering, switch to newer file, bad-line counting (tmp_path).
- `power`: against `tests/ftms_stub.py` — mailbox coalescing, dropped failed
  set, reconnect posts `reconnected`, `stop()` sends 0 W.
- `launcher`: with fake child commands (small Python scripts) — start order,
  port wait success/timeout/early exit, shutdown order, exit-code selection,
  prefixed output.
- `app`/`graph`: smoke test that builds and destroys the window with fake
  worker/follower; skipped when no display is available.
- All tests: `conda run -n wahoo python -m pytest -q -W error`.
