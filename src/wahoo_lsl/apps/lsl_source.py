"""Follow a CPS LSL stream and post its samples as the dashboard's follower events.

Posts what LogFollower posts: ("cps_status", text), ("cps_file", stream name),
("cps_event", {"kind": "connected" | "disconnected"}) and ("cps_sample", {"t_lsl": t, "fields": {...}}).
Channel labels are the log's field keys, so the dashboard's CpsDeriver works unchanged.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Sequence

import pylsl
from pylsl.util import LostError

Post = Callable[[tuple[str, Any]], None]
STREAM_TYPE = "CPS"
WAITING = "waiting for CPS LSL stream"
FORGET_AFTER_S = 2.0  # how long the resolver keeps listing a stream after its outlet vanishes


def channel_labels(info: pylsl.StreamInfo) -> list[str]:
    """desc/channels/channel/label per channel; the channel index where a label is missing."""
    labels = []
    channel = info.desc().child("channels").child("channel")
    for i in range(info.channel_count()):
        labels.append(channel.child_value("label") or str(i))
        channel = channel.next_sibling()
    return labels


def to_record(t: float, labels: Sequence[str], values: Sequence[float]) -> dict:
    """A log-style sample record: NaN channels dropped, whole-number values as int (counters, power)."""
    fields: dict[str, Any] = {}
    for label, value in zip(labels, values):
        if math.isnan(value):
            continue
        fields[label] = int(value) if float(value).is_integer() else value
    return {"t_lsl": t, "fields": fields}


class LslFollower:
    def __init__(self, post: Post, source_id: str | None = None, stream_type: str = STREAM_TYPE,
                 resolve_s: float = 1.0, pull_s: float = 0.5) -> None:
        self._post = post
        self.source_id, self.stream_type = source_id, stream_type
        self.resolve_s, self.pull_s = resolve_s, pull_s
        self._stopping = threading.Event()
        self._connected = False
        self._thread = threading.Thread(target=self._run, name="lsl-follower", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(self.resolve_s + self.pull_s + 5.0)

    def _run(self) -> None:
        try:
            holder = [self._new_resolver()]  # holder must be the resolver's only owner (see finally)
        except Exception as e:
            self._post(("cps_status", f"LSL error: {e}"))
            return
        try:
            self._loop(holder[0])
        finally:
            # liblsl takes seconds to destroy a resolver; let a throwaway thread pay that, not stop()
            threading.Thread(target=holder.clear, name="lsl-resolver-close", daemon=True).start()

    def _new_resolver(self) -> pylsl.ContinuousResolver:
        if self.source_id:
            return pylsl.ContinuousResolver("source_id", self.source_id, forget_after=FORGET_AFTER_S)
        return pylsl.ContinuousResolver("type", self.stream_type, forget_after=FORGET_AFTER_S)

    def _loop(self, resolver: pylsl.ContinuousResolver) -> None:
        waiting = False
        lost: set[str] = set()  # uids of streams whose outlet vanished, skipped until the resolver forgets them
        while not self._stopping.is_set():
            found = self._resolve(resolver, lost)
            if found is None:
                if not waiting:
                    self._post(("cps_status", WAITING))
                    waiting = True
                continue
            waiting = False
            try:
                self._follow(*found)
            except LostError:
                lost.add(found[0].uid())
                if self._connected:
                    self._post(("cps_event", {"kind": "disconnected"}))
            except Exception as e:
                self._post(("cps_status", f"LSL error: {e}"))
                self._stopping.wait(self.resolve_s)
            self._connected = False

    def _resolve(self, resolver: pylsl.ContinuousResolver, lost: set[str]) -> tuple[pylsl.StreamInfo, int] | None:
        """The stream to follow and how many matched; None if none within resolve_s (or on stop)."""
        started = time.monotonic()
        while not self._stopping.is_set():
            streams = resolver.results()
            lost &= {info.uid() for info in streams}  # forgotten: a restarted outlet gets a new uid anyway
            streams = [info for info in streams if info.uid() not in lost]
            elapsed = time.monotonic() - started
            # by type, wait out resolve_s so every stream that answers is counted
            if streams and (self.source_id or elapsed >= self.resolve_s):
                streams.sort(key=lambda info: info.source_id())
                return streams[0], len(streams)
            if elapsed >= self.resolve_s:
                return None
            self._stopping.wait(0.1)
        return None

    def _follow(self, info: pylsl.StreamInfo, matched: int) -> None:
        inlet = pylsl.StreamInlet(info, recover=False)
        try:
            full = inlet.info(timeout=2.0)
            labels = channel_labels(full)
            try:
                offset = inlet.time_correction(timeout=2.0)
            except LostError:
                raise
            except Exception:  # pylsl's TimeoutError: assume the same clock (same machine)
                offset = 0.0
            name = full.name() if matched == 1 else f"{full.name()} (1 of {matched} {self.stream_type} streams)"
            self._post(("cps_file", name))
            self._post(("cps_event", {"kind": "connected"}))
            self._connected = True
            while not self._stopping.is_set():
                values, t = inlet.pull_sample(timeout=self.pull_s)
                if values is not None:
                    self._post(("cps_sample", to_record(t + offset, labels, values)))
        finally:
            inlet.close_stream()
