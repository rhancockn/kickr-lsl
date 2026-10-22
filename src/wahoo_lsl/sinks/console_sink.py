"""Debug sink: print each parsed sample and event (spec §7.5, --debug only)."""
from __future__ import annotations

import sys
from typing import TextIO

from ..pipeline import Event, Sample, Sink


class ConsoleSink(Sink):
    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def handle(self, item: Sample | Event) -> None:
        if isinstance(item, Sample):
            values = [f"{k}={v}" for k, v in item.fields.items()]
            values += [f"{k}={v:.1f}" if v is not None else f"{k}=None" for k, v in item.derived.items()]
            fields = " ".join(values)
            line = f"[{item.seq:>5}] t_lsl={item.t_lsl:.6f}  {fields}"
            if item.parse_error:
                line += f" PARSE_ERROR={item.parse_error} raw={item.raw_hex}"
        else:
            line = f"[event] {item.kind} {item.detail}"
        print(line, file=self._stream, flush=True)
