import sys
from pathlib import Path

from wahoo_lsl.apps import paths, worker


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("WAHOO_LSL_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_data_dir_programdata_on_windows(monkeypatch):
    monkeypatch.delenv("WAHOO_LSL_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramData", "PD")
    assert paths.data_dir() == Path("PD") / "wahoo-lsl"


def test_data_dir_is_cwd_elsewhere(monkeypatch, tmp_path):
    monkeypatch.delenv("WAHOO_LSL_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.chdir(tmp_path)
    assert paths.data_dir() == Path.cwd()


def test_worker_argv_unpackaged(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.worker_argv("ftms", "serve") == [sys.executable, "-m", "wahoo_lsl.apps.worker", "ftms", "serve"]


def test_worker_argv_packaged_uses_sibling_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WahooServer.exe"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths.worker_argv("lsl", "run") == [str(tmp_path / "wahoo-worker.exe"), "lsl", "run"]


def test_service_commands(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    head = [sys.executable, "-m", "wahoo_lsl.apps.worker"]
    assert paths.service_commands() == {
        "ftms": [*head, "ftms", "serve", "-c", "ftms.json"],
        "cps": [*head, "lsl", "run", "-c", "config.json"],
    }
    debug = paths.service_commands(debug=True)
    assert debug["ftms"][-1] == "--debug" and debug["cps"][-1] == "--debug"


def test_dashboard_argv(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.dashboard_argv() == [sys.executable, "-m", "wahoo_lsl.apps.dashboard_app"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WahooServer.exe"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths.dashboard_argv() == [str(tmp_path / "WahooDashboard.exe")]


def test_connect_host_maps_wildcards_to_loopback():
    assert paths.connect_host("0.0.0.0") == "127.0.0.1"
    assert paths.connect_host("::") == "::1"
    assert paths.connect_host("192.168.1.5") == "192.168.1.5"


def test_worker_dispatches_ftms(monkeypatch):
    from wahoo_lsl.ftms import cli as ftms_cli
    calls = []
    monkeypatch.setattr(ftms_cli, "main", lambda argv: calls.append(argv) or 7)
    assert worker.main(["ftms", "serve", "-c", "ftms.json"]) == 7
    assert calls == [["serve", "-c", "ftms.json"]]


def test_worker_dispatches_lsl(monkeypatch):
    from wahoo_lsl import cli as lsl_cli
    calls = []
    monkeypatch.setattr(lsl_cli, "main", lambda argv: calls.append(argv) or 0)
    assert worker.main(["lsl", "scan"]) == 0
    assert calls == [["scan"]]


def test_worker_usage(capsys):
    assert worker.main([]) == 2
    assert worker.main(["nope"]) == 2
    assert "usage: wahoo-worker {ftms|lsl}" in capsys.readouterr().err
