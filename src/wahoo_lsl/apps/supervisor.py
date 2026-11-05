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
        deadline = time.monotonic() + self.t.cps_early_exit_s  # as in the launcher: let the child settle
        while time.monotonic() < deadline and not self._stop.is_set():
            if cps.poll() is not None:
                return f"{NAMES['cps']} exited with code {cps.poll()}"
            self._stop.wait(self.t.poll_s)
        if self._stop.is_set():
            return "stopped"
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
