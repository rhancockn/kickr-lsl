"""Tk canvas line chart with a fixed 0-300 s sweep axis."""
from __future__ import annotations

import math
import tkinter as tk
from typing import Sequence

MAX_GAP_S = 5.0
SMALL_FONT = "TkSmallCaptionFont"

Point = tuple[float, "float | None"]


def segments(points: Sequence[Point], max_gap_s: float = MAX_GAP_S) -> list[list[tuple[float, float]]]:
    """Split points into drawable runs, breaking at None values and at x gaps > max_gap_s."""
    runs: list[list[tuple[float, float]]] = []
    run: list[tuple[float, float]] = []
    for x, y in points:
        if y is None or (run and x - run[-1][0] > max_gap_s):
            if run:
                runs.append(run)
            run = []
        if y is not None:
            run.append((x, float(y)))
    if run:
        runs.append(run)
    return runs


def nice_ceiling(value: float, floor: float) -> float:
    """Smallest 1/2/5 x 10^k that is >= max(value, floor)."""
    v = max(value, floor)
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        c = m * 10.0**exp
        if c >= v:
            return c
    return 10.0 ** (exp + 1)


class Graph:
    LEFT, RIGHT, TOP, BOTTOM = 44, 10, 18, 18

    def __init__(self, parent: tk.Misc, title: str, unit: str, y_floor: float,
                 window_s: float = 300.0, width: int = 640, height: int = 130, color: str = "#1f77b4") -> None:
        self.title, self.unit, self.y_floor, self.window_s, self.color = title, unit, y_floor, window_s, color
        self.canvas = tk.Canvas(parent, width=width, height=height, bg="white", highlightthickness=0)

    def draw(self, points: Sequence[Point]) -> None:
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() if c.winfo_width() > 1 else int(c.cget("width"))
        h = c.winfo_height() if c.winfo_height() > 1 else int(c.cget("height"))
        x0, x1, top, bottom = self.LEFT, w - self.RIGHT, self.TOP, h - self.BOTTOM
        ymax = nice_ceiling(max((y for _, y in points if y is not None), default=0.0), self.y_floor)

        def sx(x: float) -> float:
            return x0 + (x1 - x0) * x / self.window_s

        def sy(y: float) -> float:
            return bottom - (bottom - top) * max(y, 0.0) / ymax

        c.create_text(x0, 2, text=f"{self.title} ({self.unit})", anchor="nw", font=SMALL_FONT)
        c.create_rectangle(x0, top, x1, bottom, outline="#999")
        for tick in range(0, int(self.window_s) + 1, 60):
            c.create_line(sx(tick), bottom, sx(tick), bottom + 3, fill="#999")
            c.create_text(sx(tick), bottom + 3, text=f"{tick // 60}m", anchor="n", font=SMALL_FONT)
        for frac in (0.0, 0.5, 1.0):
            c.create_text(x0 - 4, sy(ymax * frac), text=f"{ymax * frac:g}", anchor="e", font=SMALL_FONT)
        for run in segments(points):
            coords = [v for x, y in run for v in (sx(x), sy(y))]
            if len(run) == 1:
                cx, cy = coords
                c.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5, fill=self.color, outline="", tags="data")
            else:
                c.create_line(*coords, fill=self.color, width=1.5, tags="data")
