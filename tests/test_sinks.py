import io
import json
import re
from datetime import datetime
from pathlib import Path

from wahoo_lsl.pipeline import Event, Sample
from wahoo_lsl.sinks import ConsoleSink, LogSink, event_record, format_log_path, sample_record

WALL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def sample(seq=0, parse_error=None) -> Sample:
    fields = {} if parse_error else {"power_w": 165, "wheel_revs": 1203}
    return Sample(seq, 12345.678901234, 1_700_000_000_123_456_789, "0000a500", 0, fields, parse_error)


def event(kind="disconnected") -> Event:
    return Event(kind, 12350.1, 1_700_000_000_000_000_000, {"address": "AA:BB:CC:DD:EE:FF"})


def test_format_log_path():
    p = format_log_path("logs/cps_{start:%Y%m%d_%H%M%S}.jsonl", datetime(2026, 10, 20, 18, 1, 2))
    assert p == Path("logs/cps_20261020_180102.jsonl")


def test_sample_record_shape():
    rec = sample_record(sample())
    assert rec == {
        "type": "sample",
        "seq": 0,
        "t_lsl": 12345.678901234,
        "t_wall": "2023-11-14T22:13:20.123456+00:00",
        "raw": "0000a500",
        "flags": 0,
        "fields": {"power_w": 165, "wheel_revs": 1203},
        "derived": {},
    }


def test_sample_record_includes_derived():
    s = Sample(0, 1.0, 1_700_000_000_123_456_789, "", 0, {}, None, {"speed_kmh": 30.5, "cadence_rpm": None})
    assert sample_record(s)["derived"] == {"speed_kmh": 30.5, "cadence_rpm": None}


def test_console_sink_prints_derived():
    out = io.StringIO()
    s = Sample(7, 1.0, 0, "", 0, {"power_w": 1}, None, {"speed_kmh": 30.54, "cadence_rpm": None})
    ConsoleSink(out).handle(s)
    assert out.getvalue().rstrip("\n").endswith("power_w=1 speed_kmh=30.5 cadence_rpm=None")


def test_sample_record_includes_parse_error_only_when_set():
    assert "parse_error" not in sample_record(sample())
    assert sample_record(sample(parse_error="truncated"))["parse_error"] == "truncated"


def test_event_record_shape():
    rec = event_record(event())
    assert rec["type"] == "event"
    assert rec["kind"] == "disconnected"
    assert rec["t_lsl"] == 12350.1
    assert WALL_RE.match(rec["t_wall"])
    assert rec["detail"] == {"address": "AA:BB:CC:DD:EE:FF"}


def test_log_sink_writes_jsonl_and_creates_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "log.jsonl"
    sink = LogSink(path, flush_every_n=10)
    sink.start()
    sink.handle(sample(0))
    sink.handle(event())
    sink.handle(sample(1, parse_error="truncated payload"))
    sink.stop()
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["type"] for r in records] == ["sample", "event", "sample"]
    assert records[0]["t_lsl"] == 12345.678901234  # full float precision survives
    assert records[2]["parse_error"] == "truncated payload"


def test_log_sink_flush_policy(tmp_path):
    path = tmp_path / "log.jsonl"
    sink = LogSink(path, flush_every_n=3)
    sink.start()
    sink.handle(sample(0))
    sink.handle(sample(1))
    assert path.read_text(encoding="utf-8") == ""  # buffered
    sink.handle(sample(2))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3  # flushed at N
    sink.handle(sample(3))
    sink.handle(event())  # events flush immediately
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5
    sink.stop()


def test_log_sink_appends_to_existing_file(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"type":"event"}\n', encoding="utf-8")
    sink = LogSink(path)
    sink.start()
    sink.handle(sample())
    sink.stop()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_console_sink_formats_samples_and_events():
    out = io.StringIO()
    sink = ConsoleSink(out)
    sink.handle(sample(42))
    sink.handle(sample(43, parse_error="truncated"))
    sink.handle(event())
    lines = out.getvalue().splitlines()
    assert lines[0] == "[   42] t_lsl=12345.678901  power_w=165 wheel_revs=1203"
    assert "PARSE_ERROR=truncated" in lines[1] and "raw=0000a500" in lines[1]
    assert lines[2].startswith("[event] disconnected ")
