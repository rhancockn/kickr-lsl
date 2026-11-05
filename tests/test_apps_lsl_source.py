import gc
import math
import queue
import time
import uuid

import pylsl
import pytest

from wahoo_lsl.apps.lsl_source import WAITING, LslFollower, channel_labels, to_record
from wahoo_lsl.dashboard.metrics import CpsDeriver

LABELS = ("power_w", "wheel_revs", "wheel_event_t")


def outlet(source_id, stream_type, labels=LABELS):
    info = pylsl.StreamInfo(f"name-{source_id}", stream_type, len(labels), 0, "double64", source_id)
    channels = info.desc().append_child("channels")
    for label in labels:
        channels.append_child("channel").append_child_value("label", label)
    return pylsl.StreamOutlet(info)


def collect(q, until, timeout=10.0, push=None):
    """Drain q into a list until until(events) holds; call push() between polls."""
    events, deadline = [], time.monotonic() + timeout
    while not until(events):
        assert time.monotonic() < deadline, f"timed out; got {events}"
        if push is not None:
            push()
        try:
            events.append(q.get(timeout=0.05))
        except queue.Empty:
            pass
    return events


def stop_quickly(follower):
    t0 = time.monotonic()
    follower.stop()
    assert time.monotonic() - t0 < 5.0


def test_to_record_drops_nan_and_makes_whole_numbers_int():
    rec = to_record(1.5, ["a", "b", "c"], [math.nan, 3.0, 2.5])
    assert rec == {"t_lsl": 1.5, "fields": {"b": 3, "c": 2.5}}
    assert isinstance(rec["fields"]["b"], int)


def test_channel_labels_fall_back_to_index():
    info = pylsl.StreamInfo("n", "T", 2, 0, "double64", "sid")
    assert channel_labels(info) == ["0", "1"]


def test_lsl_records_drive_the_deriver_like_log_records():
    log = [{"t_lsl": 1.0 + i, "fields": {"power_w": 150, "wheel_revs": 10 + i, "wheel_event_t": 2048 * (i + 1)}}
           for i in range(3)]
    lsl = [to_record(r["t_lsl"], LABELS, [float(r["fields"][k]) for k in LABELS]) for r in log]
    assert lsl == log
    a, b = CpsDeriver(), CpsDeriver()
    assert [a.update(r) for r in log] == [b.update(r) for r in lsl]


def test_follows_a_stream_by_source_id():
    sid = f"test-{uuid.uuid4().hex}"
    out = outlet(sid, "CPSTEST")
    q = queue.Queue()
    f = LslFollower(q.put, source_id=sid, resolve_s=0.5, pull_s=0.1)
    f.start()
    try:
        events = collect(q, lambda ev: ("cps_event", {"kind": "connected"}) in ev)
        assert ("cps_file", f"name-{sid}") in events
        events = collect(q, lambda ev: any(k == "cps_sample" for k, _ in ev),
                         push=lambda: out.push_sample([150.0, 10.0, 2048.0]))
        sample = next(p for k, p in events if k == "cps_sample")
        assert sample["fields"] == {"power_w": 150, "wheel_revs": 10, "wheel_event_t": 2048}
    finally:
        stop_quickly(f)
        del out


def test_waits_for_a_missing_stream_and_stops_promptly():
    q = queue.Queue()
    f = LslFollower(q.put, source_id=f"absent-{uuid.uuid4().hex}", resolve_s=0.2, pull_s=0.1)
    f.start()
    collect(q, lambda ev: ("cps_status", WAITING) in ev)
    stop_quickly(f)


def test_by_type_names_the_chosen_stream_among_several():
    stype = f"CPSTEST-{uuid.uuid4().hex[:8]}"
    sids = sorted(f"test-{uuid.uuid4().hex}" for _ in range(2))
    outs = [outlet(s, stype) for s in sids]
    q = queue.Queue()
    f = LslFollower(q.put, stream_type=stype, resolve_s=1.0, pull_s=0.1)
    f.start()
    try:
        events = collect(q, lambda ev: any(k == "cps_file" for k, _ in ev))
        assert ("cps_file", f"name-{sids[0]} (1 of 2 {stype} streams)") in events
    finally:
        stop_quickly(f)
        del outs


def test_reports_disconnected_when_the_outlet_goes_away():
    sid = f"test-{uuid.uuid4().hex}"
    out = outlet(sid, "CPSTEST")
    q = queue.Queue()
    f = LslFollower(q.put, source_id=sid, resolve_s=0.5, pull_s=0.1)
    f.start()
    try:
        collect(q, lambda ev: ("cps_event", {"kind": "connected"}) in ev)
        del out
        gc.collect()
        collect(q, lambda ev: ("cps_event", {"kind": "disconnected"}) in ev, timeout=20.0)
    finally:
        stop_quickly(f)
