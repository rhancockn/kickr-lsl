import io
import sys
import threading
import time
from pathlib import Path

from wahoo_lsl.apps.supervisor import LineSink, ServerSupervisor
from wahoo_lsl.dashboard.launcher import Timings

FAKE = str(Path(__file__).with_name("server_fake_worker.py"))
FAST = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)


def worker(order, *switches):
    return {"worker": [sys.executable, FAKE, str(order), *map(str, switches)]}


def order(path):
    return path.read_text().split() if path.exists() else []


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


class Events(list):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def __call__(self, event):
        with self._lock:
            self.append(event)

    def of(self, kind):
        return [p for k, p in list(self) if k == kind]


def make(commands, timings=FAST, ftms_enabled=True):
    events, out = Events(), io.StringIO()
    return ServerSupervisor(commands, out, events, timings, ftms_enabled=ftms_enabled), events, out


def running(tmp_path, *switches, timings=FAST, ftms_enabled=True):
    o = tmp_path / "order"
    sup, events, out = make(worker(o, *(["--no-ftms"] if not ftms_enabled else []), *switches), timings, ftms_enabled)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    return o, sup, events, out


def finish(sup, events):
    sup.stop()
    sup.join(30)
    assert not sup.running
    assert [e for e in events if e[0] == "stopped"] == [("stopped", "stopped")]


def test_ready_line_posts_on_and_stop_orders_ftms_before_cps(tmp_path):
    o, sup, events, out = running(tmp_path)
    wait_for(lambda: events.of("ftms") == ["on"])
    assert "[ftms] " in out.getvalue() and "[cps] " in out.getvalue()  # relayed verbatim, no extra prefix
    sup.stop()
    sup.join(30)
    assert order(o) == ["ftms", "cps"]
    assert ("supervisor", "stopping") in events
    assert events.of("stopped") == ["stopped"]


def test_lines_are_relayed_verbatim(tmp_path):
    o, sup, events, out = running(tmp_path)
    assert "\n[cps] 12:00:00 INFO    wahoo_lsl.cli: cps started\n" in "\n" + out.getvalue()
    finish(sup, events)


def test_ftms_disabled_runs_cps_only(tmp_path):
    o, sup, events, out = running(tmp_path, ftms_enabled=False)
    assert events.of("ftms") == [] and "[ftms]" not in out.getvalue()
    finish(sup, events)
    assert order(o) == ["cps"]


def test_unticking_sends_ftms_off_then_posts_off_and_ticking_restarts(tmp_path):
    o, sup, events, _ = running(tmp_path)
    wait_for(lambda: events.of("ftms") == ["on"])
    sup.set_ftms(False)
    wait_for(lambda: events.of("ftms") == ["on", "off"])
    assert order(o) == ["ftms-off", "ftms"]
    sup.set_ftms(True)
    wait_for(lambda: events.of("ftms") == ["on", "off", "on"])
    assert "ftms-on" in order(o)
    assert sup.running
    finish(sup, events)


def test_unrequested_ftms_exit_posts_stopped_and_keeps_the_session(tmp_path):
    o, sup, events, _ = running(tmp_path, "--ftms-dies", 4, 0.5)
    wait_for(lambda: len(events.of("ftms")) == 2)
    assert events.of("ftms")[1] == "stopped (FTMS service exited with code 4)"
    time.sleep(0.3)
    assert sup.running and len(events.of("ftms")) == 2
    finish(sup, events)


def test_ftms_exit_during_startup_says_so(tmp_path):
    o, sup, events, _ = running(tmp_path, "--no-ready", "--ftms-dies", 4, 0.1)
    wait_for(lambda: len(events.of("ftms")) == 1)
    assert events.of("ftms") == ["stopped (FTMS service exited with code 4 during start-up)"]
    finish(sup, events)


def test_startup_timeout_turns_ftms_off_once(tmp_path):
    t = Timings(port_wait_s=0.5, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)
    o, sup, events, _ = running(tmp_path, "--no-ready", timings=t)
    wait_for(lambda: events.of("ftms"))
    assert events.of("ftms") == ["stopped (FTMS service did not start within 0.5 s)"]
    wait_for(lambda: order(o) == ["ftms-off", "ftms"])  # `ftms off` was sent
    time.sleep(0.3)
    assert events.of("ftms") == ["stopped (FTMS service did not start within 0.5 s)"]  # the exit is not a second post
    finish(sup, events)


def test_ftms_off_that_never_confirms_still_posts_off(tmp_path):
    t = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=0.5, kill_after_s=2.0, poll_s=0.05)
    o, sup, events, out = running(tmp_path, "--ignore-ftms-off", timings=t)  # FTMS never reports its exit
    wait_for(lambda: events.of("ftms") == ["on"])
    sup.set_ftms(False)
    wait_for(lambda: events.of("ftms") == ["on", "off"])
    assert "[server] FTMS did not stop within 0.5 s" in out.getvalue()
    finish(sup, events)


def test_late_exit_after_an_off_timeout_is_not_reported_as_a_failure(tmp_path):
    t = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=0.3, kill_after_s=2.0, poll_s=0.05)
    o, sup, events, out = running(tmp_path, "--late-ftms-off", 1.0, timings=t)
    wait_for(lambda: events.of("ftms") == ["on"])
    sup.set_ftms(False)
    wait_for(lambda: events.of("ftms") == ["on", "off"])  # posted by the off timeout
    wait_for(lambda: order(o) == ["ftms-off", "ftms"])  # then the late exit line arrives
    time.sleep(0.4)
    assert events.of("ftms") == ["on", "off"] and sup.running
    finish(sup, events)


def test_retick_after_an_off_timeout_ignores_the_late_exit_and_waits_for_ready(tmp_path):
    t = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=0.3, kill_after_s=2.0, poll_s=0.05)
    o, sup, events, out = running(tmp_path, "--late-ftms-off", 1.0, timings=t)
    wait_for(lambda: events.of("ftms") == ["on"])
    sup.set_ftms(False)
    wait_for(lambda: events.of("ftms") == ["on", "off"])
    sup.set_ftms(True)
    wait_for(lambda: events.of("ftms") == ["on", "off", "on"])  # old exit ignored, new ready seen
    assert sup.running
    finish(sup, events)


def test_worker_exit_during_startup_ends_the_session_with_its_code(tmp_path):
    sup, events, _ = make(worker(tmp_path / "order", "--exit", 3, 0.1))
    sup.start()
    sup.join(30)
    assert events.of("stopped") == ["CPS logger exited with code 3"]


def test_worker_exit_after_running_ends_the_session(tmp_path):
    o, sup, events, _ = running(tmp_path, "--exit", 5, 1.0)
    sup.join(30)
    assert events.of("stopped") == ["CPS logger exited with code 5"]


def test_second_stop_hurries_a_worker_that_ignores_stop(tmp_path):
    t = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=60.0, kill_after_s=2.0, poll_s=0.05)
    o, sup, events, _ = running(tmp_path, "--ignore-stop", timings=t)
    t0 = time.monotonic()
    sup.stop()
    time.sleep(0.5)
    assert sup.running  # still waiting out the grace period
    sup.stop()
    sup.join(30)
    assert time.monotonic() - t0 < 15
    assert events.of("stopped") == ["stopped"]


def test_stop_during_startup_posts_one_stopped(tmp_path):
    o = tmp_path / "order"
    sup, events, _ = make(worker(o))
    sup.start()
    sup.stop()
    sup.join(30)
    assert events.of("stopped") == ["stopped"]


def test_start_error_is_reported_once(tmp_path):
    sup, events, _ = make({"worker": [str(tmp_path / "missing-exe")]})
    sup.start()
    sup.join(30)
    [reason] = events.of("stopped")
    assert reason.startswith("error: ")


def test_failing_worker_stop_still_posts_one_stopped(tmp_path):
    o, sup, events, _ = running(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("cannot stop")

    proc = sup.worker
    proc.stop = boom
    sup.stop()
    sup.join(30)
    proc.proc.kill()
    proc.proc.wait()
    assert not sup.running
    assert [e for e in events if e[0] == "stopped"] == [("stopped", "stopped")]


def test_line_sink_posts_lines_and_appends_to_file(tmp_path):
    posted = []
    sink = LineSink(posted.append, tmp_path / "logs" / "server.log")
    sink.write("[ftms] a\n")
    sink.write("[cps] b\n[cps] c\n")
    sink.flush()
    sink.close()
    assert posted == [("line", "[ftms] a"), ("line", "[cps] b"), ("line", "[cps] c")]
    assert (tmp_path / "logs" / "server.log").read_text(encoding="utf-8") == "[ftms] a\n[cps] b\n[cps] c\n"
