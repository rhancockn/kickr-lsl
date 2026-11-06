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


def test_slew_defaults():
    cfg = parse_ftms_config({"device": {"name": "KICKR"}})
    assert cfg.ftms.slew_w_per_s == 10.0
    assert cfg.ftms.slew_update_interval_s == 10.0


def test_slew_values():
    cfg = parse_ftms_config({"device": {"name": "KICKR"}, "ftms": {"slew_w_per_s": 0, "slew_update_interval_s": 2.5}})
    assert cfg.ftms.slew_w_per_s == 0.0
    assert cfg.ftms.slew_update_interval_s == 2.5
    assert parse_ftms_config({"device": {"name": "KICKR"}, "ftms": {"slew_w_per_s": 1000}}).ftms.slew_w_per_s == 1000.0


@pytest.mark.parametrize(
    "ftms, message",
    [
        ({"slew_w_per_s": -1}, "ftms.slew_w_per_s"),
        ({"slew_w_per_s": 1001}, "ftms.slew_w_per_s"),
        ({"slew_w_per_s": 10**400}, "ftms.slew_w_per_s"),
        ({"slew_w_per_s": True}, "ftms.slew_w_per_s"),
        ({"slew_w_per_s": "10"}, "ftms.slew_w_per_s"),
        ({"slew_w_per_s": float("nan")}, "ftms.slew_w_per_s"),
        ({"slew_update_interval_s": 0}, "ftms.slew_update_interval_s"),
        ({"slew_update_interval_s": float("inf")}, "ftms.slew_update_interval_s"),
    ],
)
def test_invalid_slew_settings(ftms, message):
    with pytest.raises(ConfigError, match=message):
        parse_ftms_config({"device": {"name": "KICKR"}, "ftms": ftms})


def test_example_config_has_slew_keys():
    cfg = load_ftms_config(Path(__file__).resolve().parents[1] / "ftms.example.json")
    assert (cfg.ftms.slew_w_per_s, cfg.ftms.slew_update_interval_s) == (10.0, 10.0)


def test_allowed_hosts_default_local_only():
    s = cfg().ftms
    assert s.allowed_hosts == ()
    assert not s.remote
    assert s.bind_host == "127.0.0.1"


def test_allowed_hosts_parsed():
    s = cfg(allowed_hosts=["Lab-PC-3.lab.local", "10.0.0.23"]).ftms
    assert s.allowed_hosts == ("lab-pc-3.lab.local", "10.0.0.23")
    assert s.remote
    assert s.bind_host == "0.0.0.0"


@pytest.mark.parametrize("value", ["lab-pc", [""], ["a b"], [1], None])
def test_allowed_hosts_rejects_bad(value):
    with pytest.raises(ConfigError, match="ftms.allowed_hosts"):
        cfg(allowed_hosts=value)


def test_listen_host_custom():
    assert cfg(allowed_hosts=["x"], listen_host="192.168.1.5").ftms.bind_host == "192.168.1.5"


@pytest.mark.parametrize("value", ["lab-pc", "::", "fe80::1", 5])
def test_listen_host_rejects_bad(value):
    with pytest.raises(ConfigError, match="ftms.listen_host"):
        cfg(allowed_hosts=["x"], listen_host=value)


def test_listen_host_needs_allowed_hosts():
    with pytest.raises(ConfigError, match="listen_host needs allowed_hosts"):
        cfg(listen_host="0.0.0.0")


@pytest.mark.parametrize("host", ["localhost", "::1"])
def test_allowed_hosts_requires_ipv4_loopback_host(host):
    with pytest.raises(ConfigError, match="with allowed_hosts, ftms.host must be 127.0.0.1"):
        cfg(allowed_hosts=["x"], host=host)


def test_example_config_parses():
    assert load_ftms_config(ROOT / "ftms.example.json").ftms.allowed_hosts == ()
