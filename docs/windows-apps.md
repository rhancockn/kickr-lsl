# Wahoo LSL Windows apps

## What it is

Two apps are installed together.

- **Wahoo Server** starts the FTMS control service and the CPS logger/LSL streamer, and shows their status in a small window. Both run inside one `wahoo-worker` process that shares a single Bluetooth connection to the KICKR (Windows allows only one process per KICKR).
- **Wahoo Dashboard** is the test dashboard. Open it only when you need manual control of the trainer. It reads CPS data from the LSL stream and controls the trainer through the FTMS service.

The normal workflow: open Wahoo Server and leave it open while you record. Open Wahoo Dashboard only when you need to set the trainer's power by hand. Closing Wahoo Server spins the trainer down to 0 W and then stops both services.

No Python installation is needed on the lab machine.

## Building the installer

On Windows with Python 3.12 (the `py` launcher) and Inno Setup 7 (or 6.3+) installed, from the repository root:

```
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

The script makes a clean virtual environment, installs the project with the `windows` extra, runs PyInstaller, then Inno Setup. The installer is written to `build\installer\WahooLSL-Setup-<version>.exe`.

## Installing

1. Run the installer as an administrator.
2. The installer is not code-signed, so Windows SmartScreen warns about it. Click **More info**, then **Run anyway**.
3. Optionally tick "Create desktop icons". Start menu shortcuts are always created.

| What | Where |
|---|---|
| Programs (`WahooServer.exe`, `WahooDashboard.exe`, `wahoo-worker.exe`) | `C:\Program Files\Wahoo LSL` |
| CPS logger config | `%ProgramData%\wahoo-lsl\config.json` |
| FTMS service config | `%ProgramData%\wahoo-lsl\ftms.json` |
| Logs | `%ProgramData%\wahoo-lsl\logs\` |

Upgrading keeps your two config files. Uninstalling removes the programs and the firewall rule but keeps the configs and the logs.

The data folder is `WAHOO_LSL_HOME` if that environment variable is set, otherwise `%ProgramData%\wahoo-lsl`, and on a non-Windows machine the current folder.

## First-run configuration

Edit the `device` section (`name` or `mac`) in **both** config files, `config.json` and `ftms.json`, using the same values. Then click **Restart services** in Wahoo Server.

To find the trainer, run these in a Command Prompt:

```
"C:\Program Files\Wahoo LSL\wahoo-worker.exe" lsl scan
"C:\Program Files\Wahoo LSL\wahoo-worker.exe" lsl list-features -c "%ProgramData%\wahoo-lsl\config.json"
```

By default the FTMS service accepts clients on this PC only. To let other lab PCs control the trainer, list them in `ftms.allowed_hosts` in `ftms.json` (see "Remote clients" in the README), then click **Restart services**. The installer's firewall rule already covers private networks. Client code for Python, MATLAB and Unity is in [CLIENT.md](../CLIENT.md).

## The server window

| Row | Meaning |
|---|---|
| Services | `starting` until the CPS logger has run for about 2 s, then `running`; `stopping: spinning down to 0 W…` while closing; `stopped (<reason>)`, for example `stopped (CPS logger exited with code 1)`, when a service crashes (click **Restart services** to start again); or a config error. |
| Trainer control (FTMS) | `starting`, `ready`, `reconnecting`, `stopped (<reason>)` or `off` (FTMS control unticked). |
| Trainer data (CPS) | `starting`, `streaming`, `reconnecting` or `stopped`. |
| LSL stream | The `source_id` of the stream this server publishes. |
| FTMS client | The peer connected to the FTMS service (for example the dashboard), if any. |
| Power | Latest power in W, read back from the LSL stream. |

Only one FTMS client (the dashboard, a script or MATLAB) can control the trainer at a time.

**FTMS control checkbox.** It is ticked at every launch. Untick it to let another app or device, such as a training app, control the trainer while logging and LSL continue.

- Unticking asks for confirmation if a client is connected, spins the trainer down to 0 W inside the worker, and stops only the FTMS service. The CPS logger and LSL stream keep running with no gap.
- Unticking keeps the worker's BLE link to the KICKR up, because the CPS logger needs it. Control therefore passes to another app by that app taking control (the KICKR hands it over), not by this one disconnecting.
- Tick it again once the other app has disconnected; the FTMS service starts again and the row returns to `ready`.
- Ticking while nothing is running only changes what the next start uses.
- If the FTMS service stops on its own, the box unticks itself.

**Buttons.**

- **Open logs folder** opens the `logs` folder.
- **Restart services** stops both services in order and starts them again, re-reading `config.json` and `ftms.json`.
- **Open dashboard** starts Wahoo Dashboard.

**Closing.** If an FTMS client is connected you are asked to confirm. The server then tells the worker to stop: it spins the trainer down to 0 W, stops the FTMS service, then the CPS logger. A second close while it is waiting skips the waits. Closing during a restart is still an ordered stop.

**Spin-down to 0 W.** The FTMS service spins the trainer down in-process when it stops, whether you close the window, untick FTMS control or the server dies, and the log shows the result: `spin-down (...): 0 W confirmed` or a warning. Only a forced kill skips it: ending the worker in Task Manager, or a hung BLE stop that outlasts the grace period (the worker is then terminated hard, with no spin-down), or a second close while the server is still waiting (that terminates the worker immediately and can skip the 0 W spin-down).

**One worker, one Bluetooth link.** If the link drops, both services reconnect on their own onto one new connection; the rows show `reconnecting` meanwhile. If one service gives up, FTMS unticks itself and the CPS logger carries on; if the CPS logger gives up, the session ends.

**Config errors.** If a config file is missing or invalid, the Services row shows the error. Fix the file and click **Restart services**.

**Single instance.** Two different messages can appear when you open the server, and the second window then exits.

- "Wahoo Server is already running." Another server holds the lock file `server.lock` in the data folder.
- "Another program is using host:port (the FTMS port in ftms.json)." Something else is listening on the FTMS port.

## The dashboard

Wahoo Dashboard takes its defaults from the server's configs: FTMS host, port and slew rate come from `ftms.json`, and wheel circumference from `config.json`. It follows the first CPS LSL stream it finds, and the status bar names it. With several streams the status bar shows "(1 of N CPS streams)". To choose one, run it with `--stream <source_id>`, using the source id shown in the server's LSL stream row. `--host`, `--port`, `--max-watts`, `--circumference-m` and `--slew` override the other defaults.

## Logs

Logs are in `%ProgramData%\wahoo-lsl\logs\`.

- `server_<time>.log`: the output of the worker (`[ftms]`, `[cps]`, `[worker]` and `[ble]` lines), one file per server run.
- `cps_<time>.jsonl`: the CPS data, one file per CPS logger start.

## Firewall and LSL

The installer adds an inbound allow rule named "Wahoo LSL worker" for `wahoo-worker.exe` on **private** networks, so LabRecorder on another PC on the private network can see the stream without a firewall prompt. Uninstalling removes the rule. On a public network the rule does not apply.

## Limitations

`wahoo-dashboard` (the command-line launcher, not the apps) still runs two processes, so on Windows it cannot run both services against one KICKR. Use Wahoo Server.

## Developing on macOS/Linux

`wahoo-server` and `wahoo-dashboard-app` run unpackaged after `pip install -e .`. They use `WAHOO_LSL_HOME` if set, otherwise the current folder, for configs and logs. The worker runs as `python -m wahoo_lsl.apps.worker`.
