"""Trainer Dashboard: the KICKR test dashboard fed by the CPS LSL stream instead of the log file."""
from __future__ import annotations

import argparse
import queue
import signal
import sys
import tkinter as tk
from pathlib import Path
from typing import Sequence

from wahoo_lsl.config import ConfigError, load_config
from wahoo_lsl.dashboard.app import Dashboard
from wahoo_lsl.dashboard.metrics import DEFAULT_CIRCUMFERENCE_M
from wahoo_lsl.dashboard.power import PowerWorker
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.config import load_ftms_config
from wahoo_lsl.ftms.slew import MAX_SLEW_W_PER_S

from .lsl_source import LslFollower
from .paths import CPS_CONFIG, FTMS_CONFIG, connect_host, data_dir

LOG_TITLE, LSL_TITLE = "CPS (from log)", "CPS (LSL)"


class LslDashboard(Dashboard):
    """The test dashboard with LSL wording: the CPS frame title and a status bar naming the stream."""

    def _build(self, max_watts: int, slew_w_per_s: float) -> None:
        super()._build(max_watts, slew_w_per_s)
        for widget in self.root.winfo_children():
            if isinstance(widget, tk.LabelFrame) and widget.cget("text") == LOG_TITLE:
                widget.configure(text=LSL_TITLE)

    def _refresh_status(self) -> None:
        self.status_text.set(f"service: {self._service} · stream: {self._log_file}")


def defaults(home: Path) -> dict:
    """Defaults from the server's configs in home; built-in defaults for a missing or invalid file."""
    d = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "slew": 10.0, "circumference_m": DEFAULT_CIRCUMFERENCE_M}
    try:
        s = load_ftms_config(home / FTMS_CONFIG).ftms
        d.update(host=connect_host(s.host), port=s.port, slew=s.slew_w_per_s)
    except ConfigError:
        pass
    try:
        d["circumference_m"] = load_config(home / CPS_CONFIG, require_measurements=False).derive.wheel_circumference_m
    except ConfigError:
        pass
    return d


def build_parser(d: dict) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo-dashboard-app", description="KICKR test dashboard fed by the CPS LSL stream")
    p.add_argument("--host", default=d["host"], help="FTMS service host (default %(default)s)")
    p.add_argument("--port", type=int, default=d["port"], help="FTMS service port (default %(default)s)")
    p.add_argument("--max-watts", type=int, default=1000, help="slider maximum in W (default %(default)s)")
    p.add_argument("--circumference-m", type=float, default=d["circumference_m"],
                   help="wheel circumference for speed (default %(default)s)")
    p.add_argument("--slew", type=float, default=d["slew"], help="initial slew rate in W/s (default %(default)s)")
    p.add_argument("--stream", default=None, help="CPS stream source_id (default: the first CPS stream found)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(defaults(data_dir()))
    args = parser.parse_args(argv)
    if args.max_watts <= 0:
        parser.error("--max-watts must be positive")
    if not args.circumference_m > 0:
        parser.error("--circumference-m must be positive")
    if not 0 <= args.slew <= MAX_SLEW_W_PER_S:
        parser.error(f"--slew must be 0..{MAX_SLEW_W_PER_S:g}")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        if sys.stderr is not None:  # None in the windowed build
            print(f"wahoo-dashboard-app: cannot open a window: {e}", file=sys.stderr)
        return 2
    events: queue.Queue = queue.Queue()
    worker = PowerWorker(events.put, args.host, args.port)
    follower = LslFollower(events.put, source_id=args.stream)
    app = LslDashboard(root, events, worker, follower, max_watts=args.max_watts,
                       slew_w_per_s=args.slew, circumference_m=args.circumference_m)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: app.request_close())
    worker.start()
    follower.start()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
