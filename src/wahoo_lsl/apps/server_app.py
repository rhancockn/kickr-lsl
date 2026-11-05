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
