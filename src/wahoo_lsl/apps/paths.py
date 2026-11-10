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


def service_commands(debug: bool = False, ftms_enabled: bool = True) -> dict[str, list[str]]:
    """The server's one worker; config paths are relative to data_dir(), the server's working directory."""
    extra = [*([] if ftms_enabled else ["--no-ftms"]), *(["--debug"] if debug else [])]
    return {"worker": worker_argv("server", "-c", CPS_CONFIG, "-f", FTMS_CONFIG, *extra)}


def dashboard_argv() -> list[str]:
    if frozen():
        return [_sibling("TrainerDashboard")]
    return [sys.executable, "-m", "wahoo_lsl.apps.dashboard_app"]


def connect_host(host: str) -> str:
    """The address a local client dials for a service bound to `host` (wildcards -> loopback)."""
    return _WILDCARDS.get(host, host)
