# Windows Apps (Server + Dashboard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two Windows apps from one installer. Wahoo Server supervises the FTMS service and the CPS logger/LSL streamer and shows their status. Wahoo Dashboard is the existing dashboard fed by the CPS LSL stream.

**Architecture:**
- New package `wahoo_lsl.apps`:
  - `paths`, `worker`, `server_status`, `supervisor`, `lsl_source`, `dashboard_app`, `server_app`.
  - It reuses the existing CLIs, `dashboard.launcher.Child` / `confirm_zero`, and `dashboard.app.Dashboard` by import only.
- `packaging/windows/`: one PyInstaller spec (three executables, one onedir folder) and an Inno Setup installer.

**Tech Stack:** Python 3.12, stdlib (`tkinter`, `subprocess`, `threading`, `ctypes`), `pylsl` 1.18, `bleak`; PyInstaller ≥ 6; Inno Setup 6.

**Spec:** `docs/superpowers/specs/2026-10-27-windows-apps-design.md`

## Global Constraints

- **Do not modify any existing file except `pyproject.toml`** (Task 1 only). That includes existing tests, test helpers, README, `.gitignore`, and existing modules. New code goes in new files only. Existing test helpers (e.g. `tests/dashboard_fake_child.py`) may be *used*, not edited.
- Use minimal, clean Python that matches the surrounding style: `from __future__ import annotations`, module docstrings, type hints, small functions, comments only where non-obvious.
- Stdlib plus the existing dependencies only. `pyinstaller>=6` goes in a new `windows` extra.
- Conda env: `source "$(conda info --base)/etc/profile.d/conda.sh" && conda activate wahoo`. Tests: `python -m pytest -q`. The baseline is 583 passed on `main`.
- Never run `wahoo-dashboard` (except `--help`), `wahoo-ftms serve`, `wahoo-lsl run/scan/list-features`, `wahoo-server`, `wahoo-dashboard-app`, or any GUI app outside pytest. Real BLE hardware and real windows are off limits. Tk tests use a withdrawn root and skip without a display.
- Never add, modify or delete the user's untracked `config.json`, `config.json~`, `ftms.json` or `logs/`. Tests use `tmp_path` and copy `config.example.json` / `ftms.example.json`.
- Commit trailer exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Data folder: `WAHOO_LSL_HOME`, else `%ProgramData%\wahoo-lsl` on Windows, else the current directory. Config names: `ftms.json`, `config.json`. Logs: `<data_dir>/logs/`.

## Review Focus

1. **A config edited to invalid JSON, then Restart services.** The window shows the error with the file path, nothing starts, and Restart works once the file is fixed (Task 6 tests).
2. **`ftms.json` host `0.0.0.0` (or `::`).** The single-instance check, `confirm_zero`, and the dashboard connect to `127.0.0.1` (or `::1`), never to the wildcard address, which fails on Windows (Tasks 1, 5, 6).
3. **Other CPS streams on the lab network.** The server window shows only its own stream's power, bound by `source_id`. The dashboard names the stream it picked and says how many it saw (Tasks 4, 6).
4. **A child crashes mid-session.** The other child is stopped, FTMS first with the 0 W check. The window stays open showing which child exited and its exit code, and Restart works (Tasks 3, 6).
5. **FTMS control switched off and on during a recording.** The CPS logger process, its log file and the LSL stream continue untouched; only the FTMS child stops and starts. An FTMS failure while switching on unticks the box rather than ending the session (Tasks 3, 6).
6. **The close button pressed twice during a slow shutdown.** The second press skips the grace periods instead of being ignored or stopping twice (Tasks 3, 6).

The Windows-only paths (hidden console + CTRL_BREAK, PyInstaller collection, installer) cannot run here. They are covered by the manual checklist in `docs/windows-apps.md` (Task 7).

---

### Task 1: Data folder, child command lines, worker, entry points

**Files:**
- Create: `src/wahoo_lsl/apps/__init__.py`, `src/wahoo_lsl/apps/paths.py`, `src/wahoo_lsl/apps/worker.py`
- Modify: `pyproject.toml` (scripts + `windows` extra)
- Test: `tests/test_apps_paths.py`

**Interfaces:**
- Produces:
  - `paths.HOME_ENV = "WAHOO_LSL_HOME"`, `FTMS_CONFIG = "ftms.json"`, `CPS_CONFIG = "config.json"`, `LOG_DIR = "logs"`
  - `data_dir() -> Path`, `frozen() -> bool`
  - `worker_argv(kind: str, *args: str) -> list[str]`
  - `service_commands(debug: bool = False) -> dict[str, list[str]]` (keys `"ftms"`, `"cps"`)
  - `dashboard_argv() -> list[str]`
  - `connect_host(host: str) -> str`
  - `worker.main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_paths.py`:

```python
import sys
from pathlib import Path

from wahoo_lsl.apps import paths, worker


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("WAHOO_LSL_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_data_dir_programdata_on_windows(monkeypatch):
    monkeypatch.delenv("WAHOO_LSL_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramData", "PD")
    assert paths.data_dir() == Path("PD") / "wahoo-lsl"


def test_data_dir_is_cwd_elsewhere(monkeypatch, tmp_path):
    monkeypatch.delenv("WAHOO_LSL_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.chdir(tmp_path)
    assert paths.data_dir() == Path.cwd()


def test_worker_argv_unpackaged(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.worker_argv("ftms", "serve") == [sys.executable, "-m", "wahoo_lsl.apps.worker", "ftms", "serve"]


def test_worker_argv_packaged_uses_sibling_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WahooServer.exe"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths.worker_argv("lsl", "run") == [str(tmp_path / "wahoo-worker.exe"), "lsl", "run"]


def test_service_commands(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    head = [sys.executable, "-m", "wahoo_lsl.apps.worker"]
    assert paths.service_commands() == {
        "ftms": [*head, "ftms", "serve", "-c", "ftms.json"],
        "cps": [*head, "lsl", "run", "-c", "config.json"],
    }
    debug = paths.service_commands(debug=True)
    assert debug["ftms"][-1] == "--debug" and debug["cps"][-1] == "--debug"


def test_dashboard_argv(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.dashboard_argv() == [sys.executable, "-m", "wahoo_lsl.apps.dashboard_app"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WahooServer.exe"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths.dashboard_argv() == [str(tmp_path / "WahooDashboard.exe")]


def test_connect_host_maps_wildcards_to_loopback():
    assert paths.connect_host("0.0.0.0") == "127.0.0.1"
    assert paths.connect_host("::") == "::1"
    assert paths.connect_host("192.168.1.5") == "192.168.1.5"


def test_worker_dispatches_ftms(monkeypatch):
    from wahoo_lsl.ftms import cli as ftms_cli
    calls = []
    monkeypatch.setattr(ftms_cli, "main", lambda argv: calls.append(argv) or 7)
    assert worker.main(["ftms", "serve", "-c", "ftms.json"]) == 7
    assert calls == [["serve", "-c", "ftms.json"]]


def test_worker_dispatches_lsl(monkeypatch):
    from wahoo_lsl import cli as lsl_cli
    calls = []
    monkeypatch.setattr(lsl_cli, "main", lambda argv: calls.append(argv) or 0)
    assert worker.main(["lsl", "scan"]) == 0
    assert calls == [["scan"]]


def test_worker_usage(capsys):
    assert worker.main([]) == 2
    assert worker.main(["nope"]) == 2
    assert "usage: wahoo-worker {ftms|lsl}" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_paths.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.apps'`.

- [ ] **Step 3: Implement**

`src/wahoo_lsl/apps/__init__.py`:

```python
"""Standalone apps: Wahoo Server (FTMS service + CPS logger with a status window) and Wahoo Dashboard."""
```

`src/wahoo_lsl/apps/paths.py`:

```python
"""Where the apps keep configs and logs, and the command lines of the processes they start."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HOME_ENV = "WAHOO_LSL_HOME"
FTMS_CONFIG = "ftms.json"
CPS_CONFIG = "config.json"
LOG_DIR = "logs"
_WILDCARDS = {"0.0.0.0": "127.0.0.1", "::": "::1"}


def data_dir() -> Path:
    """WAHOO_LSL_HOME, else %ProgramData%\\wahoo-lsl on Windows, else the current directory."""
    if os.environ.get(HOME_ENV):
        return Path(os.environ[HOME_ENV])
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "wahoo-lsl"
    return Path.cwd()


def frozen() -> bool:
    """True inside the PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def _sibling(name: str) -> str:
    return str(Path(sys.executable).with_name(f"{name}.exe" if sys.platform == "win32" else name))


def worker_argv(kind: str, *args: str) -> list[str]:
    if frozen():
        return [_sibling("wahoo-worker"), kind, *args]
    return [sys.executable, "-m", "wahoo_lsl.apps.worker", kind, *args]


def service_commands(debug: bool = False) -> dict[str, list[str]]:
    """The server's children; config paths are relative to data_dir(), the server's working directory."""
    extra = ["--debug"] if debug else []
    return {
        "ftms": worker_argv("ftms", "serve", "-c", FTMS_CONFIG, *extra),
        "cps": worker_argv("lsl", "run", "-c", CPS_CONFIG, *extra),
    }


def dashboard_argv() -> list[str]:
    if frozen():
        return [_sibling("WahooDashboard")]
    return [sys.executable, "-m", "wahoo_lsl.apps.dashboard_app"]


def connect_host(host: str) -> str:
    """The address a local client dials for a service bound to `host` (wildcards -> loopback)."""
    return _WILDCARDS.get(host, host)
```

`src/wahoo_lsl/apps/worker.py`:

```python
"""wahoo-worker: run the FTMS service (`ftms ...`) or the CPS logger (`lsl ...`) through their existing CLIs."""
from __future__ import annotations

import sys
from typing import Sequence

USAGE = "usage: wahoo-worker {ftms|lsl} <arguments>"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("ftms", "lsl"):
        print(USAGE, file=sys.stderr)
        return 2
    if args[0] == "ftms":
        from wahoo_lsl.ftms import cli
    else:
        from wahoo_lsl import cli
    return cli.main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
```

`pyproject.toml`: add to `[project.scripts]`:

```toml
wahoo-server = "wahoo_lsl.apps.server_app:main"
wahoo-dashboard-app = "wahoo_lsl.apps.dashboard_app:main"
```

and to `[project.optional-dependencies]`:

```toml
windows = ["pyinstaller>=6"]
```

Then `python -m pip install -e .` in the wahoo env, so the scripts are registered. They point at modules created in Tasks 5–6; that's fine.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_apps_paths.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, commit**

```bash
python -m pytest -q
git add src/wahoo_lsl/apps/__init__.py src/wahoo_lsl/apps/paths.py src/wahoo_lsl/apps/worker.py pyproject.toml tests/test_apps_paths.py
git commit -m "feat(apps): data folder, child command lines and wahoo-worker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Server status from child log lines

**Files:**
- Create: `src/wahoo_lsl/apps/server_status.py`
- Test: `tests/test_apps_server_status.py`

**Interfaces:**
- Produces:
  - `RULES`
  - `ServerStatus` dataclass: fields `ftms: str = "starting"`, `cps: str = "starting"`, `client: str = ""`, `stream: str = ""`
  - methods `update(line: str) -> bool`, `starting(ftms_enabled: bool = True) -> None`, `stopped() -> None`
  - `ftms` also takes the value `"off"`, which the window sets when FTMS control is switched off.
- A relayed line looks like `[ftms] 2026-10-28 10:00:00,000 INFO    wahoo_lsl.ftms.server: Client connected: ('127.0.0.1', 50000)`. The label is `ftms`, `cps` or `server`.

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_server_status.py`:

```python
from pathlib import Path

import pytest

from wahoo_lsl.apps.server_status import ServerStatus

SRC = Path(__file__).resolve().parents[1] / "src" / "wahoo_lsl"
PREFIX = "2026-10-28 10:00:00,000 INFO    wahoo_lsl.x: "

# (format string as logged, file that logs it, child label, example message, field, expected value)
SOURCES = [
    ("FTMS control ready at 0 W (link_epoch %d)", "ftms/controller.py", "ftms",
     "FTMS control ready at 0 W (link_epoch 1)", "ftms", "ready"),
    ("FTMS connection to %s lost", "ftms/link.py", "ftms", "FTMS connection to KICKR lost", "ftms", "reconnecting"),
    ("FTMS reconnect attempt %d (after %.1fs)", "ftms/controller.py", "ftms",
     "FTMS reconnect attempt 2 (after 1.0s)", "ftms", "reconnecting"),
    ("Client connected: %s", "ftms/server.py", "ftms", "Client connected: ('127.0.0.1', 5000)",
     "client", "('127.0.0.1', 5000)"),
    ("Streaming from %s (%s %s)", "ble.py", "cps", "Streaming from KICKR (mac AA:BB)", "cps", "streaming"),
    ("Reconnected to %s", "ble.py", "cps", "Reconnected to KICKR", "cps", "streaming"),
    ("Connection to %s lost", "ble.py", "cps", "Connection to KICKR lost", "cps", "reconnecting"),
    ("Reconnect attempt %d (after %.1fs)", "ble.py", "cps", "Reconnect attempt 3 (after 5.0s)", "cps", "reconnecting"),
    ("LSL outlet %s: %d channels (%s)", "sinks/lsl_sink.py", "cps",
     "LSL outlet KICKR-CPS-f24bfa: 3 channels (power_w, wheel_revs, wheel_event_t)", "stream", "KICKR-CPS-f24bfa"),
]


@pytest.mark.parametrize("fmt, source, label, message, field, value", SOURCES)
def test_pattern_is_still_logged_and_recognised(fmt, source, label, message, field, value):
    assert fmt in (SRC / source).read_text(encoding="utf-8"), f"log message changed in {source}: update server_status"
    st = ServerStatus()
    assert st.update(f"[{label}] {PREFIX}{message}")
    assert getattr(st, field) == value


def test_client_disconnected_clears_client():
    assert "Client disconnected: %s" in (SRC / "ftms/server.py").read_text(encoding="utf-8")
    st = ServerStatus(client="('127.0.0.1', 5000)")
    assert st.update(f"[ftms] {PREFIX}Client disconnected: ('127.0.0.1', 5000)")
    assert st.client == ""


def test_lines_from_the_other_child_are_ignored():
    st = ServerStatus()
    assert not st.update(f"[cps] {PREFIX}FTMS control ready at 0 W (link_epoch 1)")
    assert not st.update(f"[ftms] {PREFIX}Streaming from KICKR (mac AA:BB)")
    assert (st.ftms, st.cps) == ("starting", "starting")


def test_unrelated_and_malformed_lines_change_nothing():
    st = ServerStatus()
    for line in ("", "no label", f"[ftms] {PREFIX}Accepting client commands on 127.0.0.1:47610", "[server] hello"):
        assert not st.update(line)
    assert st == ServerStatus()


def test_repeated_line_reports_no_change():
    st = ServerStatus()
    line = f"[ftms] {PREFIX}FTMS control ready at 0 W (link_epoch 1)"
    assert st.update(line)
    assert not st.update(line)


def test_starting_and_stopped_keep_the_stream_name():
    st = ServerStatus(ftms="ready", cps="streaming", client="peer", stream="KICKR-CPS-f24bfa")
    st.stopped()
    assert (st.ftms, st.cps, st.client, st.stream) == ("stopped", "stopped", "", "KICKR-CPS-f24bfa")
    st.starting()
    assert (st.ftms, st.cps, st.client) == ("starting", "starting", "")
    st.starting(ftms_enabled=False)
    assert (st.ftms, st.cps) == ("off", "starting")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_server_status.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.apps.server_status'`.

- [ ] **Step 3: Implement** `src/wahoo_lsl/apps/server_status.py`:

```python
"""Wahoo Server status from the children's relayed log lines ("[ftms] ...", "[cps] ..."). Pure, no Tk.

The patterns match existing log messages; tests/test_apps_server_status.py fails if one is reworded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# (child label, pattern in that child's log text, field, value; None = the pattern's first group)
RULES: tuple[tuple[str, re.Pattern[str], str, str | None], ...] = (
    ("ftms", re.compile(r"FTMS control ready at 0 W"), "ftms", "ready"),
    ("ftms", re.compile(r"FTMS connection to .+ lost"), "ftms", "reconnecting"),
    ("ftms", re.compile(r"FTMS reconnect attempt \d+"), "ftms", "reconnecting"),
    ("ftms", re.compile(r"Client connected: (.+)"), "client", None),
    ("ftms", re.compile(r"Client disconnected: "), "client", ""),
    ("cps", re.compile(r"Streaming from "), "cps", "streaming"),
    ("cps", re.compile(r"Reconnected to "), "cps", "streaming"),
    ("cps", re.compile(r"Connection to .+ lost"), "cps", "reconnecting"),
    ("cps", re.compile(r"Reconnect attempt \d+"), "cps", "reconnecting"),
    ("cps", re.compile(r"LSL outlet (\S+): \d+ channels"), "stream", None),
)


@dataclass
class ServerStatus:
    ftms: str = "starting"  # starting | ready | reconnecting | stopped | off
    cps: str = "starting"  # starting | streaming | reconnecting | stopped
    client: str = ""  # peer of the connected FTMS client; "" = none
    stream: str = ""  # LSL source_id of this server's CPS stream; "" = not yet known

    def update(self, line: str) -> bool:
        """Apply one relayed line; True if any field changed."""
        if not line.startswith("[") or "] " not in line:
            return False
        label, text = line[1:].split("] ", 1)
        changed = False
        for child, pattern, field, value in RULES:
            match = pattern.search(text) if child == label else None
            if match is None:
                continue
            new = match.group(1) if value is None else value
            if getattr(self, field) != new:
                setattr(self, field, new)
                changed = True
        return changed

    def starting(self, ftms_enabled: bool = True) -> None:
        self.ftms = "starting" if ftms_enabled else "off"
        self.cps = "starting"
        self.client = ""

    def stopped(self) -> None:
        self.ftms = self.cps = "stopped"
        self.client = ""
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_apps_server_status.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, commit**

```bash
python -m pytest -q
git add src/wahoo_lsl/apps/server_status.py tests/test_apps_server_status.py
git commit -m "feat(apps): server status from child log lines

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: ServerSupervisor and LineSink

**Files:**
- Create: `src/wahoo_lsl/apps/supervisor.py`
- Test: `tests/test_apps_supervisor.py`. It uses the existing `tests/dashboard_fake_child.py` unchanged. If the off→on test needs a fake that sets `SO_REUSEADDR` to rebind its port, add a **new** helper `tests/apps_fake_child.py` (a copy plus that one line) rather than editing the existing one.

**Interfaces:**
- Consumes: `wahoo_lsl.dashboard.launcher.Child(label, argv, out, out_lock)` with `.poll()`, `.proc` and `.stop(grace_s, kill_after_s, hurry) -> int`; `Timings(port_wait_s, cps_early_exit_s, grace_s, kill_after_s, poll_s)`; `confirm_zero(host, port, ..., say=...)`.
- Produces:
  - `LineSink(post, log_path)` with `write`, `flush`, `close`.
  - `ServerSupervisor(commands, host, port, out, post, timings=Timings(), confirm=None, ftms_enabled=True)` with `start()`, `stop()`, `set_ftms(enabled)`, `join(timeout)`, `running`.
  - Events it posts:
    - `("supervisor", "starting" | "running" | "stopping")`
    - `("ftms", "on" | "off" | "stopped (<reason>)")` for the FTMS service only
    - exactly one final `("stopped", reason)`
  - One instance runs one session; the window builds a new one to restart.
- Rules:
  - A CPS exit ends the session, stopping FTMS first with the 0 W check.
  - An FTMS exit or start failure stops only FTMS: it posts `("ftms", "stopped (...)")`, and FTMS stays off until `set_ftms(True)`.
  - `set_ftms` never touches the CPS child.

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_supervisor.py`:

```python
import io
import socket
import sys
import threading
import time
from pathlib import Path

from wahoo_lsl.apps.supervisor import LineSink, ServerSupervisor
from wahoo_lsl.dashboard.launcher import Timings

FAKE = str(Path(__file__).with_name("dashboard_fake_child.py"))
FAST = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake(label, order, *mode):
    return [sys.executable, FAKE, label, str(order), *map(str, mode)]


def order(path):
    return path.read_text().split() if path.exists() else []


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


class Events(list):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def __call__(self, event):
        with self._lock:
            self.append(event)

    def of(self, kind):
        return [p for k, p in list(self) if k == kind]


def make(commands, port, timings=FAST, confirm=None, ftms_enabled=True):
    events, out = Events(), io.StringIO()
    sup = ServerSupervisor(commands, "127.0.0.1", port, out, events, timings,
                           confirm=confirm if confirm is not None else (lambda h, p: None),
                           ftms_enabled=ftms_enabled)
    return sup, events, out


def happy(tmp_path, port, cps_mode=()):
    o = tmp_path / "order"
    return o, {"ftms": fake("ftms", o, "listen", port), "cps": fake("cps", o, *cps_mode)}


def test_starts_ftms_then_cps_and_stops_ftms_first(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    sup, events, out = make(cmds, port, confirm=lambda h, p: calls.append(order(o)))
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    wait_for(lambda: "[cps] cps started" in out.getvalue())
    assert out.getvalue().index("[ftms] ftms started") < out.getvalue().index("[cps] cps started")
    assert events.of("ftms") == ["on"]
    sup.stop()
    sup.join(30)
    assert not sup.running
    assert calls == [[]]  # 0 W check ran before either child stopped
    assert order(o) == ["ftms", "cps"]
    assert ("supervisor", "stopping") in events
    assert events.of("stopped") == ["stopped"]


def test_ftms_disabled_runs_cps_only(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    sup, events, out = make(cmds, port, confirm=lambda h, p: calls.append(1), ftms_enabled=False)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    sup.stop()
    sup.join(30)
    assert "[ftms]" not in out.getvalue()
    assert calls == [] and order(o) == ["cps"]


def test_cps_exit_ends_the_session_and_stops_ftms(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, cps_mode=("exit", 3, 0.3))
    sup, events, _ = make(cmds, port)
    sup.start()
    sup.join(30)
    assert events.of("stopped") == ["CPS logger exited with code 3"]
    assert order(o) == ["ftms"]  # FTMS was stopped gracefully


def test_ftms_exit_during_startup_keeps_cps(tmp_path):
    port, o = free_port(), tmp_path / "order"
    sup, events, out = make({"ftms": fake("ftms", o, "exit", 4, 0.1), "cps": fake("cps", o)}, port)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    assert events.of("ftms") == ["stopped (FTMS service exited with code 4 during start-up)"]
    assert sup.running
    sup.stop()
    sup.join(30)
    assert order(o) == ["cps"]
    assert events.of("stopped") == ["stopped"]


def test_ftms_exit_while_running_keeps_cps(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    sup, events, _ = make(cmds, port)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    sup.children["ftms"].proc.kill()
    wait_for(lambda: len(events.of("ftms")) == 2)
    assert events.of("ftms")[1].startswith("stopped (FTMS service exited with code ")
    assert sup.running and "ftms" not in sup.children
    sup.stop()
    sup.join(30)
    assert order(o) == ["cps"]


def test_port_timeout_turns_ftms_off_without_zero_check(tmp_path):
    port, o = free_port(), tmp_path / "order"
    calls = []
    t = Timings(port_wait_s=0.5, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)
    sup, events, _ = make({"ftms": fake("ftms", o), "cps": fake("cps", o)}, port, t,
                          confirm=lambda h, p: calls.append(1))
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    assert events.of("ftms") == [f"stopped (FTMS service did not open 127.0.0.1:{port} within 0.5 s)"]
    assert order(o) == ["ftms"]  # the silent FTMS child was stopped
    sup.stop()
    sup.join(30)
    assert calls == [] and order(o) == ["ftms", "cps"]


def test_set_ftms_off_then_on_keeps_the_same_cps_process(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    sup, events, _ = make(cmds, port, confirm=lambda h, p: calls.append(1))
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    cps_pid = sup.children["cps"].proc.pid
    sup.set_ftms(False)
    wait_for(lambda: events.of("ftms") == ["on", "off"])
    assert calls == [1] and order(o) == ["ftms"]
    sup.set_ftms(True)
    wait_for(lambda: events.of("ftms") == ["on", "off", "on"])
    assert sup.children["cps"].proc.pid == cps_pid
    sup.stop()
    sup.join(30)
    assert order(o) == ["ftms", "ftms", "cps"]


def test_second_stop_skips_grace(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, cps_mode=("ignore",))
    t = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=60.0, kill_after_s=2.0, poll_s=0.05)
    sup, events, _ = make(cmds, port, t)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    time.sleep(0.3)  # let the cps child install its ignore handler
    t0 = time.monotonic()
    sup.stop()
    wait_for(lambda: order(o) == ["ftms"])
    sup.stop()
    sup.join(30)
    assert time.monotonic() - t0 < 15
    assert events.of("stopped") == ["stopped"]


def test_start_error_is_reported(tmp_path):
    port = free_port()
    sup, events, _ = make({"ftms": [str(tmp_path / "missing-exe")], "cps": ["x"]}, port)
    sup.start()
    sup.join(30)
    [reason] = events.of("stopped")
    assert reason.startswith("error: ")


def test_line_sink_posts_lines_and_appends_to_file(tmp_path):
    posted = []
    sink = LineSink(posted.append, tmp_path / "logs" / "server.log")
    sink.write("[ftms] a\n")
    sink.write("[cps] b\n[cps] c\n")
    sink.flush()
    sink.close()
    assert posted == [("line", "[ftms] a"), ("line", "[cps] b"), ("line", "[cps] c")]
    assert (tmp_path / "logs" / "server.log").read_text(encoding="utf-8") == "[ftms] a\n[cps] b\n[cps] c\n"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_supervisor.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.apps.supervisor'`.

- [ ] **Step 3: Implement** `src/wahoo_lsl/apps/supervisor.py`:

```python
"""Wahoo Server supervisor: the FTMS service (optional, switchable at runtime) and the CPS logger.

Reuses the dashboard launcher's Child (relay, CTRL_BREAK -> terminate -> kill) and confirm_zero.
One instance runs one session (start ... stop); the window builds a new one to restart.
The CPS logger is the session: its exit ends it. The FTMS service can stop, fail or be switched
off and on without touching the CPS logger, so recordings never see a gap.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable

from wahoo_lsl.dashboard.launcher import Child, Timings, confirm_zero

Post = Callable[[tuple[str, Any]], None]
NAMES = {"ftms": "FTMS service", "cps": "CPS logger"}


class LineSink:
    """Target for the children's relayed output: each line is posted as ("line", text) and appended to a log file."""

    def __init__(self, post: Post, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._post = post
        self._file = open(log_path, "a", encoding="utf-8")

    def write(self, text: str) -> int:
        self._file.write(text)
        for line in text.splitlines():
            self._post(("line", line))
        return len(text)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class ServerSupervisor:
    def __init__(self, commands: dict[str, list[str]], host: str, port: int, out: IO[str], post: Post,
                 timings: Timings = Timings(), confirm: Callable[[str, int], object] | None = None,
                 ftms_enabled: bool = True) -> None:
        self.commands, self.host, self.port, self.out, self.post, self.t = commands, host, port, out, post, timings
        self._confirm = confirm
        self._want_ftms = ftms_enabled  # set by the window; acted on by the supervisor thread
        self._lock = threading.Lock()  # shared with the Child relays: whole lines only
        self._stop, self._hurry = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._port_ready = False
        self.children: dict[str, Child] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """First call: ordered shutdown with grace periods. Any later call: skip them."""
        if self._stop.is_set():
            self._hurry.set()
        self._stop.set()

    def set_ftms(self, enabled: bool) -> None:
        """Ask for the FTMS service to be started or stopped; the CPS logger is never touched."""
        self._want_ftms = enabled

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        reason = "stopped"
        try:
            self.post(("supervisor", "starting"))
            reason = self._start_and_watch()
        except Exception as e:  # e.g. a worker executable that cannot be started
            reason = f"error: {e}"
        finally:
            if self.children:
                self.post(("supervisor", "stopping"))
            self._shutdown()
            self.post(("stopped", reason))

    def _start_and_watch(self) -> str:
        if self._want_ftms:
            self._start_ftms()
        if self._stop.is_set():
            return "stopped"
        cps = self._start("cps")
        self.post(("supervisor", "running"))
        while not self._stop.wait(self.t.poll_s):
            code = cps.poll()
            if code is not None:
                return f"{NAMES['cps']} exited with code {code}"
            ftms = self.children.get("ftms")
            if ftms is not None and ftms.poll() is not None:
                self._ftms_failed(f"{NAMES['ftms']} exited with code {ftms.poll()}")
            elif ftms is None and self._want_ftms:
                self._start_ftms()
            elif ftms is not None and not self._want_ftms:
                self._confirm_zero(ftms)
                self._stop_child("ftms")
                self.post(("ftms", "off"))
        return "stopped"

    def _start_ftms(self) -> None:
        """Start the FTMS service and wait for its port; on failure post why and leave FTMS off."""
        ftms = self._start("ftms")
        ready = self._wait_for_port(ftms)
        if ready == "ready":
            self._port_ready = True
            self.post(("ftms", "on"))
        elif ready == "exited":
            self._ftms_failed(f"{NAMES['ftms']} exited with code {ftms.poll()} during start-up")
        elif ready == "timeout":
            self._ftms_failed(f"{NAMES['ftms']} did not open {self.host}:{self.port} within {self.t.port_wait_s:g} s")
        # "stopped": the session is ending; _shutdown stops the child

    def _ftms_failed(self, reason: str) -> None:
        self._want_ftms = False
        self._stop_child("ftms")
        self.post(("ftms", f"stopped ({reason})"))

    def _shutdown(self) -> None:
        ftms = self.children.get("ftms")
        if ftms is not None:
            self._confirm_zero(ftms)
        for label in ("ftms", "cps"):
            if label in self.children:
                self._stop_child(label)

    def _stop_child(self, label: str) -> None:
        child = self.children.pop(label)
        if label == "ftms":
            self._port_ready = False
        grace = 0.0 if self._hurry.is_set() else self.t.grace_s
        code = child.stop(grace, self.t.kill_after_s, self._hurry)
        self._say(f"{NAMES[label]} stopped (exit code {code})")

    def _confirm_zero(self, ftms: Child) -> None:
        if not self._port_ready or self._hurry.is_set() or ftms.poll() is not None:
            return
        try:
            if self._confirm is not None:
                self._confirm(self.host, self.port)
            else:
                confirm_zero(self.host, self.port, say=lambda m: self._say(f"0 W check: {m}"))
        except Exception as e:  # never let the safety check block the shutdown
            self._say(f"0 W check failed: {e}")

    def _start(self, label: str) -> Child:
        child = self.children[label] = Child(label, self.commands[label], self.out, self._lock)
        return child

    def _wait_for_port(self, ftms: Child) -> str:
        deadline = time.monotonic() + self.t.port_wait_s
        while True:
            if ftms.poll() is not None:
                return "exited"
            if self._stop.is_set():
                return "stopped"
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return "ready"
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return "timeout"
            self._stop.wait(self.t.poll_s)

    def _say(self, msg: str) -> None:
        with self._lock:
            try:
                self.out.write(f"[server] {msg}\n")
                self.out.flush()
            except (OSError, ValueError):
                pass
```

Notes:
- Waiting for the FTMS port inside the watch loop blocks that loop for up to `port_wait_s`. That's acceptable: a stop request ends the wait, and a CPS exit is noticed right after.
- `test_ftms_exit_while_running_keeps_cps` kills the FTMS child directly, so it does not write its order line.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_apps_supervisor.py -q`
Expected: all pass. The `ignore` test proves that the second `stop()` hurries.

- [ ] **Step 5: Full suite, commit**

```bash
python -m pytest -q
git add src/wahoo_lsl/apps/supervisor.py tests/test_apps_supervisor.py
git commit -m "feat(apps): server supervisor (switchable FTMS, CPS session, 0 W check, hurry)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: LslFollower (CPS LSL stream → dashboard events)

**Files:**
- Create: `src/wahoo_lsl/apps/lsl_source.py`
- Test: `tests/test_apps_lsl_source.py`

**Interfaces:**
- Consumes: the CPS outlet's channel labels, which are the log field keys (`power_w`, `wheel_revs`, `wheel_event_t`, …, `speed_kmh`, `cadence_rpm`). `pylsl.util.LostError` (pylsl 1.18.6 is installed; the top-level `pylsl` has no `LostError`).
- Produces:
  - `channel_labels(info) -> list[str]`
  - `to_record(t, labels, values) -> dict`
  - `LslFollower(post, source_id=None, stream_type="CPS", resolve_s=1.0, pull_s=0.5)` with `start()` / `stop()`
  - It posts the `LogFollower` events: `("cps_status", text)`, `("cps_file", name)`, `("cps_event", {"kind": "connected"|"disconnected"})`, `("cps_sample", {"t_lsl", "fields"})`.

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_lsl_source.py`:

```python
import gc
import math
import queue
import time
import uuid

import pylsl
import pytest

from wahoo_lsl.apps.lsl_source import WAITING, LslFollower, channel_labels, to_record
from wahoo_lsl.dashboard.metrics import CpsDeriver

LABELS = ("power_w", "wheel_revs", "wheel_event_t")


def outlet(source_id, stream_type, labels=LABELS):
    info = pylsl.StreamInfo(f"name-{source_id}", stream_type, len(labels), 0, "double64", source_id)
    channels = info.desc().append_child("channels")
    for label in labels:
        channels.append_child("channel").append_child_value("label", label)
    return pylsl.StreamOutlet(info)


def collect(q, until, timeout=10.0, push=None):
    """Drain q into a list until until(events) holds; call push() between polls."""
    events, deadline = [], time.monotonic() + timeout
    while not until(events):
        assert time.monotonic() < deadline, f"timed out; got {events}"
        if push is not None:
            push()
        try:
            events.append(q.get(timeout=0.05))
        except queue.Empty:
            pass
    return events


def stop_quickly(follower):
    t0 = time.monotonic()
    follower.stop()
    assert time.monotonic() - t0 < 5.0


def test_to_record_drops_nan_and_makes_whole_numbers_int():
    rec = to_record(1.5, ["a", "b", "c"], [math.nan, 3.0, 2.5])
    assert rec == {"t_lsl": 1.5, "fields": {"b": 3, "c": 2.5}}
    assert isinstance(rec["fields"]["b"], int)


def test_channel_labels_fall_back_to_index():
    info = pylsl.StreamInfo("n", "T", 2, 0, "double64", "sid")
    assert channel_labels(info) == ["0", "1"]


def test_lsl_records_drive_the_deriver_like_log_records():
    log = [{"t_lsl": 1.0 + i, "fields": {"power_w": 150, "wheel_revs": 10 + i, "wheel_event_t": 2048 * (i + 1)}}
           for i in range(3)]
    lsl = [to_record(r["t_lsl"], LABELS, [float(r["fields"][k]) for k in LABELS]) for r in log]
    assert lsl == log
    a, b = CpsDeriver(), CpsDeriver()
    assert [a.update(r) for r in log] == [b.update(r) for r in lsl]


def test_follows_a_stream_by_source_id():
    sid = f"test-{uuid.uuid4().hex}"
    out = outlet(sid, "CPSTEST")
    q = queue.Queue()
    f = LslFollower(q.put, source_id=sid, resolve_s=0.5, pull_s=0.1)
    f.start()
    try:
        events = collect(q, lambda ev: ("cps_event", {"kind": "connected"}) in ev)
        assert ("cps_file", f"name-{sid}") in events
        events = collect(q, lambda ev: any(k == "cps_sample" for k, _ in ev),
                         push=lambda: out.push_sample([150.0, 10.0, 2048.0]))
        sample = next(p for k, p in events if k == "cps_sample")
        assert sample["fields"] == {"power_w": 150, "wheel_revs": 10, "wheel_event_t": 2048}
    finally:
        stop_quickly(f)
        del out


def test_waits_for_a_missing_stream_and_stops_promptly():
    q = queue.Queue()
    f = LslFollower(q.put, source_id=f"absent-{uuid.uuid4().hex}", resolve_s=0.2, pull_s=0.1)
    f.start()
    collect(q, lambda ev: ("cps_status", WAITING) in ev)
    stop_quickly(f)


def test_by_type_names_the_chosen_stream_among_several():
    stype = f"CPSTEST-{uuid.uuid4().hex[:8]}"
    sids = sorted(f"test-{uuid.uuid4().hex}" for _ in range(2))
    outs = [outlet(s, stype) for s in sids]
    q = queue.Queue()
    f = LslFollower(q.put, stream_type=stype, resolve_s=1.0, pull_s=0.1)
    f.start()
    try:
        events = collect(q, lambda ev: any(k == "cps_file" for k, _ in ev))
        assert ("cps_file", f"name-{sids[0]} (1 of 2 {stype} streams)") in events
    finally:
        stop_quickly(f)
        del outs


def test_reports_disconnected_when_the_outlet_goes_away():
    sid = f"test-{uuid.uuid4().hex}"
    out = outlet(sid, "CPSTEST")
    q = queue.Queue()
    f = LslFollower(q.put, source_id=sid, resolve_s=0.5, pull_s=0.1)
    f.start()
    try:
        collect(q, lambda ev: ("cps_event", {"kind": "connected"}) in ev)
        del out
        gc.collect()
        collect(q, lambda ev: ("cps_event", {"kind": "disconnected"}) in ev, timeout=20.0)
    finally:
        stop_quickly(f)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_lsl_source.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.apps.lsl_source'`.

- [ ] **Step 3: Implement** `src/wahoo_lsl/apps/lsl_source.py`:

```python
"""Follow a CPS LSL stream and post its samples as the dashboard's follower events.

Posts what LogFollower posts: ("cps_status", text), ("cps_file", stream name),
("cps_event", {"kind": "connected" | "disconnected"}) and ("cps_sample", {"t_lsl": t, "fields": {...}}).
Channel labels are the log's field keys, so the dashboard's CpsDeriver works unchanged.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Callable, Sequence

import pylsl
from pylsl.util import LostError

Post = Callable[[tuple[str, Any]], None]
STREAM_TYPE = "CPS"
WAITING = "waiting for CPS LSL stream"


def channel_labels(info: pylsl.StreamInfo) -> list[str]:
    """desc/channels/channel/label per channel; the channel index where a label is missing."""
    labels = []
    channel = info.desc().child("channels").child("channel")
    for i in range(info.channel_count()):
        labels.append(channel.child_value("label") or str(i))
        channel = channel.next_sibling()
    return labels


def to_record(t: float, labels: Sequence[str], values: Sequence[float]) -> dict:
    """A log-style sample record: NaN channels dropped, whole-number values as int (counters, power)."""
    fields: dict[str, Any] = {}
    for label, value in zip(labels, values):
        if math.isnan(value):
            continue
        fields[label] = int(value) if float(value).is_integer() else value
    return {"t_lsl": t, "fields": fields}


class LslFollower:
    def __init__(self, post: Post, source_id: str | None = None, stream_type: str = STREAM_TYPE,
                 resolve_s: float = 1.0, pull_s: float = 0.5) -> None:
        self._post = post
        self.source_id, self.stream_type = source_id, stream_type
        self.resolve_s, self.pull_s = resolve_s, pull_s
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lsl-follower", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(self.resolve_s + self.pull_s + 5.0)

    def _run(self) -> None:
        waiting = False
        while not self._stopping.is_set():
            found = self._resolve()
            if found is None:
                if not waiting:
                    self._post(("cps_status", WAITING))
                    waiting = True
                continue
            waiting = False
            try:
                self._follow(*found)
            except LostError:
                self._post(("cps_event", {"kind": "disconnected"}))
            except Exception as e:
                self._post(("cps_status", f"LSL error: {e}"))
                self._stopping.wait(self.resolve_s)

    def _resolve(self) -> tuple[pylsl.StreamInfo, int] | None:
        """The stream to follow and how many matched; None if none within resolve_s."""
        if self.source_id:
            streams = pylsl.resolve_byprop("source_id", self.source_id, 1, self.resolve_s)
        else:  # minimum 2: collect every stream of the type that answers within resolve_s
            streams = pylsl.resolve_byprop("type", self.stream_type, 2, self.resolve_s)
        if not streams:
            return None
        streams.sort(key=lambda info: info.source_id())
        return streams[0], len(streams)

    def _follow(self, info: pylsl.StreamInfo, matched: int) -> None:
        inlet = pylsl.StreamInlet(info, recover=False)
        try:
            full = inlet.info(timeout=2.0)
            labels = channel_labels(full)
            try:
                offset = inlet.time_correction(timeout=2.0)
            except LostError:
                raise
            except Exception:  # pylsl's TimeoutError: assume the same clock (same machine)
                offset = 0.0
            name = full.name() if matched == 1 else f"{full.name()} (1 of {matched} {self.stream_type} streams)"
            self._post(("cps_file", name))
            self._post(("cps_event", {"kind": "connected"}))
            while not self._stopping.is_set():
                values, t = inlet.pull_sample(timeout=self.pull_s)
                if values is not None:
                    self._post(("cps_sample", to_record(t + offset, labels, values)))
        finally:
            inlet.close_stream()
```

Verify while implementing:
- With `recover=False`, `pull_sample` raises `LostError` after the outlet is destroyed. That is the purpose of the last test.
- If liblsl takes longer than 20 s to notice on this machine, report it rather than lengthening the timeout silently.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_apps_lsl_source.py -q`
Expected: all pass. They create real LSL outlets on this machine with unique source_ids and types, so they never pick up the user's streams.

- [ ] **Step 5: Full suite, commit**

```bash
python -m pytest -q
git add src/wahoo_lsl/apps/lsl_source.py tests/test_apps_lsl_source.py
git commit -m "feat(apps): LSL follower posting dashboard CPS events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Wahoo Dashboard app

**Files:**
- Create: `src/wahoo_lsl/apps/dashboard_app.py`
- Test: `tests/test_apps_dashboard.py`

**Interfaces:**
- Consumes:
  - `wahoo_lsl.dashboard.app.Dashboard(root, events, worker, follower, *, max_watts, slew_w_per_s, circumference_m, clock, monotonic)`. Its `_build(max_watts, slew_w_per_s)` creates a `tk.LabelFrame` titled `"CPS (from log)"`. Its `_refresh_status()` sets `self.status_text` from `self._service` and `self._log_file`.
  - `PowerWorker(post, host, port)`.
  - `LslFollower` (Task 4); `paths` (Task 1).
  - `load_ftms_config(path).ftms.{host,port,slew_w_per_s}`; `load_config(path, require_measurements=False).derive.wheel_circumference_m`. Both raise `ConfigError`.
- Produces: `LslDashboard`, `defaults(home) -> dict` with keys `host`, `port`, `slew`, `circumference_m`, `build_parser(d)`, `main(argv)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_dashboard.py`:

```python
import gc
import json
import queue
import shutil
from pathlib import Path

import pytest

from wahoo_lsl.apps import dashboard_app
from wahoo_lsl.apps.dashboard_app import LslDashboard, build_parser, defaults
from wahoo_lsl.dashboard.metrics import DEFAULT_CIRCUMFERENCE_M
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
    del root
    gc.collect()


class FakeWorker:
    def request(self, watts):
        pass

    def request_slew(self, w_per_s):
        pass

    def stop(self):
        pass


class FakeFollower:
    def stop(self):
        pass


def write_configs(home, host="0.0.0.0", port=5000, slew=5, circumference=2.0):
    ftms = json.loads((ROOT / "ftms.example.json").read_text())
    ftms["ftms"].update(host=host, port=port, slew_w_per_s=slew)
    (home / "ftms.json").write_text(json.dumps(ftms))
    cps = json.loads((ROOT / "config.example.json").read_text())
    cps["derive"] = {"wheel_circumference_m": circumference}
    (home / "config.json").write_text(json.dumps(cps))


def test_defaults_from_the_server_configs(tmp_path):
    write_configs(tmp_path)
    assert defaults(tmp_path) == {"host": "127.0.0.1", "port": 5000, "slew": 5, "circumference_m": 2.0}


def test_defaults_fall_back_when_configs_are_missing_or_invalid(tmp_path):
    expected = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "slew": 10.0, "circumference_m": DEFAULT_CIRCUMFERENCE_M}
    assert defaults(tmp_path) == expected
    (tmp_path / "ftms.json").write_text("{not json")
    (tmp_path / "config.json").write_text("[]")
    assert defaults(tmp_path) == expected


def test_flags_override_defaults(tmp_path):
    write_configs(tmp_path)
    args = build_parser(defaults(tmp_path)).parse_args(["--port", "6000", "--stream", "KICKR-CPS-abc"])
    assert (args.host, args.port, args.slew, args.circumference_m, args.stream) == (
        "127.0.0.1", 6000, 5, 2.0, "KICKR-CPS-abc")
    assert build_parser(defaults(tmp_path)).parse_args([]).stream is None


@pytest.mark.parametrize("argv", [["--max-watts", "0"], ["--circumference-m", "0"], ["--slew", "-1"]])
def test_main_rejects_bad_values(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "data_dir", lambda: tmp_path)
    with pytest.raises(SystemExit) as e:
        dashboard_app.main(argv)
    assert e.value.code == 2


def test_lsl_wording(tk_root):
    d = LslDashboard(tk_root, queue.Queue(), FakeWorker(), FakeFollower())
    titles = [w.cget("text") for w in tk_root.winfo_children() if w.winfo_class() == "Labelframe"]
    assert "CPS (LSL)" in titles and "CPS (from log)" not in titles
    d.handle("cps_file", "KICKR-CPS-abc")
    assert d.status_text.get() == "service: starting · stream: KICKR-CPS-abc"
    d.handle("cps_sample", {"t_lsl": 1.0, "fields": {"power_w": 150}})
    assert d.cps_values["power"].get() == "150"
    d.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_dashboard.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.apps.dashboard_app'`.

- [ ] **Step 3: Implement** `src/wahoo_lsl/apps/dashboard_app.py`:

```python
"""Wahoo Dashboard: the KICKR test dashboard fed by the CPS LSL stream instead of the log file."""
from __future__ import annotations

import argparse
import queue
import signal
import sys
import tkinter as tk
from pathlib import Path
from typing import Sequence

from wahoo_lsl.config import ConfigError, load_config
from wahoo_lsl.dashboard.app import Dashboard
from wahoo_lsl.dashboard.metrics import DEFAULT_CIRCUMFERENCE_M
from wahoo_lsl.dashboard.power import PowerWorker
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.config import load_ftms_config
from wahoo_lsl.ftms.slew import MAX_SLEW_W_PER_S

from .lsl_source import LslFollower
from .paths import CPS_CONFIG, FTMS_CONFIG, connect_host, data_dir

LOG_TITLE, LSL_TITLE = "CPS (from log)", "CPS (LSL)"


class LslDashboard(Dashboard):
    """The test dashboard with LSL wording: the CPS frame title and a status bar naming the stream."""

    def _build(self, max_watts: int, slew_w_per_s: float) -> None:
        super()._build(max_watts, slew_w_per_s)
        for widget in self.root.winfo_children():
            if isinstance(widget, tk.LabelFrame) and widget.cget("text") == LOG_TITLE:
                widget.configure(text=LSL_TITLE)

    def _refresh_status(self) -> None:
        self.status_text.set(f"service: {self._service} · stream: {self._log_file}")


def defaults(home: Path) -> dict:
    """Defaults from the server's configs in home; built-in defaults for a missing or invalid file."""
    d = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "slew": 10.0, "circumference_m": DEFAULT_CIRCUMFERENCE_M}
    try:
        s = load_ftms_config(home / FTMS_CONFIG).ftms
        d.update(host=connect_host(s.host), port=s.port, slew=s.slew_w_per_s)
    except ConfigError:
        pass
    try:
        d["circumference_m"] = load_config(home / CPS_CONFIG, require_measurements=False).derive.wheel_circumference_m
    except ConfigError:
        pass
    return d


def build_parser(d: dict) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo-dashboard-app", description="KICKR test dashboard fed by the CPS LSL stream")
    p.add_argument("--host", default=d["host"], help="FTMS service host (default %(default)s)")
    p.add_argument("--port", type=int, default=d["port"], help="FTMS service port (default %(default)s)")
    p.add_argument("--max-watts", type=int, default=1000, help="slider maximum in W (default %(default)s)")
    p.add_argument("--circumference-m", type=float, default=d["circumference_m"],
                   help="wheel circumference for speed (default %(default)s)")
    p.add_argument("--slew", type=float, default=d["slew"], help="initial slew rate in W/s (default %(default)s)")
    p.add_argument("--stream", default=None, help="CPS stream source_id (default: the first CPS stream found)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(defaults(data_dir()))
    args = parser.parse_args(argv)
    if args.max_watts <= 0:
        parser.error("--max-watts must be positive")
    if not args.circumference_m > 0:
        parser.error("--circumference-m must be positive")
    if not 0 <= args.slew <= MAX_SLEW_W_PER_S:
        parser.error(f"--slew must be 0..{MAX_SLEW_W_PER_S:g}")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        if sys.stderr is not None:  # None in the windowed build
            print(f"wahoo-dashboard-app: cannot open a window: {e}", file=sys.stderr)
        return 2
    events: queue.Queue = queue.Queue()
    worker = PowerWorker(events.put, args.host, args.port)
    follower = LslFollower(events.put, source_id=args.stream)
    app = LslDashboard(root, events, worker, follower, max_watts=args.max_watts,
                       slew_w_per_s=args.slew, circumference_m=args.circumference_m)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: app.request_close())
    worker.start()
    follower.start()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_apps_dashboard.py -q`
Expected: all pass. The Tk test skips without a display.

- [ ] **Step 5: Full suite, commit**

```bash
python -m pytest -q
git add src/wahoo_lsl/apps/dashboard_app.py tests/test_apps_dashboard.py
git commit -m "feat(apps): Wahoo Dashboard fed by the CPS LSL stream

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Wahoo Server window

**Files:**
- Create: `src/wahoo_lsl/apps/server_app.py`
- Test: `tests/test_apps_server.py`

**Interfaces:**
- Consumes: Tasks 1–4; `wahoo_lsl.dashboard.launcher.timings_for(ftms_cfg)`; `load_config`, `load_ftms_config`, `ConfigError`.
- Produces:
  - `attach_hidden_console()`, `acquire_lock(path) -> IO | None`, `service_address(home)`, `already_running(host, port)`, `load_configs(home) -> FtmsConfig`, `open_path(path)`, `launch(argv, cwd)`
  - `ServerWindow(root, events, home, make_supervisor, make_follower, *, confirm, open_path, launch, monotonic)`, where `make_supervisor(cfg, ftms_enabled)` returns a supervisor with `start()`, `stop()`, `set_ftms(enabled)` and `running`. It has `ftms_var` (a `tk.BooleanVar`, True at launch), `toggle_ftms()`, `start_services()`, `restart()`, `open_logs()`, `open_dashboard()`, `handle(kind, payload)`, `close()`, `request_close()` and `destroyed`.
  - Supervisor `("ftms", …)` events:
    - `"on"`: no change; log lines drive the row;
    - `"off"`: row `off`, client `none`;
    - `"stopped (<reason>)"`: row shows it, client `none`, `ftms_var` unticked.
  - `main(argv)`

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_server.py`:

```python
import gc
import json
import queue
import shutil
import socket
from pathlib import Path

import pytest

from wahoo_lsl.apps import server_app
from wahoo_lsl.apps.paths import dashboard_argv
from wahoo_lsl.apps.server_app import (MAX_LOG_LINES, STALE_S, ServerWindow, acquire_lock, already_running,
                                       load_configs, service_address)
from wahoo_lsl.config import ConfigError
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[1]
P = "2026-10-28 10:00:00,000 INFO    wahoo_lsl.x: "


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
    del root
    gc.collect()


def write_configs(home, host="127.0.0.1", port=47610):
    ftms = json.loads((ROOT / "ftms.example.json").read_text())
    ftms["ftms"].update(host=host, port=port)
    (home / "ftms.json").write_text(json.dumps(ftms))
    shutil.copy(ROOT / "config.example.json", home / "config.json")


class FakeSupervisor:
    def __init__(self, cfg, ftms_enabled):
        self.cfg, self.ftms_enabled, self.started, self.stops, self.running = cfg, ftms_enabled, 0, 0, False
        self.ftms_requests = []

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stops += 1

    def set_ftms(self, enabled):
        self.ftms_requests.append(enabled)


class FakeFollower:
    def __init__(self, source_id):
        self.source_id, self.started, self.stopped = source_id, 0, 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def win(tk_root, tmp_path):
    write_configs(tmp_path)
    ns = type("NS", (), {})()
    ns.sups, ns.followers, ns.opened, ns.launched, ns.answer = [], [], [], [], True
    ns.clock = Clock()

    def make_supervisor(cfg, ftms_enabled):
        ns.sups.append(FakeSupervisor(cfg, ftms_enabled))
        return ns.sups[-1]

    def make_follower(sid):
        ns.followers.append(FakeFollower(sid))
        return ns.followers[-1]

    ns.w = ServerWindow(tk_root, queue.Queue(), tmp_path, make_supervisor, make_follower,
                        confirm=lambda *a: ns.answer, open_path=ns.opened.append,
                        launch=lambda argv, cwd: ns.launched.append((argv, cwd)), monotonic=ns.clock)
    ns.home = tmp_path
    return ns


def test_already_running():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert already_running("127.0.0.1", s.getsockname()[1])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not already_running("127.0.0.1", port)


def test_service_address(tmp_path):
    assert service_address(tmp_path) == (DEFAULT_HOST, DEFAULT_PORT)
    write_configs(tmp_path, host="0.0.0.0", port=5000)
    assert service_address(tmp_path) == ("127.0.0.1", 5000)


def test_load_configs_names_the_bad_file(tmp_path):
    write_configs(tmp_path)
    assert load_configs(tmp_path).ftms.port == 47610
    (tmp_path / "config.json").write_text("{bad")
    with pytest.raises(ConfigError, match="config.json"):
        load_configs(tmp_path)
    write_configs(tmp_path)
    (tmp_path / "ftms.json").unlink()
    with pytest.raises(ConfigError, match="ftms.json"):
        load_configs(tmp_path)


def test_acquire_lock_is_exclusive(tmp_path):
    first = acquire_lock(tmp_path / "server.lock")
    assert first is not None
    assert acquire_lock(tmp_path / "server.lock") is None
    first.close()
    again = acquire_lock(tmp_path / "server.lock")
    assert again is not None
    again.close()


def test_attach_hidden_console_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(server_app.sys, "platform", "linux")
    server_app.attach_hidden_console()


def test_start_services_starts_a_supervisor(win):
    assert win.w.ftms_var.get() is True  # FTMS control is on at every launch
    win.w.start_services()
    assert len(win.sups) == 1 and win.sups[0].started == 1
    assert win.sups[0].cfg.ftms.port == 47610 and win.sups[0].ftms_enabled is True


def test_start_follows_the_checkbox(win):
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()  # nothing running: only changes what the next start uses
    win.w.start_services()
    assert win.sups[0].ftms_enabled is False
    assert win.w.ftms_text.get() == "off"


def test_untick_asks_when_a_client_is_connected(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.answer = False
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    assert win.w.ftms_var.get() is True and win.sups[0].ftms_requests == []
    win.answer = True
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    assert win.sups[0].ftms_requests == [False]
    assert (win.w.ftms_text.get(), win.w.client_text.get()) == ("off", "none")


def test_tick_turns_ftms_back_on(win):
    win.w.start_services()
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    win.w.ftms_var.set(True)
    win.w.toggle_ftms()
    assert win.sups[0].ftms_requests == [False, True]
    assert win.w.ftms_text.get() == "starting"
    win.w.handle("line", f"[ftms] {P}FTMS control ready at 0 W (link_epoch 1)")
    assert win.w.ftms_text.get() == "ready"


def test_ftms_stopping_on_its_own_unticks_the_box(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.w.handle("ftms", "stopped (FTMS service exited with code 1)")
    assert win.w.ftms_var.get() is False
    assert win.w.ftms_text.get() == "stopped (FTMS service exited with code 1)"
    assert win.w.client_text.get() == "none"
    win.w.handle("ftms", "off")
    assert win.w.ftms_text.get() == "off"


def test_config_error_shown_and_restart_retries(win):
    (win.home / "ftms.json").write_text("{bad")
    win.w.start_services()
    assert win.sups == []
    assert win.w.state_text.get().startswith("config error: ") and "ftms.json" in win.w.state_text.get()
    write_configs(win.home)
    win.w.restart()
    assert len(win.sups) == 1


def test_status_lines_update_the_rows(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}FTMS control ready at 0 W (link_epoch 1)")
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.w.handle("line", f"[cps] {P}Streaming from KICKR (mac AA)")
    assert (win.w.ftms_text.get(), win.w.client_text.get(), win.w.cps_text.get()) == ("ready", "peer1", "streaming")
    assert "Client connected: peer1" in win.w.log.get("1.0", "end")


def test_follower_bound_to_this_servers_stream(win):
    win.w.handle("line", f"[cps] {P}LSL outlet KICKR-CPS-aaa: 3 channels (power_w)")
    win.w.handle("line", f"[cps] {P}LSL outlet KICKR-CPS-aaa: 3 channels (power_w)")
    assert [(f.source_id, f.started) for f in win.followers] == [("KICKR-CPS-aaa", 1)]
    assert win.w.stream_text.get() == "KICKR-CPS-aaa"
    win.w.handle("line", f"[cps] {P}LSL outlet KICKR-CPS-bbb: 3 channels (power_w)")
    assert win.followers[0].stopped == 1
    assert [f.source_id for f in win.followers] == ["KICKR-CPS-aaa", "KICKR-CPS-bbb"]


def test_power_and_staleness(win):
    win.w.handle("cps_sample", {"t_lsl": 1.0, "fields": {"power_w": 150}})
    assert win.w.power_text.get() == "150 W"
    win.clock.t += STALE_S + 1
    win.w._pump()
    assert win.w.power_text.get() == "—"
    win.w.handle("cps_sample", {"t_lsl": 2.0, "fields": {"power_w": 120}})
    win.w.handle("cps_event", {"kind": "disconnected"})
    assert win.w.power_text.get() == "—"


def test_supervisor_stopped_event(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}FTMS control ready at 0 W (link_epoch 1)")
    win.w.handle("stopped", "CPS logger exited with code 3")
    assert win.w.state_text.get() == "stopped (CPS logger exited with code 3)"
    assert win.w.ftms_text.get() == "stopped"


def test_restart_waits_for_the_running_supervisor(win):
    win.w.start_services()
    win.w.restart()
    assert win.sups[0].stops == 1 and len(win.sups) == 1
    win.w._pump()
    assert len(win.sups) == 1  # still running
    win.sups[0].running = False
    win.w._pump()
    assert len(win.sups) == 2 and win.sups[1].started == 1


def test_close_asks_when_a_client_is_connected(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.answer = False
    win.w.close()
    assert win.sups[0].stops == 0 and not win.w.destroyed
    win.answer = True
    win.w.close()
    assert win.sups[0].stops == 1
    assert win.w.state_text.get().startswith("Stopping")
    win.w.close()  # second close hurries
    assert win.sups[0].stops == 2
    win.w._pump()
    assert not win.w.destroyed
    win.sups[0].running = False
    win.w._pump()
    assert win.w.destroyed


def test_close_with_nothing_running_destroys_on_next_pump(win):
    win.w.close()
    win.w._pump()
    assert win.w.destroyed


def test_request_close_from_a_signal(win):
    win.w.request_close()
    win.w._pump()
    win.w._pump()
    assert win.w.destroyed


def test_buttons_open_logs_and_dashboard(win):
    win.w.open_logs()
    win.w.open_dashboard()
    assert win.opened == [win.home / "logs"]
    assert win.launched == [(dashboard_argv(), win.home)]


def test_log_pane_is_capped(win):
    for i in range(MAX_LOG_LINES + 10):
        win.w.handle("line", f"[cps] line {i}")
    text = win.w.log.get("1.0", "end").splitlines()
    assert len([t for t in text if t]) <= MAX_LOG_LINES
    assert text[-1] == f"[cps] line {MAX_LOG_LINES + 9}" or text[-2] == f"[cps] line {MAX_LOG_LINES + 9}"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_server.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wahoo_lsl.apps.server_app'`.

- [ ] **Step 3: Implement** `src/wahoo_lsl/apps/server_app.py`:

```python
"""Wahoo Server: runs the FTMS control service and the CPS logger/LSL streamer, with a status window.

Closing the window spins the trainer down (0 W check) and stops both services. The window never
connects to the FTMS service itself, so it never takes the single client slot.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext
from typing import IO, Any, Callable, Sequence

from wahoo_lsl.config import ConfigError, load_config
from wahoo_lsl.dashboard.launcher import timings_for
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.config import FtmsConfig, load_ftms_config

from .lsl_source import LslFollower
from .paths import CPS_CONFIG, FTMS_CONFIG, LOG_DIR, connect_host, dashboard_argv, data_dir, service_commands
from .server_status import ServerStatus
from .supervisor import LineSink, ServerSupervisor

log = logging.getLogger(__name__)

PUMP_MS = 100
STALE_S = 15.0
MAX_LOG_LINES = 2000
DASH = "—"
LOCK_FILE = "server.lock"
FTMS_HINT = "FTMS control (untick to let another app or device control the trainer)"
STATES = {"starting": "starting", "running": "running", "stopping": "stopping: spinning down to 0 W…"}


def attach_hidden_console() -> None:
    """Windows: give this windowed app a hidden console. The console-subsystem worker children
    inherit it, so the launcher's CTRL_BREAK stop reaches them (it only reaches a shared console)."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow():
        return  # started from a terminal: share that one
    kernel32.AllocConsole()
    hwnd = kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def acquire_lock(path: Path) -> IO[str] | None:
    """An exclusive OS lock on path, held while the returned file stays open; None if another process holds it."""
    f = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def service_address(home: Path) -> tuple[str, int]:
    """Where a local client reaches the FTMS service configured in home (defaults if unreadable)."""
    try:
        s = load_ftms_config(home / FTMS_CONFIG).ftms
    except ConfigError:
        return DEFAULT_HOST, DEFAULT_PORT
    return connect_host(s.host), s.port


def already_running(host: str, port: int, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def load_configs(home: Path) -> FtmsConfig:
    """Validate both configs before anything starts; the ConfigError names the file."""
    try:
        load_config(home / CPS_CONFIG)
    except ConfigError as e:
        raise ConfigError(f"{home / CPS_CONFIG}: {e}") from None
    try:
        return load_ftms_config(home / FTMS_CONFIG)
    except ConfigError as e:
        raise ConfigError(f"{home / FTMS_CONFIG}: {e}") from None


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def launch(argv: Sequence[str], cwd: Path) -> None:
    subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


class ServerWindow:
    def __init__(self, root: tk.Tk, events: queue.Queue, home: Path,
                 make_supervisor: Callable[[FtmsConfig, bool], Any], make_follower: Callable[[str], Any], *,
                 confirm: Callable[[str, str], bool] = messagebox.askyesno,
                 open_path: Callable[[Path], None] = open_path,
                 launch: Callable[[Sequence[str], Path], None] = launch,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.root, self.events, self.home = root, events, home
        self._make_supervisor, self._make_follower = make_supervisor, make_follower
        self._confirm, self._open_path, self._launch, self._monotonic = confirm, open_path, launch, monotonic
        self.status = ServerStatus()
        self.supervisor: Any = None
        self.follower: Any = None
        self._power_t: float | None = None
        self._restart_pending = self._closing = self._close_requested = self.destroyed = False
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._pump_id = root.after(PUMP_MS, self._pump)

    def _build(self) -> None:
        root = self.root
        root.title("Wahoo Server")
        rows = tk.Frame(root, padx=8, pady=6)
        rows.pack(fill="x")
        self.state_text, self.ftms_text, self.cps_text = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.stream_text, self.client_text, self.power_text = tk.StringVar(), tk.StringVar(), tk.StringVar(value=DASH)
        for i, (title, var) in enumerate((
            ("Services", self.state_text), ("Trainer control (FTMS)", self.ftms_text),
            ("Trainer data (CPS)", self.cps_text), ("LSL stream", self.stream_text),
            ("FTMS client", self.client_text), ("Power", self.power_text),
        )):
            tk.Label(rows, text=title, anchor="w").grid(row=i, column=0, sticky="w", padx=(0, 12))
            tk.Label(rows, textvariable=var, anchor="w").grid(row=i, column=1, sticky="w")
        self.ftms_var = tk.BooleanVar(value=True)  # on at every launch; not saved
        self.ftms_box = tk.Checkbutton(root, text=FTMS_HINT, variable=self.ftms_var, command=self.toggle_ftms,
                                       anchor="w", padx=8)
        self.ftms_box.pack(fill="x")
        bar = tk.Frame(root, padx=8)
        bar.pack(fill="x")
        self.buttons = [
            tk.Button(bar, text="Open logs folder", command=self.open_logs),
            tk.Button(bar, text="Restart services", command=self.restart),
            tk.Button(bar, text="Open dashboard", command=self.open_dashboard),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=(0, 6), pady=4)
        self.log = scrolledtext.ScrolledText(root, height=16, width=110, state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._render()

    # --- actions ----------------------------------------------------------

    def start_services(self) -> None:
        try:
            cfg = load_configs(self.home)
        except ConfigError as e:
            self.state_text.set(f"config error: {e}")
            self._append(f"[server] config error: {e}")
            return
        enabled = self.ftms_var.get()
        self.status.starting(enabled)
        self.state_text.set(STATES["starting"])
        self.supervisor = self._make_supervisor(cfg, enabled)
        self.supervisor.start()
        self._render()

    def restart(self) -> None:
        if self._closing:
            return
        if self.supervisor is not None and self.supervisor.running:
            self._restart_pending = True
            self.supervisor.stop()
        else:
            self.start_services()

    def toggle_ftms(self) -> None:
        """The checkbox changed: start or stop only the FTMS service (the CPS logger keeps running)."""
        enabled = self.ftms_var.get()
        if not enabled and self.status.client and self._running() and not self._confirm(
                "Wahoo Server", "A client is controlling the trainer. Turn off FTMS control anyway?"):
            self.ftms_var.set(True)
            return
        if enabled:
            self.status.ftms = "starting"
        else:
            self.status.ftms, self.status.client = "off", ""
        if self._running():
            self.supervisor.set_ftms(enabled)
        self._render()

    def open_logs(self) -> None:
        self._open_path(self.home / LOG_DIR)

    def open_dashboard(self) -> None:
        try:
            self._launch(dashboard_argv(), self.home)
        except OSError as e:
            self._append(f"[server] cannot open the dashboard: {e}")

    def request_close(self) -> None:
        """Safe from a signal handler: the next pump closes."""
        self._close_requested = True

    def close(self) -> None:
        if self._closing:
            if self.supervisor is not None:
                self.supervisor.stop()  # second close: skip the grace periods
            return
        if self.status.client and self._running() and not self._confirm(
                "Wahoo Server", "A client is controlling the trainer. Stop anyway?"):
            return
        self._closing, self._restart_pending = True, False
        for widget in (*self.buttons, self.ftms_box):
            widget.configure(state="disabled")
        self.state_text.set("Stopping: spinning down to 0 W…")
        if self.supervisor is not None:
            self.supervisor.stop()

    # --- events -------------------------------------------------------------

    def handle(self, kind: str, payload: Any) -> None:
        if kind == "line":
            self._append(payload)
            if self.status.update(payload):
                self._render()
                self._follow_stream()
        elif kind == "supervisor":
            if not self._closing:
                self.state_text.set(STATES.get(payload, payload))
        elif kind == "ftms":
            if payload != "on":  # "off" or "stopped (<reason>)"; "on": the log lines drive the row
                self.status.ftms, self.status.client = payload, ""
                if payload != "off":
                    self.ftms_var.set(False)
                self._render()
        elif kind == "stopped":
            self.status.stopped()
            if not self._closing:
                self.state_text.set(f"stopped ({payload})")
            self._render()
        elif kind == "cps_sample":
            watts = payload["fields"].get("power_w")
            if watts is not None:
                self.power_text.set(f"{watts} W")
                self._power_t = self._monotonic()
        elif kind == "cps_event" and payload.get("kind") == "disconnected":
            self._clear_power()

    def _follow_stream(self) -> None:
        """Power comes from this server's own stream: (re)bind the follower when the outlet id changes."""
        sid = self.status.stream
        if not sid or (self.follower is not None and self.follower.source_id == sid):
            return
        if self.follower is not None:
            self.follower.stop()
        self.follower = self._make_follower(sid)
        self.follower.start()

    def _render(self) -> None:
        self.ftms_text.set(self.status.ftms)
        self.cps_text.set(self.status.cps)
        self.stream_text.set(self.status.stream or DASH)
        self.client_text.set(self.status.client or "none")

    def _append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        excess = int(self.log.index("end-1c").split(".")[0]) - 1 - MAX_LOG_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def _clear_power(self) -> None:
        self.power_text.set(DASH)
        self._power_t = None

    def _running(self) -> bool:
        return self.supervisor is not None and self.supervisor.running

    # --- timer and teardown -------------------------------------------------

    def _pump(self) -> None:
        if self.destroyed:
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle(kind, payload)
            except Exception:
                log.exception("server window: bad %s event skipped", kind)
        if self._power_t is not None and self._monotonic() - self._power_t > STALE_S:
            self._clear_power()
        if self._close_requested:
            self._close_requested = False
            self.close()
        if self._restart_pending and not self._running():
            self._restart_pending = False
            self.start_services()
        if self._closing and not self._running():
            self._destroy()
            return
        self._pump_id = self.root.after(PUMP_MS, self._pump)

    def _destroy(self) -> None:
        self.destroyed = True
        try:
            self.root.after_cancel(self._pump_id)
        except tk.TclError:
            pass
        if self.follower is not None:
            self.follower.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo-server",
                                description="Run the FTMS control service and the CPS logger/LSL streamer")
    p.add_argument("--debug", action="store_true", help="pass --debug to both services")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)  # the children's relative config and log paths resolve here
    try:
        root = tk.Tk()
    except tk.TclError as e:
        if sys.stderr is not None:  # None in the windowed build
            print(f"wahoo-server: cannot open a window: {e}", file=sys.stderr)
        return 2
    lock = acquire_lock(home / LOCK_FILE)  # held until the process exits
    host, port = service_address(home)
    problem = None
    if lock is None:
        problem = "Wahoo Server is already running."
    elif already_running(host, port):
        problem = f"Another program is using {host}:{port} (the FTMS port in {FTMS_CONFIG})."
    if problem is not None:
        root.withdraw()
        messagebox.showerror("Wahoo Server", problem)
        root.destroy()
        return 1
    attach_hidden_console()
    events: queue.Queue = queue.Queue()
    sink = LineSink(events.put, home / LOG_DIR / f"server_{time.strftime('%Y%m%d_%H%M%S')}.log")

    def make_supervisor(cfg: FtmsConfig, ftms_enabled: bool) -> ServerSupervisor:
        return ServerSupervisor(service_commands(args.debug), connect_host(cfg.ftms.host), cfg.ftms.port,
                                sink, events.put, timings_for(cfg), ftms_enabled=ftms_enabled)

    window = ServerWindow(root, events, home, make_supervisor,
                          lambda source_id: LslFollower(events.put, source_id=source_id))
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: window.request_close())
    window.start_services()
    try:
        root.mainloop()
    finally:
        sink.close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Notes:
- `FakeFollower` exposes `.source_id`, matching the real `LslFollower`.
- `test_request_close_from_a_signal` pumps twice: the first pump closes, and with no supervisor the same pump destroys. Either one or two pumps is acceptable; keep the assertion.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_apps_server.py -q`
Expected: all pass. The Tk tests skip without a display.

- [ ] **Step 5: Full suite, commit**

```bash
python -m pytest -q
git add src/wahoo_lsl/apps/server_app.py tests/test_apps_server.py
git commit -m "feat(apps): Wahoo Server status window

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Windows packaging, installer and guide

**Files:**
- Create:
  - `packaging/windows/wahoo_server.py`, `wahoo_dashboard.py`, `wahoo_worker.py` (PyInstaller entry scripts)
  - `packaging/windows/wahoo.spec`, `wahoo.iss`, `build.ps1`
  - `packaging/windows/make_icons.py`, `server.ico`, `dashboard.ico`
  - `docs/windows-apps.md`
- Test: `tests/test_apps_packaging.py`

**Interfaces:**
- Consumes: entry points `wahoo_lsl.apps.server_app:main`, `wahoo_lsl.apps.dashboard_app:main`, `wahoo_lsl.apps.worker:main`; repo-root `config.example.json`, `ftms.example.json`.

- [ ] **Step 1: Write the failing tests** — `tests/test_apps_packaging.py`:

```python
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "windows"
ENTRIES = {
    "wahoo_server.py": "wahoo_lsl.apps.server_app",
    "wahoo_dashboard.py": "wahoo_lsl.apps.dashboard_app",
    "wahoo_worker.py": "wahoo_lsl.apps.worker",
}


def test_pyproject_entry_points_and_windows_extra():
    tomllib = pytest.importorskip("tomllib")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["wahoo-server"] == "wahoo_lsl.apps.server_app:main"
    assert project["scripts"]["wahoo-dashboard-app"] == "wahoo_lsl.apps.dashboard_app:main"
    assert "pyinstaller>=6" in project["optional-dependencies"]["windows"]


@pytest.mark.parametrize("script, module", ENTRIES.items())
def test_entry_scripts_call_existing_mains(script, module):
    assert f"from {module} import main" in (PKG / script).read_text()
    assert callable(importlib.import_module(module).main)


def test_spec_builds_the_three_executables():
    spec = (PKG / "wahoo.spec").read_text()
    for name in ("WahooServer", "WahooDashboard", "wahoo-worker"):
        assert f'"{name}"' in spec
    for script in ENTRIES:
        assert script in spec


def test_installer_sources_exist():
    iss = (PKG / "wahoo.iss").read_text()
    sources = re.findall(r'Source: "([^"*]+)"', iss)
    assert sources, "no single-file sources found"
    for src in sources:
        assert (PKG / src.replace("\\", "/")).resolve().is_file(), src
    assert "WahooServer.exe" in iss and "WahooDashboard.exe" in iss and "wahoo-worker.exe" in iss


@pytest.mark.parametrize("name", ["server.ico", "dashboard.ico"])
def test_icons_are_ico_files(name):
    assert (PKG / name).read_bytes()[:4] == b"\x00\x00\x01\x00"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_apps_packaging.py -q`
Expected: FAIL (files missing). `test_pyproject_entry_points_and_windows_extra` already passes after Task 1.

- [ ] **Step 3: Implement**

Entry scripts (one each, exactly this shape):

```python
# packaging/windows/wahoo_server.py — PyInstaller entry script for WahooServer.exe
from wahoo_lsl.apps.server_app import main

raise SystemExit(main())
```

`wahoo_dashboard.py` imports `from wahoo_lsl.apps.dashboard_app import main`, and `wahoo_worker.py` imports `from wahoo_lsl.apps.worker import main`. Both have the same shape.

`packaging/windows/wahoo.spec`:

```python
# PyInstaller spec: WahooServer.exe, WahooDashboard.exe and wahoo-worker.exe in one folder (WahooLSL).
# Build with packaging\windows\build.ps1 on Windows.
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

HERE = Path(SPECPATH)
HIDDEN = collect_submodules("wahoo_lsl") + collect_submodules("bleak") + collect_submodules("winrt")
BINARIES = collect_dynamic_libs("pylsl")  # liblsl (lsl.dll)

APPS = (  # name, entry script, console, icon
    ("WahooServer", "wahoo_server.py", False, "server.ico"),
    ("WahooDashboard", "wahoo_dashboard.py", False, "dashboard.ico"),
    ("wahoo-worker", "wahoo_worker.py", True, None),
)

exes, parts = [], []
for name, script, console, icon in APPS:
    a = Analysis([str(HERE / script)], binaries=BINARIES, hiddenimports=HIDDEN)
    exes.append(EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console,
                    icon=str(HERE / icon) if icon else None))
    parts += [a.binaries, a.datas]

coll = COLLECT(*exes, *parts, name="WahooLSL")
```

`packaging/windows/wahoo.iss`. Generate a fresh GUID for `AppId` (`python -c "import uuid; print(str(uuid.uuid4()).upper())"`) and keep it fixed from then on:

```iss
; Wahoo LSL installer: Wahoo Server + Wahoo Dashboard (+ wahoo-worker). Build with build.ps1.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{<GUID>}
AppName=Wahoo LSL
AppVersion={#AppVersion}
DefaultDirName={autopf}\Wahoo LSL
DefaultGroupName=Wahoo LSL
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\build\installer
OutputBaseFilename=WahooLSL-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\WahooServer.exe

[Tasks]
Name: "desktopicons"; Description: "Create desktop icons"; Flags: unchecked

[Dirs]
Name: "{commonappdata}\wahoo-lsl"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\wahoo-lsl\logs"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "..\..\build\pyinstaller\dist\WahooLSL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\config.example.json"; DestDir: "{commonappdata}\wahoo-lsl"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\..\ftms.example.json"; DestDir: "{commonappdata}\wahoo-lsl"; DestName: "ftms.json"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\Wahoo Server"; Filename: "{app}\WahooServer.exe"
Name: "{group}\Wahoo Dashboard"; Filename: "{app}\WahooDashboard.exe"
Name: "{autodesktop}\Wahoo Server"; Filename: "{app}\WahooServer.exe"; Tasks: desktopicons
Name: "{autodesktop}\Wahoo Dashboard"; Filename: "{app}\WahooDashboard.exe"; Tasks: desktopicons

[Run]
; LSL: let LabRecorder on other lab PCs (private network) see the stream without a firewall prompt.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Wahoo LSL worker"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Wahoo LSL worker"" dir=in action=allow program=""{app}\wahoo-worker.exe"" profile=private enable=yes"; Flags: runhidden

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Wahoo LSL worker"""; Flags: runhidden; RunOnceId: "DeleteFirewallRule"
```

`packaging/windows/build.ps1`:

```powershell
# Build the Wahoo LSL installer: clean venv -> pip install .[windows] -> PyInstaller -> Inno Setup.
# Requires Windows, Python 3.12 (py launcher) and Inno Setup 6. Output: build\installer\WahooLSL-Setup-<version>.exe
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$exe $args failed with exit code $LASTEXITCODE" }
}

$venv = "build\venv"
if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
Run py -3.12 -m venv $venv
$python = "$venv\Scripts\python.exe"
Run $python -m pip install --upgrade pip
Run $python -m pip install ".[windows]"
Run $python -m PyInstaller --noconfirm --distpath build\pyinstaller\dist --workpath build\pyinstaller\work packaging\windows\wahoo.spec
$version = & $python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
Run $iscc "/DAppVersion=$version" packaging\windows\wahoo.iss
Write-Host "Installer: build\installer\WahooLSL-Setup-$version.exe"
```

`packaging/windows/make_icons.py`. Run it once and commit its outputs:

```python
"""Write server.ico (green disc with a white ring) and dashboard.ico (blue tile with rising bars).

Stdlib only; run once from this folder: python make_icons.py. The .ico files are committed.
"""
import struct
from pathlib import Path

CLEAR = (0, 0, 0, 0)
WHITE = (255, 255, 255, 255)


def server(x: int, y: int, n: int) -> tuple[int, int, int, int]:
    c, r = (n - 1) / 2, n / 2 - 0.5
    d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
    if d > r:
        return CLEAR
    return WHITE if 0.45 * r < d < 0.62 * r else (34, 139, 34, 255)


def dashboard(x: int, y: int, n: int) -> tuple[int, int, int, int]:
    if not (0 < x < n - 1 and 0 < y < n - 1):
        return CLEAR
    w = max(1, n // 6)
    for i, height in enumerate((0.35, 0.6, 0.85)):  # three bars, left to right
        left = n // 6 + i * (w + max(1, n // 12))
        if left <= x < left + w and y >= n - 2 - int(height * (n - 4)):
            return WHITE
    return (30, 90, 200, 255)


def image(pixel, n: int) -> bytes:
    rows = b"".join(
        b"".join(struct.pack("BBBB", b, g, r, a) for r, g, b, a in (pixel(x, y, n) for x in range(n)))
        for y in reversed(range(n))  # BMP rows run bottom-up
    )
    mask = bytes(((n + 31) // 32 * 4) * n)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(rows) + len(mask), 0, 0, 0, 0)
    return header + rows + mask


def write_ico(path: Path, pixel, sizes=(16, 32, 48)) -> None:
    images = [image(pixel, n) for n in sizes]
    out, offset = struct.pack("<HHH", 0, 1, len(images)), 6 + 16 * len(images)
    for n, data in zip(sizes, images):
        out += struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(out + b"".join(images))


if __name__ == "__main__":
    here = Path(__file__).parent
    write_ico(here / "server.ico", server)
    write_ico(here / "dashboard.ico", dashboard)
```

`docs/windows-apps.md` is a user guide, in plain prose plus short tables, with these sections:
1. **What it is:** the two apps, the workflow (open Wahoo Server; open Wahoo Dashboard only for manual control), and that closing the server spins the trainer down.
2. **Building the installer:** on Windows with Python 3.12 + Inno Setup 6, run `powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1`; output in `build\installer\`.
3. **Installing:** run the installer as an administrator; SmartScreen ("More info → Run anyway", because it's unsigned); what goes where (`Program Files\Wahoo LSL`, `%ProgramData%\wahoo-lsl\config.json`, `ftms.json`, `logs\`); upgrades keep the configs; uninstall keeps configs and logs.
4. **First-run configuration:** edit `device` (`name` / `mac`) in **both** config files (same values), then **Restart services**. Use `"C:\Program Files\Wahoo LSL\wahoo-worker.exe" lsl scan` and `lsl list-features -c "%ProgramData%\wahoo-lsl\config.json"` to find the trainer.
5. **The server window:** each row and its values; the **FTMS control** checkbox (on at every launch; untick it to let another app or device such as a training app control the trainer while logging and LSL continue; tick it again once that app has disconnected; it unticks itself if the FTMS service stops); the buttons; closing (the confirmation when a client is connected; a second close skips the waits); config errors; the "already running" message; that only one FTMS client (dashboard, script or MATLAB) can control the trainer at a time.
6. **The dashboard:** uses the server's `ftms.json` host/port; follows the first CPS LSL stream (the status bar names it); `--stream <source_id>` to choose one.
7. **Logs:** `server_<time>.log` (both services' output) and `cps_<time>.jsonl` (data).
8. **Firewall and LSL:** the private-network rule for `wahoo-worker.exe`.
9. **Manual test checklist (Windows):** the 11 steps from the spec's Testing section, verbatim.
10. **Developing on macOS/Linux:** `wahoo-server` and `wahoo-dashboard-app` run unpackaged, using `WAHOO_LSL_HOME` or the current folder.

- [ ] **Step 4: Generate icons and run the tests**

```bash
(cd packaging/windows && python make_icons.py)
python -m pytest tests/test_apps_packaging.py -q
```

Expected: all pass. The spec and the installer cannot be run here; say so in the report.

- [ ] **Step 5: Full suite, check that no existing file changed, commit**

```bash
python -m pytest -q
git diff main --stat -- . ':!src/wahoo_lsl/apps' ':!tests/test_apps_*' ':!packaging' ':!docs/windows-apps.md' ':!docs/superpowers'
```

Expected: only `pyproject.toml` is listed.

```bash
git add packaging docs/windows-apps.md tests/test_apps_packaging.py
git commit -m "feat(apps): Windows PyInstaller spec, Inno Setup installer and guide

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
