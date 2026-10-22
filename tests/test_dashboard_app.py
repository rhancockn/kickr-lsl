import gc
import queue
from types import SimpleNamespace

import pytest

from wahoo_lsl.dashboard import app as app_mod
from wahoo_lsl.dashboard.app import DASH, STALE_S, Dashboard
from wahoo_lsl.ftms.client import PowerReading, Status, TargetState


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


class FakeWorker:
    def __init__(self):
        self.requests = []
        self.stopped = 0

    def request(self, watts):
        self.requests.append(watts)

    def stop(self):
        self.stopped += 1


class FakeFollower:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def dash(tk_root):
    events, lsl, mono = queue.Queue(), Clock(1000.0), Clock(50.0)
    worker, follower = FakeWorker(), FakeFollower()
    d = Dashboard(tk_root, events, worker, follower, clock=lsl, monotonic=mono)
    ns = SimpleNamespace(app=d, events=events, lsl=lsl, mono=mono, root=tk_root, worker=worker, follower=follower)
    yield ns
    d.close()
    ns.__dict__.clear()
    del d


def pump(ns, *items):
    for item in items:
        ns.events.put(item)
    ns.app._pump()
    ns.root.update()


def sample(t, power, wheel, crank):
    return ("cps_sample", {"type": "sample", "t_lsl": t, "fields": {
        "power_w": power, "wheel_revs": wheel[0], "wheel_event_t": wheel[1],
        "crank_revs": crank[0], "crank_event_t": crank[1]}})


def test_slider_change_requests_power_once_per_value(dash):
    dash.app._on_slider("150")  # what Tk's Scale -command calls while dragging
    dash.app._on_slider("150.0")
    assert dash.worker.requests == [150]


def test_zero_button_sends_zero(dash):
    dash.app.slider.set(200)
    dash.app._on_slider("200")
    dash.app.zero_button.invoke()
    dash.root.update()
    assert dash.app.slider.get() == 0
    assert dash.worker.requests == [200, 0]


def test_service_connected_resets_slider_to_zero(dash):
    dash.app.slider.set(200)
    dash.app._on_slider("200")
    pump(dash, ("service_connected", None))
    assert dash.app.slider.get() == 0
    assert dash.worker.requests == [200, 0]
    assert "service: connected" in dash.app.status_text.get()


def test_status_updates_target_panel_and_history(dash):
    status = Status("ready", 2, TargetState(200, 180, True, 5.0), PowerReading(190, 5.0, 0.1), "spin-down failed", 6.0)
    pump(dash, ("status", status))
    assert dash.app.target_text.get() == "requested 200 W · confirmed 180 W (pending)"
    assert dash.app.link_text.get() == "link ready (epoch 2)"
    assert dash.app.error_text.get() == "spin-down failed"
    assert dash.app.histories["target"].points() == [(0.0, 180)]


def test_service_down_blanks_target_panel(dash):
    pump(dash, ("status", Status("ready", 1, TargetState(100, 100, False, 1.0), PowerReading(None, None, None), None, None)))
    pump(dash, ("service_down", "service unreachable (refused)"))
    assert dash.app.target_text.get() == f"requested {DASH} · confirmed {DASH}"
    assert "service unreachable" in dash.app.status_text.get()


def test_set_error_shown_and_cleared_by_set_ok(dash):
    pump(dash, ("set_error", "link_down: x"))
    assert dash.app.set_error_text.get() == "link_down: x"
    pump(dash, ("set_ok", 100))
    assert dash.app.set_error_text.get() == ""


def test_cps_samples_update_values_and_histories(dash):
    pump(dash, sample(1000.0, 150, (10, 0), (5, 0)), sample(1001.0, 160, (11, 2048), (6, 1024)))
    assert dash.app.cps_values["power"].get() == "160"
    assert dash.app.cps_values["speed"].get() == "7.6"
    assert dash.app.cps_values["cadence"].get() == "60"
    assert dash.app.histories["power"].points() == [(0.0, 150), (1.0, 160)]
    speed = dash.app.histories["speed"].points()
    assert speed[0] == (0.0, None) and speed[1][1] == pytest.approx(2.105 * 3.6)


def test_values_go_stale_without_samples(dash):
    pump(dash, sample(1000.0, 150, (10, 0), (5, 0)))
    dash.mono.t += STALE_S + 0.1
    pump(dash)
    assert all(v.get() == DASH for v in dash.app.cps_values.values())
    assert "stale" in dash.app.cps_state.get()


@pytest.mark.parametrize("reset", [("cps_file", "cps_2.jsonl"), ("cps_event", {"type": "event", "kind": "connected"})])
def test_new_log_file_or_cps_event_resets_deriver(dash, reset):
    pump(dash, sample(1000.0, 150, (1000, 0), (500, 0)), reset, sample(1001.0, 150, (0, 2048), (0, 1024)))
    assert dash.app.cps_values["speed"].get() == DASH  # re-primed: no bogus wrap-around speed
    assert dash.app.cps_values["cadence"].get() == DASH


def test_status_bar_shows_log_file_and_bad_lines(dash):
    pump(dash, ("cps_file", "cps_2.jsonl"), ("cps_bad", 3))
    text = dash.app.status_text.get()
    assert "cps_2.jsonl" in text and "bad lines: 3" in text


def test_redraw_draws_and_sweep_clears_after_window(dash):
    pump(dash, ("status", Status("ready", 1, TargetState(100, 100, False, 1.0), PowerReading(None, None, None), None, None)))
    dash.app._redraw()
    assert len(dash.app.graphs["target"].canvas.find_withtag("data")) == 1
    dash.lsl.t = 1300.0
    dash.app._redraw()
    assert dash.app.histories["target"].points() == []


def test_close_stops_worker_and_follower_once(dash):
    dash.app.close()
    dash.app.close()
    assert (dash.worker.stopped, dash.follower.stopped) == (1, 1)


def test_request_close_closes_on_next_pump(dash):
    dash.app.request_close()
    dash.app._pump()
    assert dash.worker.stopped == 1


def test_main_without_display_exits_2(monkeypatch, capsys):
    def no_display():
        raise app_mod.tk.TclError("no display name")

    monkeypatch.setattr(app_mod.tk, "Tk", no_display)
    assert app_mod.main([]) == 2
    assert "cannot open a window" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--max-watts", "0"], ["--circumference-m", "-1"]])
def test_main_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as info:
        app_mod.main(argv)
    assert info.value.code == 2


def test_bad_event_is_skipped_and_later_events_apply(dash, caplog):
    pump(dash, ("cps_event", None), ("cps_bad", 7))
    assert "bad lines: 7" in dash.app.status_text.get()
    assert any(r.levelname == "ERROR" and "cps_event" in r.getMessage() for r in caplog.records)


def test_pump_stays_armed_after_handler_raises(dash, monkeypatch):
    def boom(kind, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(dash.app, "handle", boom)
    dash.events.put(("status", None))
    dash.app._pump()
    dash.app.request_close()
    dash.app._pump()
    assert dash.worker.stopped == 1


def test_draw_failure_does_not_stop_redraws(dash, monkeypatch):
    def boom(points):
        raise RuntimeError("draw failed")

    monkeypatch.setattr(dash.app.graphs["target"], "draw", boom)
    before = dash.app._redraw_id
    dash.app._redraw()
    assert dash.app._redraw_id != before
    assert dash.app.graphs["power"].canvas.find_withtag("all") != ()  # later graphs still drew


def test_zero_button_sends_even_if_zero_was_last_sent(dash):
    dash.app._on_slider("0")
    dash.app.zero_button.invoke()
    dash.root.update()
    assert dash.worker.requests == [0, 0]
