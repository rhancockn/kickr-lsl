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
