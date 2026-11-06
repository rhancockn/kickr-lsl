# Wahoo Server: one worker process sharing one BLE connection

Amends `2026-10-27-windows-apps-design.md`. Branch `feat/windows-apps`.

## Problem

On Windows, the FTMS service and the CPS logger cannot run as two processes against the
same KICKR. Whichever connects second gets "access denied" for the other services, which
bleak skips silently (`skipping service ... due to access denied` at debug level). The CPS
logger then fails with "does not expose the Cycling Power Service" (exit 3) and the
session ends. macOS allows two processes, which is why it was not seen before.

## Decision

Wahoo Server runs **one worker process** that hosts both services in **one asyncio event
loop** and shares **one BLE connection** between them through a new shared-connection
backend. This applies on every platform, so Windows and macOS behave the same.

Constraints, carried over:
- Do not modify existing, tested code outside `src/wahoo_lsl/apps/`, `packaging/`, the
  app docs and the app tests. `src/wahoo_lsl/{ble,cli,config,…}.py`,
  `src/wahoo_lsl/ftms/*` and `src/wahoo_lsl/dashboard/*` stay untouched.
  `pyproject.toml` may be edited.
- The existing worker modes `wahoo-worker ftms …` and `wahoo-worker lsl …` stay.
- The `wahoo-dashboard` launcher, which still runs two processes, is out of scope. The
  guide notes that it cannot run both services on Windows.

## Components

```
src/wahoo_lsl/apps/shared_ble.py      SharedBackend: one real BLE link, many handles
src/wahoo_lsl/apps/server_worker.py   `wahoo-worker server`: FTMS + CPS in one loop, stdin control
src/wahoo_lsl/apps/worker_process.py  WorkerProcess: the server's child (stdin pipe, verbatim relay)
src/wahoo_lsl/apps/worker.py          + "server" mode
src/wahoo_lsl/apps/paths.py           service_commands() -> one worker command
src/wahoo_lsl/apps/supervisor.py      drives the one worker
```

### 1. SharedBackend (`shared_ble.py`)

The backend implements the existing `BleBackend` protocol (`discover`, `find_by_address`,
`make_client`). It wraps an inner backend (default `BleakBackend()`) and is used from a
single event loop.

**Handles.** `make_client(device, disconnected_callback)` returns a new `Handle`. Both
services already call `make_client` again for every (re)connect and ignore callbacks from
a client that is not their current one. The services see a handle as their client, so
they need no changes.

A handle supports what `CpsClient` and `FtmsLink` use:
- `connect()`, `disconnect()`, `is_connected`, `services`;
- `read_gatt_char`, `write_gatt_char`, `start_notify`, `stop_notify`, with the same
  signatures and keyword arguments as bleak's.

**The link.** The backend holds at most one real client (from `inner.make_client`) for
one device, plus an **epoch** counter. A handle is *attached* to the epoch current when
its `connect()` succeeded.
- `connect()`:
  - link up for the same device: attach immediately;
  - a real connect in flight: await it, then attach;
  - otherwise: create a fresh real client and connect it. A failed connect raises to
    every waiter, and leaves no link and no attached handles.
- "Same device" means the same address, compared case-insensitively. A handle for a
  different device while the link is held raises `BleakError("shared link: already
  connected to <label>; only one device is supported")`.
- `disconnect()`: detach. When the last attached handle detaches, set the link down
  *before* awaiting the real disconnect, so its callback is ignored. A disconnect on an
  unattached or stale handle is a no-op. It never raises.
- **Unexpected drop:** the real client's disconnected callback fires while the link is
  up. Mark the link down, bump the epoch, then call each attached handle's
  `disconnected_callback(handle)`, each in its own try. Handles of the old epoch are now
  stale.
- **Stale or unattached handles:** `is_connected` is False. GATT calls raise
  `BleakError("shared link: not connected")`, which falls within `ble.COMMS_ERRORS`, so the
  existing error paths apply.
- **Notifications:** `start_notify(char, cb)` subscribes the real client with `cb`
  directly. A second subscription to the same characteristic (by UUID) from another handle
  raises `BleakError`; it does not happen with FTMS + CPS. `stop_notify` on a
  characteristic this handle did not subscribe is a no-op. `disconnect()` first stops this
  handle's subscriptions, each bounded and errors ignored, if the link is still up.
- **Discovery:** an `asyncio.Lock` serialises `discover` and `find_by_address`.
  - With the link up or connecting:
    - `discover(timeout)` returns one `ScanResult` for the linked device, without
      scanning: name, address, `rssi=0`, `service_uuids` from the real client's
      services, `cps_advertised` = CPS UUID in them, `device` = the linked device.
    - `find_by_address(addr)` returns the linked device if the address matches, else
      `None`.
  - Otherwise it delegates to the inner backend.

  This means the second service never scans for, or connects to, a device that is
  already connected.

### 2. Server worker (`server_worker.py`, `wahoo-worker server`)

```
wahoo-worker server -c config.json -f ftms.json [--no-ftms] [--debug]
```

- **Config.** Loads both configs (`ConfigError` → line `[worker] config error: …`, exit 2).
  Both configs must select the same KICKR; the shared backend enforces "one device".
- **Logging.** One `logging.StreamHandler(sys.stdout)`, flushed per record, with format
  `[<label>] %(asctime)s %(levelname)-7s %(name)s: %(message)s`. Labels:
  - `ftms` for loggers under `wahoo_lsl.ftms`;
  - `worker` for `wahoo_lsl.apps`;
  - `ble` for `bleak`;
  - `cps` for everything else.

  Levels as in `cli.setup_logging`. `ServerStatus` keeps parsing `[ftms]` and `[cps]`
  lines unchanged.
- **One loop.** `asyncio.run(main_async())`.
  - One `SharedBackend` for both services.
  - The FTMS service runs `wahoo_lsl.ftms.cli._serve(cfg, backend, stop)`, private but
    built to be driven this way, as its own task with its own `stop` event.
  - The CPS logger runs `run_cps(cfg, backend, stop, debug)`, an async copy of
    `cli.run_session`: log-path pre-flight, sinks, pipeline, `CpsClient.run(stop)`, the same
    exception→exit-code mapping, the `stopped` event and the pipeline close. `run_session`
    cannot be reused because it owns its own `asyncio.run` and has no stop hook. The
    duplication, about 40 lines, is accepted.
- **Start order.** FTMS first, if enabled. CPS starts once FTMS logs `Accepting client
  commands on` or the FTMS task ends. With `--no-ftms`, CPS starts immediately.
- **Control channel.** A daemon thread reads stdin lines and hands them to the loop with
  `call_soon_threadsafe`. Commands, one per line:

  | Command | Action |
  |---|---|
  | `ftms on` | Start the FTMS task if none is running. |
  | `ftms off` | Set FTMS `stop`. `_serve` closes its server and spins down to 0 W through `controller.close`, then the handle detaches; the link stays up for CPS. |
  | `stop` | Stop FTMS first and await it, then stop CPS. |
  | EOF | Same as `stop`. This covers a server that died. |

  Unknown commands log a warning.
- **Reports.** These are the supervisor's only contract besides the log lines. Each is a
  `[worker]` line, logged at INFO through the `wahoo_lsl.apps` logger:
  - `ftms exited with code N` whenever an FTMS task ends, for any reason;
  - `cps exited with code N` when CPS ends. The worker then stops FTMS, if it is running,
    and exits with N.
- **Signals.** SIGINT, SIGTERM and SIGBREAK where available behave like `stop`. A second
  signal cancels.

### 3. WorkerProcess (`worker_process.py`)

The launcher's `Child` uses `stdin=DEVNULL` and adds a `[label]` prefix, so the worker
needs its own small class.
- **Process:** `Popen(argv, stdin=PIPE, stdout=PIPE, stderr=STDOUT, text=True,
  encoding="utf-8", errors="replace", bufsize=1)`, with `CREATE_NEW_PROCESS_GROUP` on
  Windows.
- **Relay:** a relay thread writes each line verbatim to `out` under the shared lock, then
  calls `on_line(line)`.
- **API:**
  - `send(command)` writes a line and flushes; it ignores `OSError`/`ValueError`.
  - `poll()`.
  - `stop(grace_s, kill_after_s, hurry) -> int`: send `stop`, close stdin, wait up to
    `grace_s` (or until `hurry`), then `terminate()`, wait `kill_after_s`, then `kill()`.
    Join the relay for up to 2 s and return the exit code.

### 4. Supervisor

`ServerSupervisor` keeps its public interface: the constructor, `start`, `stop` (a second
call hurries), `set_ftms`, `join`, `running`, and the posted events
`("supervisor", …)`, `("ftms", "on"|"off"|"stopped (<reason>)")`, one final
`("stopped", reason)`, and the `("line", …)` lines through the out sink.
- `commands` becomes `{"worker": argv}`. `paths.service_commands(debug, ftms_enabled)`
  builds `worker_argv("server", "-c", CPS_CONFIG, "-f", FTMS_CONFIG, ["--no-ftms"],
  ["--debug"])`.
- **Session.** Start the one worker, passing `--no-ftms` if FTMS is unticked at start.
  - **FTMS ready:** a `[ftms] … Accepting client commands on` line posts `("ftms","on")`.
    The TCP port probe is removed; it was the "client connected, then disconnected" seen in
    the FTMS log.
  - **Start-up timeout:** if no ready line arrives within `port_wait_s`, post
    `("ftms","stopped (FTMS service did not start within N s)")`, untick, and send
    `ftms off`.
  - **FTMS exit:** a `[worker] ftms exited with code N` line that the supervisor did not
    ask for posts `("ftms","stopped (FTMS service exited with code N)")` and unticks.
    During start-up the reason reads "… during start-up".
  - **Ticking:** sends `ftms on`, and the ready line then posts "on".
  - **Unticking:** sends `ftms off`, waits for `ftms exited` (up to `grace_s`), then posts
    "off". A timeout logs `[server] FTMS did not stop within N s` and still posts "off".
- **"Running"** is posted once the worker has survived `cps_early_exit_s`, as now.
- **CPS / session end.** A worker exit ends the session with the reason
  `CPS logger exited with code N`, where N is the worker's exit code. A Windows
  CTRL-exit code is cleaned as now, via the launcher's `_clean_code` rule or an equivalent.
- **Stop.** `WorkerProcess.stop(grace_s, kill_after_s, hurry)`. The worker stops FTMS (to
  0 W) before CPS.
- **0 W check removed.** The supervisor's TCP `confirm_zero` goes, because the FTMS
  spin-down now runs gracefully in-process on every stop path except kill. That path
  previously relied on a CTRL_BREAK that, on Windows, ended the child without its
  spin-down. The worker's existing log lines report the spin-down result
  (`spin-down (…): 0 W confirmed` or its warning), and the window shows them.
- Line parsing runs on the relay thread. It only sets thread-safe state (events or a
  queue) that the supervisor thread acts on.

`server_app.py` changes only where it builds the supervisor and its commands. The status
window, `ServerStatus` and the dashboard app are unchanged.

## Dropped connections (behaviour)

- **The link drops:**
  - Both services are told: the logger logs `Connection to … lost` and FTMS logs
    `FTMS connection to … lost`, so the window shows "reconnecting" for each.
  - Each runs its existing reconnect policy unchanged (backoff, `max_attempts`).
  - The first retry scans and connects; a concurrent retry waits on the discovery lock or
    on the in-flight connect, then attaches to the same link.
  - Each service re-subscribes its own characteristics.
- **One service gives up:**
  - CPS ends the session (as now).
  - FTMS reports `ftms exited with code 4`; the supervisor unticks it and CPS continues.
- **Stale callbacks** from a dropped epoch never reach a reconnected service.
- **Intentional detaches** (FTMS off, CPS session end) never look like a drop to the other
  service.

## Errors

| Situation | Result |
|---|---|
| Configs select different devices | The second service fails setup with the "only one device is supported" error; FTMS → unticked, CPS → session ends |
| Worker crashes | Session ends with "CPS logger exited with code N" |
| Server dies | stdin EOF → the worker stops FTMS (0 W), then CPS, then exits |
| `ftms off` hangs | Bounded by `controller.close`; the supervisor still posts "off" after `grace_s` |

## Testing

Tests run on macOS/Linux with fakes; `tests/fakes.py` and `tests/ftms_fakes.py` hold the
existing fake backends.
- **`test_apps_shared_ble.py`:**
  - two handles share one real connect;
  - concurrent connects coalesce;
  - a different device is refused;
  - the last detach really disconnects, with the callback ignored;
  - a drop calls every attached handle's callback once;
  - stale-handle GATT calls raise `BleakError`;
  - a stale disconnect is a no-op;
  - `discover`/`find_by_address` short-circuit while linked;
  - a duplicate subscription is refused;
  - detach stops only its own subscriptions;
  - a reconnect after a drop gives a new epoch and a new real client.
- **`test_apps_server_worker.py`,** in-process with a fake backend and stdin replaced:
  - FTMS then CPS start order;
  - `--no-ftms`;
  - `ftms off` → `ftms exited with code 0` and CPS still streaming;
  - `ftms on` again;
  - `stop` and EOF order (FTMS exit line before CPS exit line);
  - a drop with both attached → both reconnect, then one real connect;
  - FTMS gives up → `ftms exited with code 4`, CPS continues;
  - the log label mapping.
- **`test_apps_supervisor.py`,** reworked around a fake worker script (like
  `tests/dashboard_fake_child.py`, new file `tests/server_fake_worker.py`) that obeys the
  stdin protocol:
  - ready line → "on";
  - unticking → `ftms off` sent, then "off";
  - an unrequested FTMS exit → "stopped (…)" and unticked;
  - start-up timeout;
  - a worker exit ends the session;
  - a second stop hurries;
  - exactly one "stopped".
- `test_apps_paths.py`: the new `service_commands`.
- The existing app tests keep passing, or are updated only where they assert the
  two-child commands.
- **Windows manual checklist** (`docs/windows-apps.md`), added steps:
  1. Both services run together against one KICKR.
  2. Switch the KICKR off for 10 s mid-session and back on: both reconnect, no restart.
  3. Move it out of range and back: both reconnect.
  4. Untick FTMS during streaming: no gap in CPS. Another device can take control. Tick
     it again.
  5. Close the window with power set: the log shows `0 W confirmed`.

## Docs

`docs/windows-apps.md`:
- one worker process;
- the removed TCP 0 W check, and the in-process spin-down instead;
- `wahoo-dashboard` can't run both services on Windows;
- the new checklist steps.

The amended spec rows stay consistent with this one.
