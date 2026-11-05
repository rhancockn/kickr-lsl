import gc
import json
import queue
import shutil
import socket
from pathlib import Path

import pytest

from wahoo_lsl.apps import server_app
from wahoo_lsl.apps.paths import dashboard_argv
from wahoo_lsl.apps.server_app import (MAX_LOG_LINES, STALE_S, ServerWindow, acquire_lock, already_running,
                                       load_configs, service_address)
from wahoo_lsl.config import ConfigError
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[1]
P = "2026-10-28 10:00:00,000 INFO    wahoo_lsl.x: "


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
    del root
    gc.collect()


def write_configs(home, host="127.0.0.1", port=47610):
    ftms = json.loads((ROOT / "ftms.example.json").read_text())
    ftms["ftms"].update(host=host, port=port)
    (home / "ftms.json").write_text(json.dumps(ftms))
    shutil.copy(ROOT / "config.example.json", home / "config.json")


class FakeSupervisor:
    def __init__(self, cfg, ftms_enabled):
        self.cfg, self.ftms_enabled, self.started, self.stops, self.running = cfg, ftms_enabled, 0, 0, False
        self.ftms_requests = []

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stops += 1

    def set_ftms(self, enabled):
        self.ftms_requests.append(enabled)


class FakeFollower:
    def __init__(self, source_id):
        self.source_id, self.started, self.stopped = source_id, 0, 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def win(tk_root, tmp_path):
    write_configs(tmp_path)
    ns = type("NS", (), {})()
    ns.sups, ns.followers, ns.opened, ns.launched, ns.answer = [], [], [], [], True
    ns.clock = Clock()

    def make_supervisor(cfg, ftms_enabled):
        ns.sups.append(FakeSupervisor(cfg, ftms_enabled))
        return ns.sups[-1]

    def make_follower(sid):
        ns.followers.append(FakeFollower(sid))
        return ns.followers[-1]

    ns.w = ServerWindow(tk_root, queue.Queue(), tmp_path, make_supervisor, make_follower,
                        confirm=lambda *a: ns.answer, open_path=ns.opened.append,
                        launch=lambda argv, cwd: ns.launched.append((argv, cwd)), monotonic=ns.clock)
    ns.home = tmp_path
    return ns


def test_already_running():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert already_running("127.0.0.1", s.getsockname()[1])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not already_running("127.0.0.1", port)


def test_service_address(tmp_path):
    assert service_address(tmp_path) == (DEFAULT_HOST, DEFAULT_PORT)
    write_configs(tmp_path, host="localhost", port=5000)
    assert service_address(tmp_path) == ("localhost", 5000)


def test_load_configs_names_the_bad_file(tmp_path):
    write_configs(tmp_path)
    assert load_configs(tmp_path).ftms.port == 47610
    (tmp_path / "config.json").write_text("{bad")
    with pytest.raises(ConfigError, match="config.json"):
        load_configs(tmp_path)
    write_configs(tmp_path)
    (tmp_path / "ftms.json").unlink()
    with pytest.raises(ConfigError, match="ftms.json"):
        load_configs(tmp_path)


def test_acquire_lock_is_exclusive(tmp_path):
    first = acquire_lock(tmp_path / "server.lock")
    assert first is not None
    assert acquire_lock(tmp_path / "server.lock") is None
    first.close()
    again = acquire_lock(tmp_path / "server.lock")
    assert again is not None
    again.close()


def test_attach_hidden_console_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(server_app.sys, "platform", "linux")
    server_app.attach_hidden_console()


def test_start_services_starts_a_supervisor(win):
    assert win.w.ftms_var.get() is True  # FTMS control is on at every launch
    win.w.start_services()
    assert len(win.sups) == 1 and win.sups[0].started == 1
    assert win.sups[0].cfg.ftms.port == 47610 and win.sups[0].ftms_enabled is True


def test_start_follows_the_checkbox(win):
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()  # nothing running: only changes what the next start uses
    win.w.start_services()
    assert win.sups[0].ftms_enabled is False
    assert win.w.ftms_text.get() == "off"


def test_untick_asks_when_a_client_is_connected(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.answer = False
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    assert win.w.ftms_var.get() is True and win.sups[0].ftms_requests == []
    win.answer = True
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    assert win.sups[0].ftms_requests == [False]
    assert (win.w.ftms_text.get(), win.w.client_text.get()) == ("off", "none")


def test_tick_turns_ftms_back_on(win):
    win.w.start_services()
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    win.w.ftms_var.set(True)
    win.w.toggle_ftms()
    assert win.sups[0].ftms_requests == [False, True]
    assert win.w.ftms_text.get() == "starting"
    win.w.handle("line", f"[ftms] {P}FTMS control ready at 0 W (link_epoch 1)")
    assert win.w.ftms_text.get() == "ready"


def test_ftms_stopping_on_its_own_unticks_the_box(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.w.handle("ftms", "stopped (FTMS service exited with code 1)")
    assert win.w.ftms_var.get() is False
    assert win.w.ftms_text.get() == "stopped (FTMS service exited with code 1)"
    assert win.w.client_text.get() == "none"
    win.w.handle("ftms", "off")
    assert win.w.ftms_text.get() == "off"


def test_config_error_shown_and_restart_retries(win):
    (win.home / "ftms.json").write_text("{bad")
    win.w.start_services()
    assert win.sups == []
    assert win.w.state_text.get().startswith("config error: ") and "ftms.json" in win.w.state_text.get()
    write_configs(win.home)
    win.w.restart()
    assert len(win.sups) == 1


def test_status_lines_update_the_rows(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}FTMS control ready at 0 W (link_epoch 1)")
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.w.handle("line", f"[cps] {P}Streaming from KICKR (mac AA)")
    assert (win.w.ftms_text.get(), win.w.client_text.get(), win.w.cps_text.get()) == ("ready", "peer1", "streaming")
    assert "Client connected: peer1" in win.w.log.get("1.0", "end")


def test_follower_bound_to_this_servers_stream(win):
    win.w.handle("line", f"[cps] {P}LSL outlet KICKR-CPS-aaa: 3 channels (power_w)")
    win.w.handle("line", f"[cps] {P}LSL outlet KICKR-CPS-aaa: 3 channels (power_w)")
    assert [(f.source_id, f.started) for f in win.followers] == [("KICKR-CPS-aaa", 1)]
    assert win.w.stream_text.get() == "KICKR-CPS-aaa"
    win.w.handle("line", f"[cps] {P}LSL outlet KICKR-CPS-bbb: 3 channels (power_w)")
    assert win.followers[0].stopped == 1
    assert [f.source_id for f in win.followers] == ["KICKR-CPS-aaa", "KICKR-CPS-bbb"]


def test_power_and_staleness(win):
    win.w.handle("cps_sample", {"t_lsl": 1.0, "fields": {"power_w": 150}})
    assert win.w.power_text.get() == "150 W"
    win.clock.t += STALE_S + 1
    win.w._pump()
    assert win.w.power_text.get() == "—"
    win.w.handle("cps_sample", {"t_lsl": 2.0, "fields": {"power_w": 120}})
    win.w.handle("cps_event", {"kind": "disconnected"})
    assert win.w.power_text.get() == "—"


def test_supervisor_stopped_event(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}FTMS control ready at 0 W (link_epoch 1)")
    win.w.handle("stopped", "CPS logger exited with code 3")
    assert win.w.state_text.get() == "stopped (CPS logger exited with code 3)"
    assert win.w.ftms_text.get() == "stopped"


def test_restart_waits_for_the_running_supervisor(win):
    win.w.start_services()
    win.w.restart()
    assert win.sups[0].stops == 1 and len(win.sups) == 1
    win.w._pump()
    assert len(win.sups) == 1  # still running
    win.sups[0].running = False
    win.w._pump()
    assert len(win.sups) == 2 and win.sups[1].started == 1


def test_close_asks_when_a_client_is_connected(win):
    win.w.start_services()
    win.w.handle("line", f"[ftms] {P}Client connected: peer1")
    win.answer = False
    win.w.close()
    assert win.sups[0].stops == 0 and not win.w.destroyed
    win.answer = True
    win.w.close()
    assert win.sups[0].stops == 1
    assert win.w.state_text.get().startswith("Stopping")
    win.w.close()  # second close hurries
    assert win.sups[0].stops == 2
    win.w._pump()
    assert not win.w.destroyed
    win.sups[0].running = False
    win.w._pump()
    assert win.w.destroyed


def test_close_with_nothing_running_destroys_on_next_pump(win):
    win.w.close()
    win.w._pump()
    assert win.w.destroyed


def test_request_close_from_a_signal(win):
    win.w.request_close()
    win.w._pump()
    win.w._pump()
    assert win.w.destroyed


def test_buttons_open_logs_and_dashboard(win):
    win.w.open_logs()
    win.w.open_dashboard()
    assert win.opened == [win.home / "logs"]
    assert win.launched == [(dashboard_argv(), win.home)]


def test_log_pane_is_capped(win):
    for i in range(MAX_LOG_LINES + 10):
        win.w.handle("line", f"[cps] line {i}")
    text = win.w.log.get("1.0", "end").splitlines()
    assert len([t for t in text if t]) <= MAX_LOG_LINES
    assert text[-1] == f"[cps] line {MAX_LOG_LINES + 9}" or text[-2] == f"[cps] line {MAX_LOG_LINES + 9}"


def test_second_restart_click_does_not_hurry(win):
    win.w.start_services()
    win.w.restart()
    win.w.restart()
    assert win.sups[0].stops == 1


def test_close_during_a_pending_restart_is_not_a_hurry(win):
    win.w.start_services()
    win.w.restart()
    win.w.close()
    assert win.sups[0].stops == 1
    win.w.close()
    assert win.sups[0].stops == 2
    win.sups[0].running = False
    win.w._pump()
    assert win.w.destroyed and len(win.sups) == 1


def test_ticking_with_nothing_running_only_records_the_choice(win):
    win.w.ftms_var.set(False)
    win.w.toggle_ftms()
    assert win.w.ftms_text.get() != "off"
    assert win.w.status.ftms != "off"


def test_restart_waits_a_pump_after_the_old_supervisor_stops(win):
    win.w.start_services()
    win.w.restart()
    handle = win.w.handle

    def handle_then_stop(kind, payload):
        handle(kind, payload)
        win.sups[0].running = False  # the old supervisor finishes while events are drained

    win.w.handle = handle_then_stop
    win.w.events.put(("stopped", "old"))
    win.w._pump()
    assert len(win.sups) == 1
    win.w._pump()
    assert len(win.sups) == 2
