import gc
import json
import queue
import shutil
from pathlib import Path

import pytest

from wahoo_lsl.apps import dashboard_app
from wahoo_lsl.apps.dashboard_app import LslDashboard, build_parser, defaults
from wahoo_lsl.dashboard.metrics import DEFAULT_CIRCUMFERENCE_M
from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[1]


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
    def request(self, watts):
        pass

    def request_slew(self, w_per_s):
        pass

    def stop(self):
        pass


class FakeFollower:
    def stop(self):
        pass


def write_configs(home, host="127.0.0.1", port=5000, slew=5, circumference=2.0):
    ftms = json.loads((ROOT / "ftms.example.json").read_text())
    ftms["ftms"].update(host=host, port=port, slew_w_per_s=slew)
    (home / "ftms.json").write_text(json.dumps(ftms))
    cps = json.loads((ROOT / "config.example.json").read_text())
    cps["derive"] = {"wheel_circumference_m": circumference}
    (home / "config.json").write_text(json.dumps(cps))


def test_defaults_from_the_server_configs(tmp_path):
    write_configs(tmp_path)
    assert defaults(tmp_path) == {"host": "127.0.0.1", "port": 5000, "slew": 5, "circumference_m": 2.0}


def test_defaults_fall_back_when_configs_are_missing_or_invalid(tmp_path):
    expected = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "slew": 10.0, "circumference_m": DEFAULT_CIRCUMFERENCE_M}
    assert defaults(tmp_path) == expected
    (tmp_path / "ftms.json").write_text("{not json")
    (tmp_path / "config.json").write_text("[]")
    assert defaults(tmp_path) == expected


def test_flags_override_defaults(tmp_path):
    write_configs(tmp_path)
    args = build_parser(defaults(tmp_path)).parse_args(["--port", "6000", "--stream", "KICKR-CPS-abc"])
    assert (args.host, args.port, args.slew, args.circumference_m, args.stream) == (
        "127.0.0.1", 6000, 5, 2.0, "KICKR-CPS-abc")
    assert build_parser(defaults(tmp_path)).parse_args([]).stream is None


@pytest.mark.parametrize("argv", [["--max-watts", "0"], ["--circumference-m", "0"], ["--slew", "-1"]])
def test_main_rejects_bad_values(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "data_dir", lambda: tmp_path)
    with pytest.raises(SystemExit) as e:
        dashboard_app.main(argv)
    assert e.value.code == 2


def test_lsl_wording(tk_root):
    d = LslDashboard(tk_root, queue.Queue(), FakeWorker(), FakeFollower())
    titles = [w.cget("text") for w in tk_root.winfo_children() if w.winfo_class() == "Labelframe"]
    assert "CPS (LSL)" in titles and "CPS (from log)" not in titles
    d.handle("cps_file", "KICKR-CPS-abc")
    assert d.status_text.get() == "service: starting · stream: KICKR-CPS-abc"
    d.handle("cps_sample", {"t_lsl": 1.0, "fields": {"power_w": 150}})
    assert d.cps_values["power"].get() == "150"
    d.close()
