"""Queues, dispatcher and sink runners.

BLE callback -> ingest_q -> Dispatcher (parse, derive, warn, fan-out) -> one queue + thread per Sink.
Queues are unbounded: nothing is ever dropped. Items are immutable and shared by all sinks.
"""
from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from . import clock
from .cps import ParseError, parse_measurement
from .derive import CpsDeriver
from .warn import WarnOnce

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RawSample:
    seq: int  # session-monotonic, starts at 0, never reset on reconnect
    t_lsl: float  # pylsl.local_clock() at callback entry
    t_wall_ns: int  # time.time_ns() immediately after
    data: bytes


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    t_lsl: float
    t_wall_ns: int
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Sample:
    seq: int
    t_lsl: float
    t_wall_ns: int
    raw_hex: str
    flags: int | None  # None if the payload was too short to contain flags
    fields: dict  # ALL parsed output keys (no config filtering in MVP)
    parse_error: str | None = None
    derived: dict = field(default_factory=dict)  # derive.CpsDeriver output; {} on parse errors


class _Stop:
    def __repr__(self) -> str:
        return "STOP"


STOP = _Stop()


def make_event(kind: str, **detail: object) -> Event:
    """Create an Event stamped with both clocks, read back-to-back (t_lsl first)."""
    t_lsl = clock.now_lsl()
    t_wall_ns = clock.now_wall_ns()
    return Event(kind, t_lsl, t_wall_ns, dict(detail))


class Sink(ABC):
    """Consumer of Samples and Events. All methods are called on the sink's own thread."""

    name: str = "sink"
    batched: bool = False  # True: the runner passes each drained queue batch to handle_batch()

    def start(self) -> None:
        """Open resources."""

    @abstractmethod
    def handle(self, item: Sample | Event) -> None:
        """Process one item."""

    def handle_batch(self, items: list[Sample | Event]) -> None:
        """Process the items drained from the queue in one go (batched sinks only)."""
        for item in items:
            self.handle(item)

    def stop(self) -> None:
        """Flush and close resources."""


class SinkRunner(threading.Thread):
    """Feeds one sink from its queue until STOP, in drained batches. A failing sink never blocks the dispatcher."""

    def __init__(self, sink: Sink, q: queue.Queue, warn: WarnOnce) -> None:
        super().__init__(name=f"sink-{sink.name}", daemon=True)
        self.sink = sink
        self.queue = q
        self._warn = warn

    def run(self) -> None:
        started = True
        try:
            self.sink.start()
        except Exception:
            log.exception("Sink %s failed to start; its data will be discarded", self.sink.name)
            started = False
        while True:
            batch, stopping = self._next_batch()
            if started and batch:
                self._deliver(batch)
            if stopping:
                break
        if started:
            try:
                self.sink.stop()
            except Exception:
                log.exception("Sink %s failed to stop cleanly", self.sink.name)


    def _next_batch(self) -> tuple[list, bool]:
        """Block for one item, then take whatever else is already queued. True once STOP is seen."""
        batch: list = []
        item = self.queue.get()
        while item is not STOP:
            batch.append(item)
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return batch, False
        return batch, True

    def _deliver(self, batch: list) -> None:
        if self.sink.batched:
            self._call(self.sink.handle_batch, batch)
        else:
            for item in batch:
                self._call(self.sink.handle, item)

    def _call(self, fn: Callable[[Any], None], arg: Any) -> None:
        try:
            fn(arg)
        except Exception as e:
            if self._warn.check(("sink_error", self.sink.name, type(e).__name__)):
                log.exception(
                    "Sink %s failed handling a batch; further %s errors suppressed"
                    if self.sink.batched
                    else "Sink %s failed handling an item; further %s errors suppressed",
                    self.sink.name,
                    type(e).__name__,
                )


class Dispatcher(threading.Thread):
    """Parses RawSamples, derives speed/cadence, applies warn-once checks, fans out to sink queues."""

    def __init__(
        self,
        ingest_q: queue.Queue,
        sink_queues: dict[str, queue.Queue],
        configured: Sequence[str],
        warn: WarnOnce,
        *,
        queue_warn_depth: int = 100,
        backlog_interval_s: float = 10.0,
        deriver: CpsDeriver | None = None,
    ) -> None:
        super().__init__(name="dispatcher", daemon=True)
        self._ingest = ingest_q
        self._sinks = sink_queues
        self._configured = tuple(configured)
        self._warn = warn
        self._depth = queue_warn_depth
        self._interval = backlog_interval_s
        self._unavailable: frozenset[str] = frozenset()
        self._last_backlog_warn: float | None = None
        self._deriver = deriver if deriver is not None else CpsDeriver()
        self.parse_errors = 0

    def run(self) -> None:
        while True:
            item = self._ingest.get()
            if item is STOP:
                for q in self._sinks.values():
                    q.put(STOP)
                return
            if not isinstance(item, (RawSample, Event)):
                log.error("Dispatcher ignoring unexpected item %r", item)
                continue
            try:
                out: Sample | Event = (
                    self._process(item) if isinstance(item, RawSample) else self._on_event(item)
                )
            except Exception as e:
                self._log_error(e)
                if isinstance(item, RawSample):
                    self.parse_errors += 1
                    out = Sample(
                        item.seq,
                        item.t_lsl,
                        item.t_wall_ns,
                        item.data.hex(),
                        None,
                        {},
                        f"internal error: {type(e).__name__}: {e}",
                    )
                else:
                    out = item
            for q in self._sinks.values():
                q.put(out)
            try:
                self._check_backlog()
            except Exception as e:
                self._log_error(e)

    def _log_error(self, e: Exception) -> None:
        if self._warn.check(("dispatcher_error", type(e).__name__)):
            log.exception("Dispatcher failed processing an item; continuing")

    def _on_event(self, event: Event) -> Event:
        if event.kind == "connected":
            self._deriver.reset()  # precedes every connection's first sample; never derive across a link drop
        if event.kind == "features":
            self._unavailable = frozenset(event.detail.get("unavailable", ()))
        if event.kind == "stopped":
            return replace(event, detail={**event.detail, "parse_errors": self.parse_errors})
        return event

    def _process(self, raw: RawSample) -> Sample:
        raw_hex = raw.data.hex()
        try:
            m = parse_measurement(raw.data)
        except ParseError as e:
            self.parse_errors += 1
            msg = str(e)
            self._warn.warn(
                ("parse_error", msg),
                "CP Measurement parse error: %s (raw=%s); further identical errors suppressed",
                msg,
                raw_hex,
            )
            return Sample(raw.seq, raw.t_lsl, raw.t_wall_ns, raw_hex, e.flags, {}, msg)
        for name in self._configured:
            if name in m.present or name in self._unavailable:
                continue
            self._warn.warn(
                ("missing_field", name),
                "Measurement '%s' not present in notification (flags=0x%04x); further occurrences suppressed",
                name,
                m.flags,
            )
        derived = self._deriver.update(raw.t_lsl, m.values)
        return Sample(raw.seq, raw.t_lsl, raw.t_wall_ns, raw_hex, m.flags, dict(m.values), None, derived)

    def _check_backlog(self) -> None:
        depths = {"ingest": self._ingest.qsize(), **{name: q.qsize() for name, q in self._sinks.items()}}
        over = {name: d for name, d in depths.items() if d > self._depth}
        if not over:
            return
        now = clock.now_lsl()
        if self._last_backlog_warn is not None and now - self._last_backlog_warn < self._interval:
            return
        self._last_backlog_warn = now
        log.warning("Queue backlog above %d items: %s", self._depth, over)


class Pipeline:
    """Owns the ingest queue, the dispatcher and one runner per sink."""

    def __init__(
        self,
        sinks: Sequence[Sink],
        configured: Sequence[str],
        warn: WarnOnce,
        *,
        queue_warn_depth: int = 100,
        deriver: CpsDeriver | None = None,
    ) -> None:
        names = [s.name for s in sinks]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate sink names: {names}")
        self.ingest_q: queue.Queue = queue.Queue()
        sink_queues = {s.name: queue.Queue() for s in sinks}
        self.dispatcher = Dispatcher(
            self.ingest_q, sink_queues, configured, warn, queue_warn_depth=queue_warn_depth, deriver=deriver
        )
        self.runners = [SinkRunner(s, sink_queues[s.name], warn) for s in sinks]

    def start(self) -> None:
        for runner in self.runners:
            runner.start()
        self.dispatcher.start()

    def put(self, item: RawSample | Event) -> None:
        """Thread-safe, non-blocking. Used as the BLE layer's emit callback."""
        self.ingest_q.put_nowait(item)

    def queue_sizes(self) -> dict[str, int]:
        return {"ingest": self.ingest_q.qsize(), **{r.name: r.queue.qsize() for r in self.runners}}

    def close(self, timeout: float = 5.0) -> bool:
        """Send STOP, join all threads. Return False if any thread failed to stop in time."""
        self.ingest_q.put(STOP)
        ok = True
        for thread in (self.dispatcher, *self.runners):
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Thread %s did not stop within %.1fs", thread.name, timeout)
                ok = False
        return ok
