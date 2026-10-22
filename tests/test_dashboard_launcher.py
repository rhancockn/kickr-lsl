import gc
import io
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wahoo_lsl.dashboard import launcher
from wahoo_lsl.dashboard.launcher import LauncherError, Supervisor, Timings, build_commands, find_script

FAKE = str(Path(__file__).with_name("dashboard_fake_child.py"))
FAST = Timings(port_wait_s=5.0, cps_early_exit_s=0.3, grace_s=5.0, kill_after_s=2.0, poll_s=0.05)


@pytest.fixture(autouse=True)
def collect_garbage_in_main_thread():
    # Earlier Tk tests can leave cyclic Tcl-backed objects behind; if the cyclic GC first runs in
    # one of this module's worker threads, Tcl aborts the process ("deleted by the wrong thread").
    gc.collect()
    yield


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake(label, order, *mode):
    return [sys.executable, FAKE, label, str(order), *map(str, mode)]


def start(commands, port, timings=FAST):
    ns = SimpleNamespace(out=io.StringIO(), stop=threading.Event(), hurry=threading.Event(), code=None)
    sup = Supervisor(commands, "127.0.0.1", port, ns.out, ns.stop, ns.hurry, timings)

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
    ns = start(cmds, port)
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
