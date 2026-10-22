"""KICKR test dashboard window: target power slider, FTMS read-back and CPS metrics."""
from __future__ import annotations

import argparse
import logging
import math
import queue
import signal
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Any, Callable, Sequence

import pylsl

from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.client import Status
from wahoo_lsl.ftms.slew import MAX_SLEW_W_PER_S

from .graph import Graph
from .logtail import LogFollower
from .metrics import DEFAULT_CIRCUMFERENCE_M, CpsDeriver, History, SweepClock
from .power import PowerWorker

log = logging.getLogger(__name__)

PUMP_MS = 100
REDRAW_MS = 1000
STALE_S = 15.0  # longer than the logger's flush batches (10 samples at ~1 Hz)
DASH = "—"
CPS_PANELS = (  # key, title, unit, y floor, value format
    ("power", "Power", "W", 100, "{:.0f}"),
    ("speed", "Speed", "km/h", 10, "{:.1f}"),
    ("cadence", "Cadence", "rpm", 60, "{:.0f}"),
)


class Dashboard:
    def __init__(
        self,
        root: tk.Tk,
        events: queue.Queue,
        worker: Any,
        follower: Any,
        *,
        max_watts: int = 1000,
        slew_w_per_s: float = 10.0,
        circumference_m: float = DEFAULT_CIRCUMFERENCE_M,
        clock: Callable[[], float] = pylsl.local_clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root, self.events, self.worker, self.follower = root, events, worker, follower
        self._clock, self._monotonic = clock, monotonic
        self._deriver = CpsDeriver(circumference_m)
        self._sweep = SweepClock(clock())
        self.histories = {key: History(self._sweep) for key in ("target", "power", "speed", "cadence")}
        self._last_sent: int | None = None
        self._cps_arrival: float | None = None
        self._service, self._log_file, self._bad = "starting", DASH, 0
        self._closing = False
        self._close_requested = False
        self._build(max_watts, slew_w_per_s)
        self._refresh_status()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._pump_id = root.after(PUMP_MS, self._pump)
        self._redraw_id = root.after(REDRAW_MS, self._redraw)

    def _build(self, max_watts: int, slew_w_per_s: float) -> None:
        root = self.root
        root.title("KICKR test dashboard")
        big = tkfont.nametofont("TkDefaultFont").copy()
        big.configure(size=16, weight="bold")

        ctl = tk.Frame(root, padx=8, pady=6)
        ctl.pack(fill="x")
        tk.Label(ctl, text="Target power (W)").pack(side="left")
        self.slider = tk.Scale(ctl, from_=0, to=max_watts, orient="horizontal", resolution=1, length=420,
                               command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True, padx=6)
        self.zero_button = tk.Button(ctl, text="0 W", command=self._on_zero_button)
        self.zero_button.pack(side="left")
        tk.Label(ctl, text="Slew W/s").pack(side="left", padx=(12, 2))
        self.slew_var = tk.StringVar(value=f"{slew_w_per_s:g}")
        self.slew_box = tk.Spinbox(ctl, from_=0, to=MAX_SLEW_W_PER_S, increment=1, width=6, textvariable=self.slew_var)
        self.slew_box.pack(side="left")
        self.slew_button = tk.Button(ctl, text="Apply", command=self._on_slew_apply)
        self.slew_button.pack(side="left", padx=(2, 0))

        self.target_text = tk.StringVar(value=f"requested {DASH} · confirmed {DASH}")
        self.link_text = tk.StringVar(value=f"link {DASH}")
        self.error_text = tk.StringVar(value="")
        self.set_error_text = tk.StringVar(value="")
        tgt = tk.LabelFrame(root, text="FTMS target (read back)", padx=6, pady=4)
        tgt.pack(fill="x", padx=8, pady=4)
        tk.Label(tgt, textvariable=self.target_text, font=big, anchor="w").pack(fill="x")
        tk.Label(tgt, textvariable=self.link_text, anchor="w").pack(fill="x")
        tk.Label(tgt, textvariable=self.error_text, fg="#b00", anchor="w").pack(fill="x")
        tk.Label(tgt, textvariable=self.set_error_text, fg="#b00", anchor="w").pack(fill="x")
        self.graphs = {"target": Graph(tgt, "Confirmed target", "W", 100)}
        self.graphs["target"].canvas.pack(fill="x", expand=True)

        cps = tk.LabelFrame(root, text="CPS (from log)", padx=6, pady=4)
        cps.pack(fill="both", expand=True, padx=8, pady=4)
        self.cps_state = tk.StringVar(value="CPS: starting")
        tk.Label(cps, textvariable=self.cps_state, anchor="w").pack(fill="x")
        self.cps_values: dict[str, tk.StringVar] = {}
        for key, title, unit, floor, _ in CPS_PANELS:
            row = tk.Frame(cps)
            row.pack(fill="x", pady=2)
            box = tk.Frame(row, width=130)
            box.pack(side="left", fill="y")
            box.pack_propagate(False)
            tk.Label(box, text=f"{title} ({unit})").pack(anchor="w")
            self.cps_values[key] = tk.StringVar(value=DASH)
            tk.Label(box, textvariable=self.cps_values[key], font=big).pack(anchor="w")
            self.graphs[key] = Graph(row, title, unit, floor)
            self.graphs[key].canvas.pack(side="left", fill="x", expand=True)

        self.status_text = tk.StringVar()
        tk.Label(root, textvariable=self.status_text, anchor="w", relief="sunken", padx=4).pack(fill="x", side="bottom")

    # --- target control -------------------------------------------------

    def _on_slider(self, value: str) -> None:
        self._send(int(float(value)))

    def _on_zero_button(self) -> None:
        self._last_sent = None  # always send, even if 0 was the last value
        self._set_target(0)

    def _on_slew_apply(self) -> None:
        try:
            rate = float(self.slew_var.get())
        except ValueError:
            rate = math.nan
        if not 0 <= rate <= MAX_SLEW_W_PER_S:  # also rejects NaN
            self.set_error_text.set(f"slew must be a number 0..{MAX_SLEW_W_PER_S:g} W/s")
            return
        self.worker.request_slew(rate)

    def _set_target(self, watts: int) -> None:
        self.slider.set(watts)
        self._send(watts)

    def _send(self, watts: int) -> None:
        if watts != self._last_sent:
            self._last_sent = watts
            self.worker.request(watts)

    # --- events ---------------------------------------------------------

    def handle(self, kind: str, payload: Any) -> None:
        if kind == "status":
            self._on_status(payload)
        elif kind == "service_connected":
            self._service = "connected"
            self._last_sent = None
            self._set_target(0)  # the service spun down when any previous connection dropped
        elif kind == "service_down":
            self._service = payload
            self.target_text.set(f"requested {DASH} · confirmed {DASH}")
            self.link_text.set(f"link {DASH}")
        elif kind == "set_ok":
            self.set_error_text.set("")
        elif kind == "set_error":
            self.set_error_text.set(payload)
        elif kind == "slew_ok":
            self.set_error_text.set("")
        elif kind == "slew_error":
            self.set_error_text.set(payload)
        elif kind == "service_error":
            self.error_text.set(payload)
        elif kind == "cps_sample":
            self._on_sample(payload)
        elif kind == "cps_event":
            self._deriver.reset()  # the sensor (re)connected: its counters may have restarted
            self.cps_state.set(f"CPS: {payload.get('kind')}")
        elif kind == "cps_file":
            self._deriver.reset()
            self._log_file = payload
        elif kind == "cps_status":
            self.cps_state.set(f"CPS: {payload}")
        elif kind == "cps_bad":
            self._bad = payload
        self._refresh_status()

    def _on_status(self, s: Status) -> None:
        t = s.target
        confirmed = DASH if t.confirmed_w is None else str(t.confirmed_w)
        pending = " (pending)" if t.pending else ""
        if t.ramp_end_t_lsl is None:
            self.target_text.set(f"requested {t.requested_w} W · confirmed {confirmed} W{pending}")
        else:
            left = max(0.0, t.ramp_end_t_lsl - self._clock())
            self.target_text.set(
                f"requested {t.requested_w} W · setpoint {t.setpoint_w} W · confirmed {confirmed} W{pending}"
                f" · ramp {left:.0f} s"
            )
        link = f"link {s.link} (epoch {s.link_epoch})"
        if s.slew is not None:
            link += f" · slew {s.slew.w_per_s:g} W/s"
        self.link_text.set(link)
        self.error_text.set(s.last_error or "")
        self.histories["target"].add(self._clock(), t.confirmed_w)

    def _on_sample(self, record: dict) -> None:
        m = self._deriver.update(record)
        if m is None:
            return
        self._cps_arrival = self._monotonic()
        values = {"power": m.power_w, "speed": m.speed_kmh, "cadence": m.cadence_rpm}
        for key, _, _, _, fmt in CPS_PANELS:
            value = values[key]
            self.histories[key].add(m.t_lsl, value)
            self.cps_values[key].set(DASH if value is None else fmt.format(value))
        self.cps_state.set("CPS: live")

    def _refresh_status(self) -> None:
        self.status_text.set(f"service: {self._service} · log: {self._log_file} · bad lines: {self._bad}")

    # --- timers and lifecycle --------------------------------------------

    def _pump(self) -> None:
        if self._closing:
            return
        try:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle(kind, payload)
                except Exception:
                    log.exception("dashboard: bad %s event skipped", kind)
            if self._cps_arrival is not None and self._monotonic() - self._cps_arrival > STALE_S:
                for var in self.cps_values.values():
                    var.set(DASH)
                self.cps_state.set(f"CPS: stale (no samples for {STALE_S:.0f} s)")
                self._cps_arrival = None
        finally:
            if self._close_requested:
                self.close()
            else:
                self._pump_id = self.root.after(PUMP_MS, self._pump)

    def _redraw(self) -> None:
        if self._closing:
            return
        try:
            self._sweep.advance(self._clock())
            for key, graph in self.graphs.items():
                try:
                    graph.draw(self.histories[key].points())
                except Exception:
                    log.exception("dashboard: drawing %s graph failed", key)
        finally:
            self._redraw_id = self.root.after(REDRAW_MS, self._redraw)

    def request_close(self) -> None:
        """Safe from a signal handler: the next pump closes the window."""
        self._close_requested = True

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for after_id in (self._pump_id, self._redraw_id):
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self.worker.stop()
        self.follower.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wahoo_lsl.dashboard.app", description="KICKR FTMS/CPS test dashboard")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"FTMS service host (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"FTMS service port (default {DEFAULT_PORT})")
    p.add_argument("--log-dir", type=Path, default=Path("logs"), help="CPS log folder (default logs)")
    p.add_argument("--max-watts", type=int, default=1000, help="slider maximum in W (default 1000)")
    p.add_argument("--circumference-m", type=float, default=DEFAULT_CIRCUMFERENCE_M,
                   help=f"wheel circumference for speed (default {DEFAULT_CIRCUMFERENCE_M})")
    p.add_argument("--slew", type=float, default=10.0,
                   help="initial slew rate shown in the slew box, W/s (default 10)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
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
        print(f"wahoo-dashboard: cannot open a window: {e}", file=sys.stderr)
        return 2
    events: queue.Queue = queue.Queue()
    worker = PowerWorker(events.put, args.host, args.port)
    follower = LogFollower(args.log_dir, events.put)
    app = Dashboard(root, events, worker, follower, max_watts=args.max_watts,
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
