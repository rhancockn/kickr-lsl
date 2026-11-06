"""Wahoo Server supervisor: drives the one `wahoo-worker server` process (FTMS service + CPS logger).

The worker shares one BLE connection between both services and takes `ftms on`, `ftms off` and
`stop` on stdin. One instance runs one session (start ... stop); the window builds a new one to
restart. The CPS logger is the session: the worker exits with its code. The FTMS service can stop,
fail or be switched off and on without touching the CPS logger, so recordings never see a gap.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable

from wahoo_lsl.dashboard.launcher import Timings

from .worker_process import WorkerProcess

Post = Callable[[tuple[str, Any]], None]
FTMS_NAME, CPS_NAME = "FTMS service", "CPS logger"
FTMS_READY = "Accepting client commands on"
FTMS_EXIT = re.compile(r"^\[worker\].*\bftms exited with code (-?\d+)")


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
    def __init__(self, commands: dict[str, list[str]], out: IO[str], post: Post,
                 timings: Timings = Timings(), ftms_enabled: bool = True) -> None:
        self.commands, self.out, self.post, self.t = commands, out, post, timings
        self._want_ftms = ftms_enabled  # set by the window; acted on by the supervisor thread
        self._lock = threading.Lock()  # shared with the relay thread: whole lines only
        self._stop, self._hurry = threading.Event(), threading.Event()
        self._ready = threading.Event()  # set by the relay: FTMS logged its ready line
        self._exits: queue.Queue[int] = queue.Queue()  # set by the relay: codes of ended FTMS tasks
        self._thread: threading.Thread | None = None
        self.worker: WorkerProcess | None = None

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
            if self.worker is not None:
                self.post(("supervisor", "stopping"))
            try:
                self._shutdown()
            finally:
                self.post(("stopped", reason))

    def _on_line(self, line: str) -> None:
        """Relay thread: only sets thread-safe state for the supervisor thread."""
        if line.startswith("[ftms]") and FTMS_READY in line:
            self._ready.set()
        elif m := FTMS_EXIT.match(line):
            self._exits.put(int(m.group(1)))

    def _start_and_watch(self) -> str:
        self.worker = worker = WorkerProcess(self.commands["worker"], self.out, self._lock, self._on_line)
        # state: "off", "starting" (waiting for the ready line), "on", "stopping" (waiting for the exit line)
        state = "starting" if self._want_ftms else "off"
        deadline = time.monotonic() + self.t.port_wait_s
        quiet_exit = False  # the next FTMS exit was already reported (start-up timeout)
        began, announced = time.monotonic(), False
        while not self._stop.is_set():
            code = worker.poll()
            if code is not None:
                return f"{CPS_NAME} exited with code {code}"
            if not announced and time.monotonic() - began >= self.t.cps_early_exit_s:
                announced = True
                self.post(("supervisor", "running"))
            while not self._exits.empty():
                exit_code = self._exits.get()
                if quiet_exit:
                    quiet_exit = False
                elif state == "stopping":
                    self.post(("ftms", "off"))
                else:
                    during = " during start-up" if state == "starting" else ""
                    self._want_ftms = False
                    self.post(("ftms", f"stopped ({FTMS_NAME} exited with code {exit_code}{during})"))
                state = "off"
            if state == "starting" and self._ready.is_set():
                state = "on"
                self.post(("ftms", "on"))
            elif state == "starting" and time.monotonic() >= deadline and self._want_ftms:
                self._want_ftms, state, quiet_exit = False, "off", True
                self.post(("ftms", f"stopped ({FTMS_NAME} did not start within {self.t.port_wait_s:g} s)"))
                worker.send("ftms off")
            elif state in ("starting", "on") and not self._want_ftms:
                worker.send("ftms off")
                state, deadline = "stopping", time.monotonic() + self.t.grace_s
            elif state == "stopping" and time.monotonic() >= deadline:
                self._say(f"FTMS did not stop within {self.t.grace_s:g} s")
                self.post(("ftms", "off"))
                state = "off"
            elif state == "off" and self._want_ftms and not quiet_exit:
                self._ready.clear()
                worker.send("ftms on")
                state, deadline = "starting", time.monotonic() + self.t.port_wait_s
            self._stop.wait(self.t.poll_s)
        return "stopped"

    def _shutdown(self) -> None:
        if self.worker is None:
            return
        grace = 0.0 if self._hurry.is_set() else self.t.grace_s
        try:
            code = self.worker.stop(grace, self.t.kill_after_s, self._hurry)
            self._say(f"worker stopped (exit code {code})")
        except Exception as e:
            self._say(f"could not stop the worker: {e}")

    def _say(self, msg: str) -> None:
        with self._lock:
            try:
                self.out.write(f"[server] {msg}\n")
                self.out.flush()
            except (OSError, ValueError):
                pass
