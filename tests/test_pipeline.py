import logging
import queue
import struct

import pytest

from wahoo_lsl import clock
from wahoo_lsl.pipeline import (
    STOP,
    Dispatcher,
    Event,
    Pipeline,
    RawSample,
    Sample,
    Sink,
    make_event,
)
from wahoo_lsl.warn import WarnOnce

POWER_ONLY = struct.pack("<Hh", 0, 200)
WHEEL = struct.pack("<Hh", 1 << 4, 150) + struct.pack("<IH", 10, 2048)
TRUNCATED_WHEEL = struct.pack("<Hh", 1 << 4, 5)


class RecordingSink(Sink):
    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.items: list = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def handle(self, item) -> None:
        self.items.append(item)

    def stop(self) -> None:
        self.stopped = True


class BrokenSink(Sink):
    name = "broken"

    def handle(self, item) -> None:
        raise RuntimeError("boom")


class UnstartableSink(Sink):
    name = "unstartable"

    def start(self) -> None:
        raise OSError("cannot open")

    def handle(self, item) -> None:
        raise AssertionError("must not be called")


def raw(seq: int, data: bytes) -> RawSample:
    return RawSample(seq, 100.0 + seq, 1_700_000_000_000_000_000 + seq, data)


def features_event(unavailable=()) -> Event:
    return Event("features", 99.0, 1_700_000_000_000_000_000, {"unavailable": list(unavailable)})


def run_pipeline(items, sinks, configured=("power",)):
    warn = WarnOnce(logging.getLogger("wahoo_lsl.test"))
    p = Pipeline(sinks, configured, warn)
    p.start()
    for item in items:
        p.put(item)
    assert p.close(timeout=5) is True
    return p


def samples(sink):
    return [i for i in sink.items if isinstance(i, Sample)]


def test_samples_parsed_and_all_fields_forwarded_in_order():
    a, b = RecordingSink("a"), RecordingSink("b")
    run_pipeline([features_event(), raw(0, POWER_ONLY), raw(1, WHEEL)], [a, b], configured=("power",))
    assert a.started and a.stopped and b.started and b.stopped
    assert len(a.items) == len(b.items) == 3
    assert all(x is y for x, y in zip(a.items, b.items))
    assert isinstance(a.items[0], Event) and a.items[0].kind == "features"
    s0, s1 = samples(a)
    assert (s0.seq, s0.t_lsl, s0.t_wall_ns) == (0, 100.0, 1_700_000_000_000_000_000)
    assert s0.fields == {"power_w": 200, "offset_compensation": False}
    assert s1.raw_hex == WHEEL.hex()
    assert s1.flags == 1 << 4
    # no filtering: wheel fields are logged even though only "power" is configured
    assert s1.fields == {"power_w": 150, "wheel_revs": 10, "wheel_event_t": 2048, "offset_compensation": False}
    assert s1.parse_error is None


def test_missing_configured_field_warns_once(caplog):
    sink = RecordingSink()
    with caplog.at_level(logging.WARNING):
        run_pipeline(
            [features_event(), raw(0, POWER_ONLY), raw(1, POWER_ONLY), raw(2, POWER_ONLY)],
            [sink],
            configured=("power", "wheel_revolutions"),
        )
    warnings = [r.getMessage() for r in caplog.records if "not present in notification" in r.getMessage()]
    assert len(warnings) == 1
    assert "wheel_revolutions" in warnings[0]
    assert [s.fields["power_w"] for s in samples(sink)] == [200, 200, 200]


def test_unavailable_measurement_not_warned_again(caplog):
    sink = RecordingSink()
    with caplog.at_level(logging.WARNING):
        run_pipeline(
            [features_event(["wheel_revolutions"]), raw(0, POWER_ONLY)],
            [sink],
            configured=("power", "wheel_revolutions"),
        )
    assert not [r for r in caplog.records if "not present in notification" in r.getMessage()]


def test_parse_error_forwarded_with_raw_and_counted(caplog):
    sink = RecordingSink()
    stopped = Event("stopped", 200.0, 1_700_000_000_000_000_000, {"reason": "user"})
    with caplog.at_level(logging.WARNING):
        run_pipeline([raw(0, TRUNCATED_WHEEL), raw(1, TRUNCATED_WHEEL), stopped], [sink])
    s0, s1 = samples(sink)
    for s in (s0, s1):
        assert s.fields == {}
        assert s.flags == 1 << 4
        assert s.raw_hex == TRUNCATED_WHEEL.hex()
        assert "wheel_revolutions" in s.parse_error
    parse_warnings = [r for r in caplog.records if "parse error" in r.getMessage()]
    assert len(parse_warnings) == 1
    final = sink.items[-1]
    assert final.kind == "stopped"
    assert final.detail == {"reason": "user", "parse_errors": 2}


def _run_dispatcher_without_consumers(n_items: int, depth: int, interval: float, caplog):
    ingest: queue.Queue = queue.Queue()
    sink_q: queue.Queue = queue.Queue()
    d = Dispatcher(
        ingest,
        {"slow": sink_q},
        ("power",),
        WarnOnce(logging.getLogger("wahoo_lsl.test")),
        queue_warn_depth=depth,
        backlog_interval_s=interval,
    )
    for i in range(n_items):
        ingest.put(raw(i, POWER_ONLY))
    ingest.put(STOP)
    with caplog.at_level(logging.WARNING):
        d.run()  # synchronous: no thread needed for this test
    assert sink_q.qsize() == n_items + 1  # samples + STOP, nothing dropped
    return [r.getMessage() for r in caplog.records if "backlog" in r.getMessage()]


def test_backlog_warning_rate_limited(caplog):
    backlog = _run_dispatcher_without_consumers(6, depth=2, interval=10.0, caplog=caplog)
    assert len(backlog) == 1
    assert "slow" in backlog[0] or "ingest" in backlog[0]


def test_backlog_warning_repeats_after_interval(caplog):
    backlog = _run_dispatcher_without_consumers(6, depth=2, interval=0.0, caplog=caplog)
    assert len(backlog) >= 2


def test_failing_sink_does_not_block_others(caplog):
    good = RecordingSink("good")
    with caplog.at_level(logging.ERROR):
        run_pipeline([raw(0, POWER_ONLY), raw(1, POWER_ONLY), raw(2, POWER_ONLY)], [BrokenSink(), good])
    assert len(samples(good)) == 3
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "broken" in r.getMessage()]
    assert len(errors) == 1


def test_sink_that_fails_to_start_is_drained(caplog):
    good = RecordingSink("good")
    with caplog.at_level(logging.ERROR):
        run_pipeline([raw(0, POWER_ONLY)], [UnstartableSink(), good])
    assert len(samples(good)) == 1
    assert any("unstartable" in r.getMessage() for r in caplog.records)


def test_duplicate_sink_names_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        Pipeline([RecordingSink("x"), RecordingSink("x")], (), WarnOnce(logging.getLogger("t")))


def test_make_event_reads_both_clocks():
    before = clock.now_lsl()
    e = make_event("clock_sync", a=1)
    after = clock.now_lsl()
    assert e.kind == "clock_sync"
    assert e.detail == {"a": 1}
    assert before <= e.t_lsl <= after
    assert isinstance(e.t_wall_ns, int)
