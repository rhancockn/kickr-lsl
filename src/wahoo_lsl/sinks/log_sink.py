"""JSONL data log sink (spec §7.5): one JSON object per line."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import IO

from ..clock import wall_iso
from ..pipeline import Event, Sample, Sink


def format_log_path(template: str, start: datetime) -> Path:
    """Expand ``{start:<strftime>}`` in the configured log path."""
    return Path(template.format(start=start))


def sample_record(s: Sample) -> dict:
    record = {
        "type": "sample",
        "seq": s.seq,
        "t_lsl": s.t_lsl,
        "t_wall": wall_iso(s.t_wall_ns),
        "raw": s.raw_hex,
        "flags": s.flags,
        "fields": s.fields,
    }
    if s.parse_error is not None:
        record["parse_error"] = s.parse_error
    return record


def event_record(e: Event) -> dict:
    return {
        "type": "event",
        "kind": e.kind,
        "t_lsl": e.t_lsl,
        "t_wall": wall_iso(e.t_wall_ns),
        "detail": e.detail,
    }


class LogSink(Sink):
    name = "log"

    def __init__(self, path: Path, flush_every_n: int = 10) -> None:
        self.path = Path(path)
        self._flush_every_n = flush_every_n
        self._file: IO[str] | None = None
        self._unflushed = 0

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8", newline="\n")

    def handle(self, item: Sample | Event) -> None:
        if isinstance(item, Sample):
            self._write(sample_record(item))
            self._unflushed += 1
            if self._unflushed >= self._flush_every_n:
                self._flush()
        else:
            self._write(event_record(item))
            self._flush()

    def stop(self) -> None:
        if self._file is not None:
            self._flush()
            self._file.close()
            self._file = None

    def _write(self, record: dict) -> None:
        assert self._file is not None, "LogSink.start() was not called"
        # json uses repr() for floats, so t_lsl keeps full precision.
        self._file.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def _flush(self) -> None:
        assert self._file is not None
        self._file.flush()
        self._unflushed = 0
