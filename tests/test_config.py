import sys
from pathlib import Path

import pytest

from wahoo_lsl.config import (
    ConfigError,
    DeviceConfig,
    address_kind,
    load_config,
    parse_config,
    select_identifier,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"
MAC = "AA:BB:CC:DD:EE:FF"
UUID = "12345678-1234-5678-1234-567812345678"


def base() -> dict:
    return {"device": {"name": "KICKR"}, "measurements": ["power"]}


def _set(path: str, value):
    def mutate(d: dict) -> None:
        *parents, last = path.split(".")
        target = d
        for p in parents:
            target = target.setdefault(p, {})
        target[last] = value

    return mutate


def _delete(key: str):
    def mutate(d: dict) -> None:
        del d[key]

    return mutate


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    assert cfg.device == DeviceConfig(name="KICKR", mac=None, uuid=None, scan_timeout_s=10.0)
    assert cfg.measurements == ("power", "wheel_revolutions", "accumulated_torque")
    assert cfg.log.path == "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl"
    assert cfg.log.flush_every_n == 10
    assert cfg.reconnect.enabled is True
    assert cfg.reconnect.max_attempts == 0
    assert cfg.reconnect.backoff_s == (1.0, 2.0, 5.0, 10.0)
    assert cfg.queue_warn_depth == 100


def test_defaults_applied():
    cfg = parse_config(base())
    assert cfg.log.path == "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl"
    assert cfg.log.flush_every_n == 10
    assert cfg.reconnect.enabled is True
    assert cfg.reconnect.max_attempts == 0
    assert cfg.reconnect.backoff_s == (1.0, 2.0, 5.0, 10.0)
    assert cfg.device.scan_timeout_s == 10.0
    assert cfg.queue_warn_depth == 100


def test_name_is_stripped_and_mac_uuid_accepted():
    d = base()
    d["device"] = {"name": " KICKR ", "mac": MAC.lower(), "uuid": UUID}
    cfg = parse_config(d)
    assert cfg.device.name == "KICKR"
    assert cfg.device.mac == MAC.lower()
    assert cfg.device.uuid == UUID


def test_measurements_optional_when_not_required():
    cfg = parse_config({"device": {"name": "KICKR"}}, require_measurements=False)
    assert cfg.measurements == ()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (_set("extra", 1), "unknown top-level"),
        (_delete("device"), "device"),
        (_set("device", "KICKR"), "'device' must be an object"),
        (_set("device.name", ""), "device.name"),
        (_set("device.colour", "red"), "unknown key"),
        (_set("device.mac", "not-a-mac"), "device.mac"),
        (_set("device.uuid", "nope"), "device.uuid"),
        (_set("device.scan_timeout_s", 0), "scan_timeout_s"),
        (_set("device.scan_timeout_s", True), "scan_timeout_s"),
        (_delete("measurements"), "measurements"),
        (_set("measurements", []), "measurements"),
        (_set("measurements", ["power", "watts"]), "watts"),
        (_set("measurements", ["power", "power"]), "duplicate"),
        (_set("log.path", ""), "log.path"),
        (_set("log.path", "logs/{foo}.jsonl"), "log.path"),
        (_set("log.path", "logs/{start.nope}.jsonl"), "log.path"),
        (_set("log.path", "logs/{.jsonl"), "log.path"),
        (_set("log.path", "logs/{start[0]}.jsonl"), "log.path"),
        (_set("log.flush_every_n", 0), "flush_every_n"),
        (_set("reconnect.enabled", "yes"), "reconnect.enabled"),
        (_set("reconnect.max_attempts", -1), "max_attempts"),
        (_set("reconnect.backoff_s", []), "backoff_s"),
        (_set("reconnect.backoff_s", [1, -2]), "backoff_s"),
        (_set("queue_warn_depth", 0), "queue_warn_depth"),
    ],
)
def test_invalid_configs_rejected(mutate, message):
    d = base()
    mutate(d)
    with pytest.raises(ConfigError) as exc:
        parse_config(d)
    assert message in str(exc.value)


def test_non_object_config_rejected():
    with pytest.raises(ConfigError):
        parse_config(["device"])


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "missing.json")


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(p)


@pytest.mark.parametrize(
    "platform, mac, uuid, expected",
    [
        ("darwin", MAC, UUID, ("uuid", UUID)),
        ("darwin", MAC, None, ("name", "KICKR")),
        ("win32", MAC, UUID, ("mac", MAC)),
        ("linux", MAC, UUID, ("mac", MAC)),
        ("linux", None, UUID, ("name", "KICKR")),
        ("freebsd", MAC, None, ("mac", MAC)),
    ],
)
def test_select_identifier(platform, mac, uuid, expected):
    device = DeviceConfig(name="KICKR", mac=mac, uuid=uuid)
    assert select_identifier(device, platform) == expected


def test_address_kind_defaults_to_sys_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert address_kind() == "uuid"
    monkeypatch.setattr(sys, "platform", "win32")
    assert address_kind() == "mac"
