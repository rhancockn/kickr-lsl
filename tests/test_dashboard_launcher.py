import dataclasses
import io
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from ftms_stub import StubService, connect_reply, snapshot

from wahoo_lsl.dashboard import launcher
from wahoo_lsl.dashboard.launcher import (LauncherError, Supervisor, Timings, build_commands, confirm_zero,
                                          find_script, timings_for)
from wahoo_lsl.ftms.config import parse_ftms_config

FAKE = str(Path(__file__).with_name("dashboard_fake_child.py"))
FAST = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake(label, order, *mode):
    return [sys.executable, FAKE, label, str(order), *map(str, mode)]


def start(commands, port, timings=FAST, out=None, confirm=lambda host, port: True):
    ns = SimpleNamespace(out=out or io.StringIO(), stop=threading.Event(), hurry=threading.Event(), code=None)
    sup = Supervisor(commands, "127.0.0.1", port, ns.out, ns.stop, ns.hurry, timings, confirm=confirm)

    def run():
        ns.code = sup.run()

    ns.thread = threading.Thread(target=run)
    ns.thread.start()
    return ns


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def finish(ns, timeout=30.0):
    ns.thread.join(timeout)
    assert not ns.thread.is_alive(), "supervisor did not finish"
    return ns.code


def order(path):
    return path.read_text().split() if path.exists() else []


def happy(tmp_path, port, dash_mode=()):
    o = tmp_path / "order"
    return o, {"ftms": fake("ftms", o, "listen", port), "cps": fake("cps", o), "dash": fake("dash", o, *dash_mode)}


def test_stop_shuts_down_dash_then_ftms_then_cps(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    ns = start(cmds, port)
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 0
    assert order(o) == ["dash", "ftms", "cps"]
    out = ns.out.getvalue()
    assert "[ftms] ftms started" in out and "[cps] cps started" in out and "[ftms] ftms stopping" in out


def test_dashboard_exit_shuts_down_the_rest(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("exit", 0, 0.2))
    assert finish(start(cmds, port)) == 0
    assert order(o) == ["ftms", "cps"]


def test_first_nonzero_exit_code_is_returned(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("exit", 4, 0.2))
    assert finish(start(cmds, port)) == 4
    assert order(o) == ["ftms", "cps"]


def test_ftms_exit_during_startup_returns_its_code(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o, "exit", 3, 0), "cps": fake("cps", o), "dash": fake("dash", o)}
    ns = start(cmds, port)
    assert finish(ns) == 3
    assert "[cps]" not in ns.out.getvalue()


def test_port_timeout_returns_2_and_stops_ftms(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o), "cps": fake("cps", o), "dash": fake("dash", o)}
    ns = start(cmds, port, Timings(port_wait_s=0.5, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05))
    assert finish(ns) == 2
    assert order(o) == ["ftms"]
    assert "did not open" in ns.out.getvalue() and "[cps]" not in ns.out.getvalue()


def test_cps_early_exit_returns_its_code(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o, "listen", port), "cps": fake("cps", o, "exit", 5, 0), "dash": fake("dash", o)}
    ns = start(cmds, port, dataclasses.replace(FAST, cps_early_exit_s=3.0))
    assert finish(ns) == 5
    assert order(o) == ["ftms"]
    assert "[dash]" not in ns.out.getvalue()


def test_stop_during_port_wait_starts_nothing_else(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o), "cps": fake("cps", o), "dash": fake("dash", o)}
    ns = start(cmds, port, Timings(port_wait_s=30.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05))
    wait_for(lambda: "[ftms] ftms started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 0
    assert order(o) == ["ftms"]


def test_child_ignoring_interrupt_is_terminated(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("ignore",))
    ns = start(cmds, port, Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=0.3, kill_after_s=2.0, poll_s=0.05))
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 1  # terminated, not a clean exit
    assert order(o) == ["ftms", "cps"]


def test_hurry_skips_grace(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port, ("ignore",))
    ns = start(cmds, port, Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=60.0, kill_after_s=2.0, poll_s=0.05))
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    started = time.monotonic()
    ns.stop.set()
    time.sleep(0.3)
    ns.hurry.set()
    assert finish(ns, timeout=15.0) == 1
    assert time.monotonic() - started < 15.0


def test_find_script(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    (tmp_path / "wahoo-ftms").write_text("")
    assert find_script("wahoo-ftms", tmp_path) == tmp_path / "wahoo-ftms"
    assert find_script("wahoo-lsl", tmp_path) is None


def test_find_script_prefers_exe_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", True)
    (tmp_path / "wahoo-ftms").write_text("")
    (tmp_path / "wahoo-ftms.exe").write_text("")
    assert find_script("wahoo-ftms", tmp_path) == tmp_path / "wahoo-ftms.exe"


def test_find_script_defaults_to_this_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name: str(tmp_path) if name == "scripts" else None)
    (tmp_path / "wahoo-lsl").write_text("")
    assert find_script("wahoo-lsl") == tmp_path / "wahoo-lsl"


def test_build_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    for name in ("wahoo-ftms", "wahoo-lsl"):
        (tmp_path / name).write_text("")
    args = launcher.build_parser().parse_args(
        ["--ftms-config", "f.json", "--cps-config", "c.json", "--log-dir", "L", "--debug",
         "--max-watts", "600", "--circumference-m", "2.0"])
    cmds = build_commands(args, "127.0.0.1", 47999, tmp_path)
    assert cmds["ftms"] == [str(tmp_path / "wahoo-ftms"), "serve", "-c", "f.json", "--debug"]
    assert cmds["cps"] == [str(tmp_path / "wahoo-lsl"), "run", "-c", "c.json", "--debug"]
    assert cmds["dash"] == [sys.executable, "-m", "wahoo_lsl.dashboard.app", "--host", "127.0.0.1",
                            "--port", "47999", "--log-dir", "L", "--max-watts", "600", "--circumference-m", "2.0"]


def test_build_commands_reports_missing_scripts(tmp_path):
    args = launcher.build_parser().parse_args([])
    with pytest.raises(LauncherError, match="wahoo-ftms, wahoo-lsl"):
        build_commands(args, "127.0.0.1", 47610, tmp_path)


def test_main_config_error_exits_2(tmp_path, capsys):
    assert launcher.main(["--ftms-config", str(tmp_path / "missing.json")]) == 2
    assert "cannot read config" in capsys.readouterr().err


def test_main_missing_scripts_exits_2(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "ftms.json"
    cfg.write_text('{"device": {"name": "KICKR"}}')
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name: str(empty))
    assert launcher.main(["--ftms-config", str(cfg)]) == 2
    assert "pip install -e ." in capsys.readouterr().err


# --- confirm_zero ----------------------------------------------------------


def zero_replies(read, set_reply=None, slew_reply=None):
    def reply(r):
        if r["cmd"] == "slew":
            return slew_reply(r) if slew_reply else {"ok": True, "slew_w_per_s": float(r["w_per_s"])}
        if r["cmd"] == "set":
            return set_reply(r) if set_reply else {"ok": True, "target_w": 0, "clamped": False}
        return read
    return reply


def test_confirm_zero_true_when_target_confirmed():
    with StubService(zero_replies(snapshot(requested=0, confirmed=0))) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=2.0, poll_s=0.02) is True
        assert {"cmd": "set", "watts": 0} in svc.requests


def test_confirm_zero_false_after_timeout_when_not_confirmed():
    with StubService(zero_replies(snapshot(requested=0, confirmed=120))) as svc:
        started = time.monotonic()
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=0.3, poll_s=0.02) is False
        assert 0.25 <= time.monotonic() - started < 2.0


def test_confirm_zero_false_when_link_not_ready():
    with StubService(zero_replies(snapshot(requested=0, confirmed=120, link="lost"))) as svc:
        started = time.monotonic()
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=2.0, poll_s=0.02) is False
        assert time.monotonic() - started < 1.0


def test_confirm_zero_unreachable_is_false_and_quick():
    started = time.monotonic()
    assert confirm_zero("127.0.0.1", free_port(), timeout_s=3.0, poll_s=0.02) is False
    assert time.monotonic() - started < 1.5


def test_confirm_zero_retries_after_handshake_connection_error():
    state = {"n": 0}

    def connect(r):
        if state["n"] < 2:
            state["n"] += 1
            return None  # service drops the connection mid-handshake
        return connect_reply()

    with StubService(zero_replies(snapshot(requested=0, confirmed=0)), connect) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=3.0, poll_s=0.02) is True
    assert state["n"] == 2


def test_confirm_zero_retries_after_handshake_connection_error():
    state = {"n": 0}

    def connect(r):
        if state["n"] < 2:
            state["n"] += 1
            return None  # service drops the connection mid-handshake
        return connect_reply()

    with StubService(zero_replies(snapshot(requested=0, confirmed=0)), connect) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=3.0, poll_s=0.02) is True
    assert state["n"] == 2


def test_confirm_zero_retries_while_busy():
    state = {"n": 0}
    busy = {"ok": False, "error": "busy", "detail": "another client is connected"}

    def connect(r):
        if state["n"] < 2:
            state["n"] += 1
            return busy
        return connect_reply()

    with StubService(zero_replies(snapshot(requested=0, confirmed=0)), connect) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=3.0, poll_s=0.02) is True


def test_confirm_zero_disables_slew_before_zero():
    with StubService(zero_replies(snapshot(requested=0, confirmed=0))) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=2.0, poll_s=0.02) is True
        writes = [r for r in svc.requests if r["cmd"] != "read"]
        assert writes[:2] == [{"cmd": "slew", "w_per_s": 0}, {"cmd": "set", "watts": 0}]


def test_confirm_zero_works_with_service_without_slew():
    old = {"ok": False, "error": "bad_request", "detail": "unknown cmd 'slew'; expected 'set' or 'read'"}
    with StubService(zero_replies(snapshot(requested=0, confirmed=0), slew_reply=lambda r: old)) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=2.0, poll_s=0.02) is True


def test_build_commands_passes_slew(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WIN", False)
    for name in ("wahoo-ftms", "wahoo-lsl"):
        (tmp_path / name).write_text("")
    args = launcher.build_parser().parse_args(["--slew", "15"])
    cmds = build_commands(args, "127.0.0.1", 47610, tmp_path)
    assert cmds["dash"][-2:] == ["--slew", "15"]


def test_main_defaults_dashboard_slew_to_ftms_config(tmp_path, monkeypatch):
    cfg = tmp_path / "ftms.json"
    cfg.write_text('{"device": {"name": "KICKR"}, "ftms": {"slew_w_per_s": 7}}')
    for name in ("wahoo-ftms", "wahoo-lsl"):
        (tmp_path / name).write_text("")
    monkeypatch.setattr(launcher, "WIN", False)
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name: str(tmp_path))
    seen = {}

    class FakeSupervisor:
        def __init__(self, commands, *rest):
            seen["dash"] = commands["dash"]

        def run(self):
            return 0

    monkeypatch.setattr(launcher, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(launcher.signal, "signal", lambda *a: None)
    assert launcher.main(["--ftms-config", str(cfg)]) == 0
    assert seen["dash"][-2:] == ["--slew", "7"]


def test_confirm_zero_false_if_service_closes_connection():
    with StubService(lambda r: None) as svc:
        assert confirm_zero("127.0.0.1", svc.port, timeout_s=0.3, poll_s=0.02) is False


def test_supervisor_confirms_zero_after_dash_before_ftms_signalled(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []

    def confirm(host, p):
        calls.append((host, p, order(o)))
        return True

    ns = start(cmds, port, confirm=confirm)
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 0
    assert calls == [("127.0.0.1", port, ["dash"])]  # dash already stopped, ftms not yet
    assert order(o) == ["dash", "ftms", "cps"]


def test_supervisor_does_not_confirm_when_port_never_opened(tmp_path):
    port = free_port()
    o = tmp_path / "order"
    cmds = {"ftms": fake("ftms", o), "cps": fake("cps", o), "dash": fake("dash", o)}
    calls = []
    ns = start(cmds, port, Timings(port_wait_s=0.5, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05),
               confirm=lambda h, p: calls.append(1))
    assert finish(ns) == 2
    assert calls == []


def test_supervisor_does_not_confirm_when_hurrying(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    calls = []
    ns = start(cmds, port, confirm=lambda h, p: calls.append(1))
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.hurry.set()
    ns.stop.set()
    finish(ns)
    assert calls == []


def test_supervisor_survives_a_confirm_that_raises(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)

    def boom(h, p):
        raise RuntimeError("boom")

    ns = start(cmds, port, confirm=boom)
    wait_for(lambda: "[dash] dash started" in ns.out.getvalue())
    ns.stop.set()
    assert finish(ns) == 0
    assert order(o) == ["dash", "ftms", "cps"]


# --- output failures --------------------------------------------------------


class BrokenOut:
    def __init__(self):
        self.attempts = 0

    def write(self, text):
        self.attempts += 1
        raise OSError("broken pipe")

    def flush(self):
        raise OSError("broken pipe")


def test_broken_output_does_not_stop_shutdown(tmp_path):
    port = free_port()
    o, cmds = happy(tmp_path, port)
    out = BrokenOut()
    ns = start(cmds, port, out=out)
    wait_for(lambda: out.attempts >= 3)  # all three children printed their first line
    ns.stop.set()
    assert finish(ns) == 0
    assert order(o) == ["dash", "ftms", "cps"]


# --- small pure helpers -----------------------------------------------------


def test_timings_for_scales_port_wait_with_scan_timeout():
    cfg = parse_ftms_config({"device": {"name": "KICKR", "scan_timeout_s": 25}})
    assert timings_for(cfg).port_wait_s == 45.0
    assert timings_for(parse_ftms_config({"device": {"name": "KICKR"}})).port_wait_s == 30.0


def test_clean_code_maps_windows_ctrl_break_exit_to_zero():
    assert launcher._clean_code(0xC000013A, True) == 0
    assert launcher._clean_code(0xC000013A, False) == 0xC000013A
    assert launcher._clean_code(3, True) == 3
    assert launcher._clean_code(0, True) == 0
