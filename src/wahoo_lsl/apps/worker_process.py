"""The server's child: one `wahoo-worker server` process with a stdin command pipe.

Unlike the launcher's Child it keeps stdin open (`ftms on`, `ftms off`, `stop`) and relays the
worker's already-labelled lines verbatim.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import IO, Callable, Sequence

from wahoo_lsl.dashboard.launcher import WIN, _clean_code


class WorkerProcess:
    def __init__(self, argv: Sequence[str], out: IO[str], out_lock: threading.Lock,
                 on_line: Callable[[str], None]) -> None:
        self._signalled = False
        # POSIX: own session, so a terminal Ctrl-C reaches only the server. Windows: own process group.
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
        self.proc = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}, **group,
        )
        self._relay = threading.Thread(target=self._pump, args=(out, out_lock, on_line), name="relay-worker",
                                       daemon=True)
        self._relay.start()

    def _pump(self, out: IO[str], lock: threading.Lock, on_line: Callable[[str], None]) -> None:
        assert self.proc.stdout is not None
        broken = False
        for raw in self.proc.stdout:  # always keep draining so the worker never blocks on a full pipe
            line = raw.rstrip("\r\n")
            if not broken:
                with lock:
                    try:
                        out.write(line + "\n")
                        out.flush()
                    except (OSError, ValueError):
                        broken = True
            try:
                on_line(line)
            except Exception:  # a bad parser must not stop the relay
                pass

    def send(self, command: str) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def poll(self) -> int | None:
        return self.proc.poll()

    def stop(self, grace_s: float, kill_after_s: float, hurry: threading.Event) -> int | None:
        """`stop` on stdin, then wait; terminate, then kill, if the worker does not exit in time."""
        if self.proc.poll() is None:
            self._signalled = True
            self.send("stop")
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
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
        return _clean_code(self.proc.returncode, self._signalled and WIN)
