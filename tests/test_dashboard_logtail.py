import json
import os
import time

import pytest

from wahoo_lsl.dashboard.logtail import LogFollower


def rec(seq, t=1.0):
    return json.dumps({"type": "sample", "seq": seq, "t_lsl": t, "fields": {"power_w": 100}}) + "\n"


class Collect(list):
    def __call__(self, item):
        self.append(item)

    def kinds(self, kind):
        return [p for k, p in self if k == kind]

    def seqs(self):
        return [r["seq"] for r in self.kinds("cps_sample")]


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(posts, log_dir=None, **kw):
        f = LogFollower(tmp_path if log_dir is None else log_dir, posts, **kw)
        made.append(f)
        return f

    yield factory
    for f in made:
        f.stop()


def test_waiting_posted_once_when_no_log(tmp_path, make):
    posts = Collect()
    f = make(posts, log_dir=tmp_path / "logs")
    f.poll_once()
    f.poll_once()
    assert posts == [("cps_status", f"waiting for CPS log in {tmp_path / 'logs'}")]


def test_new_file_read_from_start(tmp_path, make):
    posts = Collect()
    f = make(posts)
    (tmp_path / "other.jsonl").write_text(rec(9))
    (tmp_path / "cps_1.jsonl").write_text(rec(0) + rec(1))
    f.poll_once()
    assert posts.kinds("cps_file") == ["cps_1.jsonl"]
    assert posts.seqs() == [0, 1]


def test_preexisting_file_starts_at_eof(tmp_path, make):
    p = tmp_path / "cps_1.jsonl"
    p.write_text(rec(0))
    posts = Collect()
    f = make(posts)
    f.poll_once()
    assert posts.kinds("cps_file") == ["cps_1.jsonl"]
    assert posts.seqs() == []
    with p.open("a") as fh:
        fh.write(rec(1))
    f.poll_once()
    assert posts.seqs() == [1]


def test_preexisting_partial_line_is_discarded(tmp_path, make):
    p = tmp_path / "cps_1.jsonl"
    p.write_text(rec(0) + '{"type": "sample", "seq": 1')
    posts = Collect()
    f = make(posts)
    f.poll_once()
    with p.open("a") as fh:
        fh.write(', "t_lsl": 1.0, "fields": {}}\n' + rec(2))
    f.poll_once()
    assert posts.seqs() == [2]
    assert f.bad_lines == 0


def test_partial_line_waits_for_newline(tmp_path, make):
    posts = Collect()
    f = make(posts)
    p = tmp_path / "cps_1.jsonl"
    line = rec(0)
    p.write_text(line[:10])
    f.poll_once()
    assert posts.seqs() == []
    with p.open("a") as fh:
        fh.write(line[10:])
    f.poll_once()
    assert posts.seqs() == [0]


def test_switches_to_newer_file_and_reads_it_from_start(tmp_path, make):
    now = time.time()
    old = tmp_path / "cps_1.jsonl"
    old.write_text(rec(0))
    os.utime(old, (now - 100, now - 100))
    posts = Collect()
    f = make(posts)
    f.poll_once()
    new = tmp_path / "cps_2.jsonl"
    new.write_text(rec(5))
    os.utime(new, (now, now))
    f.poll_once()
    assert posts.kinds("cps_file") == ["cps_1.jsonl", "cps_2.jsonl"]
    assert posts.seqs() == [5]


def test_bad_lines_counted_and_posted(tmp_path, make):
    posts = Collect()
    f = make(posts)
    (tmp_path / "cps_1.jsonl").write_bytes(b"not json\n[1]\n\xc3(\n\n" + rec(0).encode())
    f.poll_once()
    assert f.bad_lines == 3
    assert posts.kinds("cps_bad") == [3]
    assert posts.seqs() == [0]


def test_only_connection_events_are_posted(tmp_path, make):
    posts = Collect()
    f = make(posts)
    events = [
        {"type": "event", "kind": "clock_sync", "detail": {}},
        {"type": "event", "kind": "connected", "detail": {"name": "KICKR"}},
        {"type": "event", "kind": "disconnected", "detail": {}},
    ]
    (tmp_path / "cps_1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    f.poll_once()
    assert [e["kind"] for e in posts.kinds("cps_event")] == ["connected", "disconnected"]


def test_thread_follows_and_stops(tmp_path, make):
    posts = Collect()
    f = make(posts, poll_s=0.01)
    f.start()
    (tmp_path / "cps_1.jsonl").write_text(rec(0))
    deadline = time.monotonic() + 2.0
    while not posts.seqs():
        assert time.monotonic() < deadline, "follower thread never posted the sample"
        time.sleep(0.01)
    f.stop()
    assert not f._thread.is_alive()
