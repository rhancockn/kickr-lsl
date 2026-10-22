"""wahoo-dashboard: run the FTMS service, the CPS logger and the dashboard as one session.

Start-up: FTMS (wait for its port) -> CPS logger -> dashboard. Any child exiting, the window
closing, or Ctrl-C shuts the rest down in the order dashboard -> FTMS -> CPS logger.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Sequence

from wahoo_lsl.config import ConfigError
from wahoo_lsl.ftms.config import load_ftms_config

from .metrics import DEFAULT_CIRCUMFERENCE_M

WIN = sys.platform == "win32"
SHUTDOWN_ORDER = ("dash", "ftms", "cps")


class LauncherError(Exception):
    pass


@dataclass(frozen=True)
class Timings:
    port_wait_s: float = 15.0
    cps_early_exit_s: float = 2.0
    grace_s: float = 20.0
    kill_after_s: float = 5.0
    poll_s: float = 0.2


def find_script(name: str, scripts_dir: str | Path | None = None) -> Path | None:
    """An installed console script in this environment's scripts folder (never a PATH search)."""
    d = Path(scripts_dir if scripts_dir is not None else sysconfig.get_path("scripts"))
    for candidate in (f"{name}.exe", name) if WIN else (name,):
        path = d / candidate
        if path.is_file():
            return path
    return None


def build_commands(args: argparse.Namespace, host: str, port: int,
                   scripts_dir: str | Path | None = None) -> dict[str, list[str]]:
    ftms, cps = find_script("wahoo-ftms", scripts_dir), find_script("wahoo-lsl", scripts_dir)
    missing = [name for name, path in (("wahoo-ftms", ftms), ("wahoo-lsl", cps)) if path is None]
    if missing:
        where = scripts_dir if scripts_dir is not None else sysconfig.get_path("scripts")
        raise LauncherError(f"{', '.join(missing)} not found in {where}; run `pip install -e .` in this environment")
    debug = ["--debug"] if args.debug else []
    return {
        "ftms": [str(ftms), "serve", "-c", str(args.ftms_config), *debug],
        "cps": [str(cps), "run", "-c", str(args.cps_config), *debug],
        "dash": [sys.executable, "-m", "wahoo_lsl.dashboard.app", "--host", host, "--port", str(port),
                 "--log-dir", str(args.log_dir), "--max-watts", str(args.max_watts),
                 "--circumference-m", str(args.circumference_m)],
    }


class Child:
    """One child process; its merged output is relayed line by line with a [label] prefix."""

    def __init__(self, label: str, argv: Sequence[str], out: IO[str], out_lock: threading.Lock) -> None:
        self.label = label
        # POSIX: own session, so a terminal Ctrl-C reaches only the wrapper (ordered shutdown).
        # Windows: own process group, so CTRL_BREAK_EVENT can target it.
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
        self.proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"}, **group,
        )
        self._relay = threading.Thread(target=self._pump, args=(out, out_lock), name=f"relay-{label}", daemon=True)
        self._relay.start()

    def _pump(self, out: IO[str], lock: threading.Lock) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with lock:
                out.write(f"[{self.label}] {line.rstrip(chr(13) + chr(10))}\n")
                out.flush()

    def poll(self) -> int | None:
        return self.proc.poll()

    def stop(self, grace_s: float, kill_after_s: float, hurry: threading.Event) -> int:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.CTRL_BREAK_EVENT if WIN else signal.SIGINT)
            except OSError:
                pass  # already gone
            deadline = time.monotonic() + grace_s
            while self.proc.poll() is None and time.monotonic() < deadline and not hurry.is_set():
                time.sleep(0.05)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(kill_after_s)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self._relay.join(2.0)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        return self.proc.returncode


class Supervisor:
    def __init__(self, commands: dict[str, list[str]], host: str, port: int, out: IO[str],
                 stop: threading.Event, hurry: threading.Event, timings: Timings = Timings()) -> None:
        self.commands, self.host, self.port, self.out = commands, host, port, out
        self.stop_event, self.hurry, self.t = stop, hurry, timings
        self._lock = threading.Lock()
        self.children: dict[str, Child] = {}
        self.code = 0

    def run(self) -> int:
        try:
            self._start_and_watch()
        finally:
            for label in SHUTDOWN_ORDER:
                child = self.children.get(label)
                if child is not None:
                    grace = 0.0 if self.hurry.is_set() else self.t.grace_s
                    self._note(child.stop(grace, self.t.kill_after_s, self.hurry))
        return self.code

    def _say(self, msg: str) -> None:
        with self._lock:
            self.out.write(f"[wrapper] {msg}\n")
            self.out.flush()

    def _note(self, returncode: int | None) -> None:
        if self.code == 0 and returncode:
            self.code = returncode if returncode > 0 else 1  # killed by a signal -> 1

    def _start(self, label: str) -> Child:
        child = self.children[label] = Child(label, self.commands[label], self.out, self._lock)
        return child

    def _start_and_watch(self) -> None:
        ftms = self._start("ftms")
        ready = self._wait_for_port(ftms)
        if ready == "exited":
            self._say(f"FTMS service exited with code {ftms.poll()} during start-up")
            self._note(ftms.poll())
            return
        if ready == "timeout":
            self._say(f"FTMS service did not open {self.host}:{self.port} within {self.t.port_wait_s:g} s")
            self._note(2)
            return
        if ready == "stopped":
            return
        cps = self._start("cps")
        deadline = time.monotonic() + self.t.cps_early_exit_s
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if cps.poll() is not None:
                self._say(f"CPS logger exited with code {cps.poll()} during start-up")
                self._note(cps.poll())
                return
            self.stop_event.wait(self.t.poll_s)
        if self.stop_event.is_set():
            return
        self._start("dash")
        while not self.stop_event.is_set():
            for label, child in self.children.items():
                returncode = child.poll()
                if returncode is not None:
                    self._say(f"{label} exited with code {returncode}; shutting down")
                    self._note(returncode)
                    return
            self.stop_event.wait(self.t.poll_s)
        self._say("interrupted; shutting down")

    def _wait_for_port(self, ftms: Child) -> str:
        deadline = time.monotonic() + self.t.port_wait_s
        while True:
            if ftms.poll() is not None:
                return "exited"
            if self.stop_event.is_set():
                return "stopped"
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return "ready"
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return "timeout"
            self.stop_event.wait(self.t.poll_s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo-dashboard",
                                description="Run the FTMS service, the CPS logger and the test dashboard together")
    p.add_argument("--ftms-config", type=Path, default=Path("ftms.json"), help="FTMS config (default ftms.json)")
    p.add_argument("--cps-config", type=Path, default=Path("config.json"), help="CPS logger config (default config.json)")
    p.add_argument("--log-dir", type=Path, default=Path("logs"), help="CPS log folder the dashboard follows (default logs)")
    p.add_argument("--debug", action="store_true", help="pass --debug to both services")
    p.add_argument("--max-watts", type=int, default=1000, help="dashboard slider maximum in W (default 1000)")
    p.add_argument("--circumference-m", type=float, default=DEFAULT_CIRCUMFERENCE_M,
                   help=f"wheel circumference for speed (default {DEFAULT_CIRCUMFERENCE_M})")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_ftms_config(args.ftms_config).ftms
        commands = build_commands(args, settings.host, settings.port)
    except (ConfigError, LauncherError) as e:
        print(f"wahoo-dashboard: {e}", file=sys.stderr)
        return 2
    stop, hurry = threading.Event(), threading.Event()

    def on_signal(signum: int, frame: object) -> None:
        (hurry if stop.is_set() else stop).set()  # a second interrupt skips the grace periods

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, on_signal)
    return Supervisor(commands, settings.host, settings.port, sys.stdout, stop, hurry).run()


if __name__ == "__main__":
    raise SystemExit(main())
