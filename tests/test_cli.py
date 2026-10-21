import json
import re
import struct
from datetime import datetime
from pathlib import Path

import pytest
from fakes import KICKR, FakeBackend

from wahoo_lsl.ble import DeviceInfo, ScanResult
from wahoo_lsl.cli import build_parser, format_features, format_scan, main, run_session
from wahoo_lsl.config import parse_config
from wahoo_lsl.cps import parse_features

POWER = struct.pack("<Hh", 0, 200)
WHEEL = struct.pack("<Hh", 1 << 4, 150) + struct.pack("<IH", 10, 2048)
TRUNCATED_WHEEL = struct.pack("<Hh", 1 << 4, 5)
WALL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def write_config(tmp_path: Path, **overrides) -> Path:
    cfg = {
        "device": {"name": "KICKR", "scan_timeout_s": 1},
        "measurements": ["power", "accumulated_torque"],
        "log": {"path": str(tmp_path / "logs" / "cps_{start:%Y%m%d}.jsonl")},
        "reconnect": {"enabled": False},
    }
    cfg.update(overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_run_options():
    args = build_parser().parse_args(["run", "-c", "x.json", "--debug", "--diag-log", "d.log"])
    assert args.command == "run"
    assert args.config == Path("x.json")
    assert args.debug is True
    assert args.diag_log == Path("d.log")


def test_format_features():
    info = DeviceInfo("KICKR CORE 1234", "AA:BB:CC:DD:EE:01", "mac", parse_features(struct.pack("<I", 0b1100)), "Rear hub")
    text = format_features(info, ["power", "accumulated_torque"])
    lines = text.splitlines()
    assert "(mac: AA:BB:CC:DD:EE:01)" in lines[0]
    assert "Rear hub" in lines[1]
    assert "Feature bits (0x0000000C):" in text
    assert "  [x] wheel_revolution_data" in lines
    assert "  [ ] pedal_power_balance" in lines
    assert any(line.split() == ["power", "available"] for line in lines)
    assert any(line.split() == ["accumulated_torque", "UNAVAILABLE"] for line in lines)


def test_format_features_without_measurements():
    info = DeviceInfo(None, "X", "uuid", parse_features(b"\x00\x00\x00\x00"), None)
    text = format_features(info, [])
    assert "Configured measurements" not in text
    assert "(uuid: X)" in text


def test_format_scan():
    assert format_scan([]) == "No Cycling Power devices found."
    text = format_scan([ScanResult("KICKR CORE", "AA:BB:CC:DD:EE:01", -50, True)])
    assert "KICKR CORE" in text and "AA:BB:CC:DD:EE:01" in text and "yes" in text


def test_main_invalid_json_exit_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    assert main(["run", "-c", str(p)]) == 2


def test_main_missing_config_exit_2(tmp_path):
    assert main(["list-features", "-c", str(tmp_path / "missing.json")]) == 2


def test_list_features_with_fake_backend(tmp_path, capsys):
    p = write_config(tmp_path)
    assert main(["list-features", "-c", str(p)], backend=FakeBackend()) == 0
    out = capsys.readouterr().out
    assert KICKR.name in out
    assert "Rear hub" in out
    assert "UNAVAILABLE" in out


def test_list_features_device_not_found_exit_3(tmp_path):
    p = write_config(tmp_path)
    assert main(["list-features", "-c", str(p)], backend=FakeBackend([])) == 3


def test_scan_with_fake_backend(capsys):
    assert main(["scan", "--timeout", "0.1"], backend=FakeBackend()) == 0
    assert KICKR.name in capsys.readouterr().out


def test_run_session_end_to_end(tmp_path):
    cfg = parse_config(json.loads(write_config(tmp_path).read_text(encoding="utf-8")))
    backend = FakeBackend(sessions=[([POWER, WHEEL, TRUNCATED_WHEEL], True)])
    code = run_session(cfg, backend=backend, start=datetime(2026, 10, 20))
    assert code == 4  # link dropped and reconnect is disabled
    records = read_log(tmp_path / "logs" / "cps_20261020.jsonl")
    events = [r["kind"] for r in records if r["type"] == "event"]
    assert events == ["clock_sync", "connected", "features", "disconnected", "stopped"]
    samples = [r for r in records if r["type"] == "sample"]
    assert [s["seq"] for s in samples] == [0, 1, 2]
    assert samples[0]["fields"]["power_w"] == 200
    assert samples[1]["fields"]["wheel_revs"] == 10  # logged although not configured
    assert "wheel_revolutions" in samples[2]["parse_error"]
    assert samples[2]["raw"] == TRUNCATED_WHEEL.hex()
    assert samples[0]["t_lsl"] <= samples[1]["t_lsl"] <= samples[2]["t_lsl"]
    assert all(WALL_RE.match(r["t_wall"]) for r in records)
    stopped = records[-1]
    assert stopped["kind"] == "stopped"
    assert stopped["detail"] == {"reason": "reconnect_exhausted", "parse_errors": 1}


def test_run_session_setup_failure_exit_3(tmp_path):
    cfg = parse_config(json.loads(write_config(tmp_path).read_text(encoding="utf-8")))
    code = run_session(cfg, backend=FakeBackend([]), start=datetime(2026, 10, 20))
    assert code == 3
    records = read_log(tmp_path / "logs" / "cps_20261020.jsonl")
    assert [r["kind"] for r in records] == ["clock_sync", "stopped"]
    assert records[-1]["detail"]["reason"] == "setup_error"


def test_run_session_unwritable_log_path(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    cfg = parse_config(
        {
            "device": {"name": "KICKR"},
            "measurements": ["power"],
            "log": {"path": str(blocker / "cps.jsonl")},
        }
    )
    backend = FakeBackend()
    assert run_session(cfg, backend=backend) == 2
    assert backend.clients == []  # BLE never touched
