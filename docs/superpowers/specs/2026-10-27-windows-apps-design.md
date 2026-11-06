# Windows Apps (Server + Dashboard) — Design

Date: 2026-10-27
Status: approved in conversation; awaiting written-spec review
Branch: `feat/windows-apps` (off `main`; `feat/ftms-remote-tls` is NOT included)

## Purpose

Package the tools as two standalone Windows apps, installed together:

- **Wahoo Server** — opening it starts the FTMS control service and the CPS logger/LSL
  streamer and shows their status in a small window. Closing it spins the trainer down and
  stops both. FTMS control can be switched off in the window (it is on at every launch), so
  another app or device can control the trainer while data logging and LSL keep running.
- **Wahoo Dashboard** — the existing test dashboard, opened only when manual control is
  needed. It reads CPS data from the LSL stream and controls the trainer through the FTMS
  service.

No Python installation is needed on the target machine.

## Constraints

- **Existing code stays untouched.** All new code goes in new files. The only edit to an
  existing file is `pyproject.toml`, to add entry points and an extra. Existing modules are
  imported and reused, never edited. The final review checks this with
  `git diff main --stat`.
- Use minimal, clean Python and the stdlib, plus the existing dependencies (`bleak`,
  `pylsl`, `tkinter`). Packaging uses `pyinstaller>=6`, as the `windows` extra, and Inno
  Setup 6, which is a build tool and not a Python dependency.
- The Windows `.exe` files and the installer can only be built and smoke-tested on Windows.
  Everything else is unit-tested on any platform.
- The two existing config files keep their formats: `config.json` for the CPS logger and
  `ftms.json` for the FTMS service.

## Architecture

### Files (all new unless noted)

```
src/wahoo_lsl/apps/__init__.py
src/wahoo_lsl/apps/paths.py           data folder + child command lines
src/wahoo_lsl/apps/worker.py          wahoo-worker: dispatch to the existing CLIs
src/wahoo_lsl/apps/server_status.py   child log line -> status (pure, no Tk)
src/wahoo_lsl/apps/supervisor.py      ServerSupervisor (drives one `server` worker; amended)
src/wahoo_lsl/apps/lsl_source.py      LslFollower (LSL inlet -> dashboard events)
src/wahoo_lsl/apps/server_app.py      Wahoo Server window
src/wahoo_lsl/apps/dashboard_app.py   Wahoo Dashboard (existing Dashboard + LslFollower)
packaging/windows/wahoo.spec          PyInstaller: 3 executables, one onedir
packaging/windows/wahoo.iss           Inno Setup installer
packaging/windows/build.ps1           venv -> pip install .[windows] -> pyinstaller -> iscc
packaging/windows/server.ico, dashboard.ico
docs/windows-apps.md                  build, install, workflow, troubleshooting, Windows checklist
pyproject.toml (edit)                 scripts + windows extra
tests/test_apps_*.py
```

### Executables

All three live in one install folder, `Program Files\Wahoo LSL\`:

| exe | subsystem | entry point | role |
|---|---|---|---|
| `WahooServer.exe` | windowed | `wahoo_lsl.apps.server_app:main` | status window + supervisor |
| `WahooDashboard.exe` | windowed | `wahoo_lsl.apps.dashboard_app:main` | dashboard window |
| `wahoo-worker.exe` | console | `wahoo_lsl.apps.worker:main` | runs the existing CLIs |

The `pyproject.toml` scripts are `wahoo-server` and `wahoo-dashboard-app`; the existing
`wahoo-dashboard` launcher is unchanged. The worker is not a console script; when not
packaged it runs as `python -m wahoo_lsl.apps.worker`.

### Worker

`wahoo-worker ftms <args…>` calls `wahoo_lsl.ftms.cli.main(<args…>)`, and
`wahoo-worker lsl <args…>` calls `wahoo_lsl.cli.main(<args…>)`. Both return that function's
exit code, with arguments passed through unchanged. Any other first argument, or none,
prints usage and exits 2. On the lab machine this also provides `wahoo-worker lsl scan` and
`list-features` for troubleshooting.

### Data folder and command lines (`paths.py`)

- `data_dir()`:
  1. `WAHOO_LSL_HOME` if set;
  2. else, on Windows, `%ProgramData%\wahoo-lsl`;
  3. else the current directory.
- `worker_argv(kind, args)`:
  - packaged (`sys.frozen`): `[<dir of sys.executable>/wahoo-worker(.exe), kind, *args]`;
  - otherwise: `[sys.executable, "-m", "wahoo_lsl.apps.worker", kind, *args]`.
- The server `chdir`s to `data_dir()` before starting children. The relative `log.path` in
  `config.json` (`logs/cps_….jsonl`) therefore lands in `<data_dir>/logs/`, and the children
  run `ftms serve -c ftms.json` and `lsl run -c config.json`.

## Wahoo Server

### Supervisor (`supervisor.py`)

> **Amended 2026-11-06:** the server now runs one `wahoo-worker server` process (FTMS and CPS
> in one loop, one shared BLE link), not two children. There is no TCP port probe and no
> `confirm_zero`; FTMS on/off go over the worker's stdin and the 0 W spin-down runs in the
> worker. The "two children", port-wait and 0 W-check text below is superseded. See `2026-11-06-shared-ble-worker-design.md`.

It reuses, without editing, from `wahoo_lsl.dashboard.launcher`:
- `Child` (start, relay output with a `[label]` prefix, CTRL_BREAK then terminate then kill);
- `Timings` / `timings_for`;
- `_clean_code` via `Child.stop`.

`Supervisor` is not reused, because it always starts the dashboard child.

`ServerSupervisor(commands, host, port, out, post, timings, confirm=confirm_zero, ftms_enabled=True)`
runs on a background thread:

- **Start:**
  1. if `ftms_enabled`: start `ftms` and wait for its port (`timings.port_wait_s`; the child
     exiting or a stop request ends the wait);
  2. start `cps`.
  An FTMS start-up failure (port timeout, or exit during start-up) turns FTMS off, posts
  `("ftms", "stopped (<reason>)")` and still starts `cps`. Only a CPS failure or a start error
  ends the session, with `stopped` and a reason.
- **FTMS on/off at runtime:** `set_ftms(enabled)` only records the request; the supervisor
  thread carries it out in its watch loop, so starts and stops never overlap.
  - **Off:** `confirm_zero(host, port)` (if the port was reached), then stop `ftms` only.
    Posts `("ftms", "off")`.
  - **On:** start `ftms` and wait for its port as at start-up (without stopping `cps`).
    Posts `("ftms", "on")`; if it exits or times out, posts
    `("ftms", "stopped (<reason>)")`.
  - The `cps` child, its log file and the LSL stream are never touched.
- **Watch:** poll the children.
  - If `cps` exits, stop `ftms` with the shutdown sequence below and end the session with
    `stopped (CPS logger exited with code N)`. The window stays open.
  - If `ftms` exits on its own, only it is gone: post
    `("ftms", "stopped (FTMS service exited with code N)")` and keep `cps` running.
    The window unticks FTMS control, and ticking it again starts the service.
- **Stop:** shutdown in this order:
  1. `confirm_zero(host, port)`, if the port was reached and FTMS is still running;
  2. stop `ftms` (if running);
  3. stop `cps`.
  Grace periods come from `Timings`. A second close request skips the grace periods
  (hurry), as in the launcher.
- **Restart** is done by the window: it stops the supervisor, waits until it is no longer
  running, re-reads `ftms.json` (host/port may have changed) and starts a new supervisor.
  One supervisor runs one session.
- `out` is a line sink: a small writer object that puts each relayed line on the window's
  queue and appends it to `<data_dir>/logs/server_<YYYYmmdd_HHMMSS>.log`.

### Graceful stop on Windows

`Child` stops a child with `CTRL_BREAK_EVENT`, which reaches only processes sharing the
caller's console. A windowed exe has none.

- At start-up on Windows, `WahooServer.exe` calls `AllocConsole()` and hides that console
  window (`GetConsoleWindow` + `ShowWindow(SW_HIDE)`) through `ctypes`, before starting any
  child.
- The console-subsystem worker children inherit it, so CTRL_BREAK reaches them and no
  console window is ever visible.
- (Superseded: there is no `confirm_zero`; the worker spins down in-process.)

### Status (`server_status.py`)

`ServerStatus` holds the fields below. Its `update(line) -> bool` method returns True when a
field changed. Each relayed line is `[ftms] …` or `[cps] …` (or `[wrapper] …` from the
supervisor) followed by the existing log text.

| field | values | set by (existing log text) |
|---|---|---|
| `ftms` | starting · ready · reconnecting · stopped · off | `FTMS control ready at 0 W` → ready; `FTMS connection to … lost` → reconnecting; `FTMS reconnect attempt` → reconnecting |
| `client` | none · `<peer>` | `Client connected: <peer>` → peer; `Client disconnected:` → none |
| `cps` | starting · streaming · reconnecting · stopped | `Streaming from …` / `Reconnected to …` → streaming; `Connection to … lost` / `Reconnect attempt` → reconnecting |
| `stream` | `—` · `<source_id>` | `LSL outlet <source_id>: …` |

The window sets `ftms` to `off` when FTMS control is switched off, to `starting` when it is
switched on, and `ftms` and `cps` to `stopped` when their child ends. A test asserts that
every pattern still appears in the source module that logs it, so a reworded message fails a
test instead of silently breaking the window.

### Window (`server_app.py`)

- **FTMS control checkbox:** "FTMS control (untick to let another app or device control the
  trainer)". Ticked at every launch; the choice is not saved.
  - **Untick:** if a client is connected, ask "A client is controlling the trainer. Turn off
    FTMS control anyway?" (No → re-tick). Then `supervisor.set_ftms(False)`; the FTMS row
    shows `off`, the client row `none`.
  - **Tick:** `supervisor.set_ftms(True)`; the FTMS row shows `starting`, then `ready`.
  - The FTMS service exiting on its own unticks the box and shows
    `stopped (FTMS service exited with code N)` in the FTMS row.
  - **Restart services** and the first start use the current checkbox value.
  - With no supervisor running (stopped or config error), toggling only changes what the
    next start uses.
- **Rows:** Trainer control (FTMS), Trainer data (CPS) + LSL stream name, FTMS client, and
  Power (live W from an `LslFollower` bound to this server's own stream: it is created with
  the `source_id` from the `LSL outlet` line, so another CPS stream on the network is never shown;
  `—` when stale for more than 15 s).
- **Log pane:** a scrolling, read-only view of the relayed lines, capped at the last 2000.
- **Buttons:**
  - **Open logs folder** (`os.startfile` on Windows; `open` / `xdg-open` elsewhere);
  - **Restart services**;
  - **Open dashboard**, which starts `WahooDashboard.exe` when packaged or
    `wahoo-dashboard-app` otherwise, as a detached process.
- **Closing:** if a client is connected, ask "A client is controlling the trainer. Stop
  anyway?". Then disable the buttons, show "Stopping: spinning down to 0 W…", run the
  supervisor's stop, and destroy the window when it finishes. A second close skips the
  grace periods.
- **Single instance:** before starting anything:
  1. take an exclusive OS lock on `<data_dir>/server.lock` (held open for the app's lifetime;
     `msvcrt.locking` on Windows, `fcntl.flock` elsewhere). If it is held, show "Wahoo Server
     is already running" and exit. This works when FTMS is off and nothing listens on the port.
  2. if a TCP connect to the FTMS `host:port` (from `ftms.json`) succeeds, show "Another
     program is using <host>:<port> (the FTMS port in ftms.json)" and exit.
- **Config errors** (`ftms.json` / `config.json` missing or invalid) are shown in the window
  with the file path. The services are not started, and Restart retries after the file is
  fixed.
- The window does **not** connect to the FTMS service, so it never takes the single client
  slot. It therefore shows power but not the FTMS target.

## Wahoo Dashboard

### `LslFollower` (`lsl_source.py`)

`LslFollower(post, source_id=None, stream_type="CPS", resolve_s=1.0, pull_s=0.5)` is a
background thread with the same `start()` / `stop()` / `post` events as `LogFollower`:

- **Resolve:** loop on `pylsl.resolve_byprop` (by `source_id` when given, else by
  `type`), posting `("cps_status", "waiting for CPS LSL stream")` until a stream is found.
  When several streams match and no `source_id` was given, use the first by `source_id` order
  and name it in the status bar: `cps_file` is posted as `"<name> (1 of <n> CPS streams)"`.
  (A `cps_status` message would be overwritten by "connected" a moment later.)
- **Open:**
  - open an inlet with `recover=False`;
  - read the channel labels from `desc/channels/channel/label`, falling back to the
    channel index;
  - post `("cps_file", <stream name>)`;
  - post `("cps_event", {"kind": "connected"})`.
- **Samples:**
  - each sample becomes `{"t_lsl": timestamp + time_correction, "fields": {label: value}}`;
  - NaN values are dropped, and float values that are whole numbers become `int` (counters,
    `power_w`);
  - posted as `("cps_sample", record)`.
- **Lost:** `pylsl.LostError` → post `("cps_event", {"kind": "disconnected"})`, then resolve
  again.
- **Stop:** `stop()` sets a flag; pulls and resolves time out within `pull_s` / `resolve_s`,
  so the thread exits promptly. `stop()` joins it.

### App (`dashboard_app.py`)

- `LslDashboard(Dashboard)` retitles the CPS frame to "CPS (LSL)" and the status bar to
  `service: … · stream: <name>` (no bad-line count; LSL has no bad lines). It overrides `_refresh_status` and
  relabels the frame after `_build`.
- `main(argv)`:
  - **Defaults** from `<data_dir>/ftms.json`: `host`, `port`, `slew_w_per_s`. A missing or
    invalid file falls back to `DEFAULT_HOST` / `DEFAULT_PORT` / 10 W/s.
  - **Defaults** from `<data_dir>/config.json`: `derive.wheel_circumference_m`, falling
    back to `DEFAULT_CIRCUMFERENCE_M`.
  - **Flags:** `--host`, `--port`, `--max-watts`, `--circumference-m`, `--slew` and
    `--stream <source_id>` override the defaults, with the same validation as `app.py`.
  - Builds `LslDashboard(root, events, PowerWorker(events.put, host, port), LslFollower(...))`
    and installs the same signal handling as `app.py`.

The CPS speed and cadence still come from the dashboard's existing `CpsDeriver`, which is
fed the counter fields from the LSL stream.

## Packaging and install

### PyInstaller (`wahoo.spec`)

- One spec builds three `EXE`s into one `COLLECT` (`onedir`): `WahooServer`
  (`console=False`, `server.ico`), `WahooDashboard` (`console=False`, `dashboard.ico`) and
  `wahoo-worker` (`console=True`).
- It collects:
  - `bleak` with its WinRT back end (`collect_submodules("bleak")` plus the `winrt` packages);
  - pylsl's `liblsl` DLL (`collect_dynamic_libs("pylsl")`).
  Tcl/Tk are included automatically.
- The version comes from `pyproject.toml`.

### Inno Setup (`wahoo.iss`) → `WahooLSL-Setup-<version>.exe`

- Installs per machine (admin) to `{autopf}\Wahoo LSL`.
- `{commonappdata}\wahoo-lsl` and `logs\`:
  - created with `users-modify` permissions;
  - `config.example.json` → `config.json` and `ftms.example.json` → `ftms.json`, with
    `onlyifdoesntexist uninsneveruninstall`;
  - left in place on uninstall, with its configs and logs.
- Start-menu shortcuts **Wahoo Server** and **Wahoo Dashboard**; optional desktop icons
  (an unchecked-by-default task).
- An inbound firewall rule for `wahoo-worker.exe` on the private profile (`netsh advfirewall`),
  removed on uninstall.

### Build (`build.ps1`)

Requires Windows, Python 3.12 and Inno Setup 7 or 6.3+ (`iscc` on PATH, or under Program Files / Program Files (x86)):

1. Create a clean venv in `build\venv`.
2. `pip install .[windows]`.
3. `pyinstaller packaging\windows\wahoo.spec`.
4. `iscc packaging\windows\wahoo.iss`.

PyInstaller output goes to `build\pyinstaller\` and the installer to
`build\installer\` (`build/` is already git-ignored). It is unsigned, so SmartScreen shows "More info → Run
anyway" the first time.

## Error handling

| Situation | Behaviour |
|---|---|
| Config missing or invalid | The server window shows the path and the error; services are not started; Restart retries |
| Server already running (lock held) | "already running" message; exit |
| FTMS port in use by another program at launch | "another program is using host:port" message; exit |
| FTMS control switched off | `ftms off` to the worker, which spins down to 0 W in-process and stops FTMS; CPS logger and LSL keep running; dashboard target panel shows the service unreachable |
| FTMS service exits on its own (or fails to start after being switched on, e.g. another device holds control) | Only FTMS stops; FTMS row shows the reason; checkbox unticked; CPS keeps running |
| FTMS never reports ready | FTMS turned off; FTMS row shows `stopped (FTMS service did not start within N s)`; checkbox unticked; CPS is still started |
| CPS logger exits (the worker exits) | The worker has already stopped FTMS (0 W in-process); the state shows the exit code; Restart available |
| Close with a client connected | Confirmation prompt, then ordered shutdown |
| Close during shutdown | Hurry: skips the grace periods |
| Dashboard: no CPS stream | CPS status "waiting for CPS LSL stream"; the FTMS panel works independently |
| Dashboard: FTMS busy or unreachable | Existing `PowerWorker` behaviour (status shown, retries) |

## Testing

**Automated (pytest, any platform, no BLE, no hardware):**
- **`server_status`:** each pattern → field change; unrelated lines → no change; the
  pattern-exists-in-source guard.
- **`supervisor`:** fake children (short Python scripts, following the existing launcher
  tests):
  - (amended: one fake worker obeying the stdin protocol, see the 2026-11-06 spec)
  - ready line, then "on"; FTMS disabled at start; an unrequested FTMS exit only unticks;
  - `set_ftms(False)` sends `ftms off`; `set_ftms(True)` sends `ftms on` (same worker);
  - a worker exit ends the session; stop sends `stop`;
  - Restart;
  - hurry on a second stop;
  - the line sink writes to the queue and the log file.
- **`lsl_source`:** a real pylsl outlet in the test process:
  - resolve by type and by `source_id`;
  - the several-streams message;
  - labels and values → fields (NaN dropped, whole floats → int);
  - connected and disconnected events when the outlet is destroyed;
  - `stop()` returns promptly;
  - `CpsDeriver` output from these records matches the same samples as log records.
- **`paths`:** `WAHOO_LSL_HOME`; ProgramData on Windows (monkeypatched `sys.platform` and
  env); `worker_argv` packaged and unpackaged (monkeypatched `sys.frozen`).
- **`worker`:** dispatch to both CLIs with the arguments unchanged (monkeypatched `main`s);
  usage for a bad kind.
- **Apps:** argument parsing and config-derived defaults; the single-instance lock and port
  check; the FTMS checkbox (untick/tick, confirmation, FTMS exit unticks it); Tk
  smoke tests (retitles; the window builds and closes) in the same style as the existing
  dashboard tests.
- The whole existing suite stays green, and `git diff main --stat` shows no existing file
  changed except `pyproject.toml`.

**Manual on Windows (checklist in `docs/windows-apps.md`):**
1. Build; install on a clean machine; SmartScreen; shortcuts.
2. Server with the trainer off: "starting / connecting"; no console window is visible.
3. Trainer on: FTMS ready, CPS streaming, LSL name, power updating.
4. Dashboard: graphs live; the slider changes the trainer's power.
5. Close the server with the dashboard connected: prompt, "spinning down…", trainer at
   0 W, no `wahoo-worker.exe` left in Task Manager.
6. Edit a config, then Restart services: the change takes effect.
7. A second server instance shows the "already running" message.
8. LabRecorder on another PC on the private network sees the stream.
9. Untick FTMS control while recording: no gap in the CPS log or LSL stream; another
   app/device (e.g. a training app) can connect to the KICKR and control resistance while
   the CPS logger stays connected. Tick it again after the other app disconnects: FTMS ready.
10. Close with FTMS control off: nothing to spin down, the worker is gone. Open a second server
    while FTMS is off: "already running".
11. Uninstall: `%ProgramData%\wahoo-lsl` configs and logs remain.

## Out of scope

- Remote TLS clients (`feat/ftms-remote-tls`); the relay idea.
- Sharing one BLE connection between FTMS and CPS.
- Code signing; auto-start at login; an in-app settings editor or config-folder button.
- macOS and Linux app bundles (both apps run unpackaged there for development).
