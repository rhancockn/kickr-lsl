# Windows Apps (Server + Dashboard) — Design

Date: 2026-10-27
Status: approved in conversation; awaiting written-spec review
Branch: `feat/windows-apps` (off `main`; `feat/ftms-remote-tls` is NOT included)

## Purpose

Package the tools as two standalone Windows apps, installed together:

- **Wahoo Server** — opening it starts the FTMS control service and the CPS logger/LSL
  streamer and shows their status in a small window. Closing it spins the trainer down and
  stops both.
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
src/wahoo_lsl/apps/supervisor.py      ServerSupervisor (reuses launcher.Child/confirm_zero)
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

It reuses, without editing, from `wahoo_lsl.dashboard.launcher`:
- `Child` (start, relay output with a `[label]` prefix, CTRL_BREAK then terminate then kill);
- `confirm_zero`;
- `Timings` / `timings_for`;
- `_clean_code` via `Child.stop`.

`Supervisor` is not reused, because it always starts the dashboard child.

`ServerSupervisor(commands, host, port, out, timings, confirm=confirm_zero)` runs on a
background thread:

- **Start:**
  1. start `ftms`;
  2. wait for its port (`timings.port_wait_s`; the child exiting or a stop request ends the wait);
  3. start `cps`.
  A failure leaves the state at `stopped` with a reason.
- **Watch:** poll the children. If either exits, stop the other with the shutdown sequence
  below and set the state to `stopped (<label> exited with code N)`. The window stays open.
- **Stop:** shutdown in this order:
  1. `confirm_zero(host, port)`, if the port was reached and FTMS is still running;
  2. stop `ftms`;
  3. stop `cps`.
  Grace periods come from `Timings`. A second close request skips the grace periods
  (hurry), as in the launcher.
- **Restart:** stop, then start, with the same commands. Configs are re-read by the children.
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
- `confirm_zero` before stopping FTMS remains the safety net if a child must be killed.

### Status (`server_status.py`)

`ServerStatus` holds the fields below. Its `update(line) -> bool` method returns True when a
field changed. Each relayed line is `[ftms] …` or `[cps] …` (or `[wrapper] …` from the
supervisor) followed by the existing log text.

| field | values | set by (existing log text) |
|---|---|---|
| `ftms` | starting · ready · reconnecting · stopped | `FTMS control ready at 0 W` → ready; `FTMS connection to … lost` → reconnecting; `FTMS reconnect attempt` → reconnecting |
| `client` | none · `<peer>` | `Client connected: <peer>` → peer; `Client disconnected:` → none |
| `cps` | starting · streaming · reconnecting · stopped | `Streaming from …` / `Reconnected to …` → streaming; `Connection to … lost` / `Reconnect attempt` → reconnecting |
| `stream` | `—` · `<source_id>` | `LSL outlet <source_id>: …` |

The supervisor sets `ftms` and `cps` to `stopped` when their child ends. A test asserts that
every pattern still appears in the source module that logs it, so a reworded message fails a
test instead of silently breaking the window.

### Window (`server_app.py`)

- **Rows:** Trainer control (FTMS), Trainer data (CPS) + LSL stream name, FTMS client, and
  Power (live W from `LslFollower`; `—` when stale for more than 15 s).
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
- **Single instance:** before starting anything, if a TCP connect to the FTMS
  `host:port` (from `ftms.json`) succeeds, show "Wahoo Server is already running (or
  another program is using <host>:<port>)" and exit.
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
  When several streams match and no `source_id` was given, use the first and post
  `("cps_status", "<n> CPS streams, using <source_id>")`.
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
  `service: … · stream: <name> · bad lines: …`. It overrides `_refresh_status` and
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

Requires Windows, Python 3.12 and Inno Setup 6 (`iscc` on PATH, or the default install path):

1. Create a clean venv in `build\venv`.
2. `pip install .[windows]`.
3. `pyinstaller packaging\windows\wahoo.spec`.
4. `iscc packaging\windows\wahoo.iss`.

The installer ends up in `dist\`. It is unsigned, so SmartScreen shows "More info → Run
anyway" the first time.

## Error handling

| Situation | Behaviour |
|---|---|
| Config missing or invalid | The server window shows the path and the error; services are not started; Restart retries |
| FTMS port in use at launch | "already running" message; exit |
| FTMS never opens its port | `stopped (FTMS service did not open host:port within N s)`; CPS is not started |
| A child exits | The other is stopped (FTMS first with the 0 W check); the state shows which one and its exit code; Restart available |
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
  - start order, and the port wait before CPS;
  - a child exit stops the other and sets the state;
  - stop order with the 0 W check called before FTMS stops;
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
- **Apps:** argument parsing and config-derived defaults; the single-instance check; Tk
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
9. Uninstall: `%ProgramData%\wahoo-lsl` configs and logs remain.

## Out of scope

- Remote TLS clients (`feat/ftms-remote-tls`); the relay idea.
- Sharing one BLE connection between FTMS and CPS.
- Code signing; auto-start at login; an in-app settings editor or config-folder button.
- macOS and Linux app bundles (both apps run unpackaged there for development).
