import json
from pathlib import Path

import pytest

from wahoo_lsl.config import ConfigError
from wahoo_lsl.ftms.config import FtmsSettings, load_ftms_config, parse_ftms_config

ROOT = Path(__file__).resolve().parents[1]


def cfg(**ftms):
    return parse_ftms_config({"device": {"name": "KICKR"}, "ftms": ftms})


def test_defaults():
    c = parse_ftms_config({"device": {"name": "KICKR"}})
    assert c.ftms == FtmsSettings("127.0.0.1", 47610, 2.0, 1.0, None)
    assert c.device.name == "KICKR"
    assert c.reconnect.backoff_s == (1.0, 2.0, 5.0, 10.0)


def test_example_file_loads():
    c = load_ftms_config(ROOT / "ftms.example.json")
    assert c.ftms.port == 47610
    assert c.ftms.max_rate_hz == 2.0


def test_values_parsed():
    c = cfg(host="localhost", port=50000, max_rate_hz=4, ack_timeout_s=0.5, max_watts=600)
    assert c.ftms == FtmsSettings("localhost", 50000, 4.0, 0.5, 600)
    assert cfg(host="::1").ftms.host == "::1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "", None, ["127.0.0.1"]])
def test_non_loopback_host_rejected(host):
    with pytest.raises(ConfigError, match="ftms.host"):
        cfg(host=host)


@pytest.mark.parametrize("port", [0, 65536, "47610", True, 1.5])
def test_bad_port(port):
    with pytest.raises(ConfigError, match="ftms.port"):
        cfg(port=port)


@pytest.mark.parametrize("key", ["max_rate_hz", "ack_timeout_s"])
@pytest.mark.parametrize("value", [0, -1, "2", True])
def test_bad_positive_numbers(key, value):
    with pytest.raises(ConfigError, match=key):
        cfg(**{key: value})


@pytest.mark.parametrize("value", [0, -5, 1.5, "300", True])
def test_bad_max_watts(value):
    with pytest.raises(ConfigError, match="ftms.max_watts"):
        cfg(max_watts=value)


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown"):
        parse_ftms_config({"device": {"name": "K"}, "measurements": ["power"]})
    with pytest.raises(ConfigError, match="unknown"):
        cfg(rate=2)


def test_device_section_shared_rules():
    with pytest.raises(ConfigError, match="device.mac"):
        parse_ftms_config({"device": {"name": "K", "mac": "nope"}})
    with pytest.raises(ConfigError, match="device"):
        parse_ftms_config({"ftms": {}})


def test_not_an_object():
    with pytest.raises(ConfigError):
        parse_ftms_config([])


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_ftms_config(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_ftms_config(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"device": {"name": "K"}}), encoding="utf-8")
    assert load_ftms_config(good).device.name == "K"
