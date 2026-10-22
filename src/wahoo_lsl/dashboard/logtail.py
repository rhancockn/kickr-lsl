"""Follow the newest CPS JSONL log and post its samples to the dashboard."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import IO, Any, Callable

Post = Callable[[tuple[str, Any]], None]
CONNECTION_EVENTS = ("connected", "disconnected")


class LogFollower:
    """Polls log_dir for the newest file matching pattern and posts complete lines.

    A file that already existed when the follower was created is read from its end, so an
    old session is never plotted; files that appear later are read from the start.
    """

    def __init__(self, log_dir: str | Path, post: Post, pattern: str = "cps_*.jsonl", poll_s: float = 0.25) -> None:
        self._dir = Path(log_dir)
        self._post = post
        self._pattern = pattern
        self._poll_s = poll_s
        self._preexisting = set(self._candidates())
        self._path: Path | None = None
        self._file: IO[bytes] | None = None
        self._buf = b""
        self._discard_partial = False
        self._waiting_posted = False
        self.bad_lines = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cps-log-follower", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self._close()

    def poll_once(self) -> None:
        newest = self._newest()
        if newest is None:
            if self._path is None and not self._waiting_posted:
                self._post(("cps_status", f"waiting for CPS log in {self._dir}"))
                self._waiting_posted = True
            return
        if newest != self._path:
            self._open(newest)
        self._read()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except OSError as e:
                self._post(("cps_status", f"log read error: {e}"))
                self._close()
            self._stop.wait(self._poll_s)

    def _candidates(self) -> list[Path]:
        try:
            return [p for p in self._dir.glob(self._pattern) if p.is_file()]
        except OSError:
            return []

    def _newest(self) -> Path | None:
        best, best_key = None, None
        for p in self._candidates():
            try:
                key = (p.stat().st_mtime_ns, p.name)
            except OSError:
                continue  # removed between glob and stat
            if best_key is None or key > best_key:
                best, best_key = p, key
        return best

    def _open(self, path: Path) -> None:
        self._close()
        f = open(path, "rb")
        self._discard_partial = False
        if path in self._preexisting:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                self._discard_partial = f.read(1) != b"\n"
        self._file, self._path, self._buf = f, path, b""
        self._waiting_posted = False
        self._post(("cps_file", path.name))

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file, self._path, self._buf = None, None, b""

    def _read(self) -> None:
        assert self._file is not None
        chunk = self._file.read()
        if not chunk:
            return
        *lines, self._buf = (self._buf + chunk).split(b"\n")
        if self._discard_partial and lines:
            lines = lines[1:]  # the tail of a line that was mid-write when we attached
            self._discard_partial = False
        before = self.bad_lines
        for line in lines:
            self._handle(line)
        if self.bad_lines != before:
            self._post(("cps_bad", self.bad_lines))

    def _handle(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            self.bad_lines += 1
            return
        if not isinstance(record, dict):
            self.bad_lines += 1
            return
        if record.get("type") == "sample":
            self._post(("cps_sample", record))
        elif record.get("type") == "event" and record.get("kind") in CONNECTION_EVENTS:
            self._post(("cps_event", record))
