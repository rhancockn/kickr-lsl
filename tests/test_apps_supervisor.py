import io
import socket
import sys
import threading
import time
from pathlib import Path

from wahoo_lsl.apps.supervisor import LineSink, ServerSupervisor
from wahoo_lsl.dashboard.launcher import Timings

FAKE = str(Path(__file__).with_name("dashboard_fake_child.py"))
FAST = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake(label, order, *mode):
    return [sys.executable, FAKE, label, str(order), *map(str, mode)]


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


def make(commands, port, timings=FAST, confirm=None, ftms_enabled=True):
    events, out = Events(), io.StringIO()
    sup = ServerSupervisor(commands, "127.0.0.1", port, out, events, timings,
                           confirm=confirm if confirm is not None else (lambda h, p: None),
                           ftms_enabled=ftms_enabled)
    return sup, events, out


def happy(tmp_path, port, cps_mode=()):
    o = tmp_path / "order"
    return o, {"ftms": fake("ftms", o, "listen", port), "cps": fake("cps", o, *cps_mode)}


def test_starts_ftms_then_cps_and_stops_ftms_first(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    sup, events, out = make(cmds, port, confirm=lambda h, p: calls.append(order(o)))
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    wait_for(lambda: "[cps] cps started" in out.getvalue())
    assert out.getvalue().index("[ftms] ftms started") < out.getvalue().index("[cps] cps started")
    assert events.of("ftms") == ["on"]
    sup.stop()
    sup.join(30)
    assert not sup.running
    assert calls == [[]]  # 0 W check ran before either child stopped
    assert order(o) == ["ftms", "cps"]
    assert ("supervisor", "stopping") in events
    assert events.of("stopped") == ["stopped"]


def test_ftms_disabled_runs_cps_only(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    sup, events, out = make(cmds, port, confirm=lambda h, p: calls.append(1), ftms_enabled=False)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    sup.stop()
    sup.join(30)
    assert "[ftms]" not in out.getvalue()
    assert calls == [] and order(o) == ["cps"]


def test_cps_exit_ends_the_session_and_stops_ftms(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, cps_mode=("exit", 3, 0.3))
    sup, events, _ = make(cmds, port)
    sup.start()
    sup.join(30)
    assert events.of("stopped") == ["CPS logger exited with code 3"]
    assert order(o) == ["ftms"]  # FTMS was stopped gracefully


def test_ftms_exit_during_startup_keeps_cps(tmp_path):
    port, o = free_port(), tmp_path / "order"
    sup, events, out = make({"ftms": fake("ftms", o, "exit", 4, 0.1), "cps": fake("cps", o)}, port)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    assert events.of("ftms") == ["stopped (FTMS service exited with code 4 during start-up)"]
    assert sup.running
    sup.stop()
    sup.join(30)
    assert order(o) == ["cps"]
    assert events.of("stopped") == ["stopped"]


def test_ftms_exit_while_running_keeps_cps(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    sup, events, _ = make(cmds, port)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    sup.children["ftms"].proc.kill()
    wait_for(lambda: len(events.of("ftms")) == 2)
    assert events.of("ftms")[1].startswith("stopped (FTMS service exited with code ")
    assert sup.running and "ftms" not in sup.children
    sup.stop()
    sup.join(30)
    assert order(o) == ["cps"]


def test_port_timeout_turns_ftms_off_without_zero_check(tmp_path):
    port, o = free_port(), tmp_path / "order"
    calls = []
    t = Timings(port_wait_s=0.5, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)
    sup, events, _ = make({"ftms": fake("ftms", o), "cps": fake("cps", o)}, port, t,
                          confirm=lambda h, p: calls.append(1))
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    assert events.of("ftms") == [f"stopped (FTMS service did not open 127.0.0.1:{port} within 0.5 s)"]
    assert order(o) == ["ftms"]  # the silent FTMS child was stopped
    sup.stop()
    sup.join(30)
    assert calls == [] and order(o) == ["ftms", "cps"]


def test_set_ftms_off_then_on_keeps_the_same_cps_process(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    sup, events, _ = make(cmds, port, confirm=lambda h, p: calls.append(1))
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    cps_pid = sup.children["cps"].proc.pid
    sup.set_ftms(False)
    wait_for(lambda: events.of("ftms") == ["on", "off"])
    assert calls == [1] and order(o) == ["ftms"]
    sup.set_ftms(True)
    wait_for(lambda: events.of("ftms") == ["on", "off", "on"])
    assert sup.children["cps"].proc.pid == cps_pid
    sup.stop()
    sup.join(30)
    assert order(o) == ["ftms", "ftms", "cps"]


def test_second_stop_skips_grace(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, cps_mode=("ignore",))
    t = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=60.0, kill_after_s=2.0, poll_s=0.05)
    sup, events, _ = make(cmds, port, t)
    sup.start()
    wait_for(lambda: ("supervisor", "running") in events)
    time.sleep(0.3)  # let the cps child install its ignore handler
    t0 = time.monotonic()
    sup.stop()
    wait_for(lambda: order(o) == ["ftms"])
    sup.stop()
    sup.join(30)
    assert time.monotonic() - t0 < 15
    assert events.of("stopped") == ["stopped"]


def test_start_error_is_reported(tmp_path):
    port = free_port()
    sup, events, _ = make({"ftms": [str(tmp_path / "missing-exe")], "cps": ["x"]}, port)
    sup.start()
    sup.join(30)
    [reason] = events.of("stopped")
    assert reason.startswith("error: ")


def test_line_sink_posts_lines_and_appends_to_file(tmp_path):
    posted = []
    sink = LineSink(posted.append, tmp_path / "logs" / "server.log")
    sink.write("[ftms] a\n")
    sink.write("[cps] b\n[cps] c\n")
    sink.flush()
    sink.close()
    assert posted == [("line", "[ftms] a"), ("line", "[cps] b"), ("line", "[cps] c")]
    assert (tmp_path / "logs" / "server.log").read_text(encoding="utf-8") == "[ftms] a\n[cps] b\n[cps] c\n"
