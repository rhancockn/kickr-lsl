import asyncio
import time

from wahoo_lsl.ftms.allow import is_allowed, normalize_ip

HOSTS = {"lab-pc-3.lab.local": ["10.0.0.23"], "other": ["10.0.0.99"]}


async def fake_resolve(name):
    if name == "bad":
        raise OSError("no such host")
    if name == "slow":
        await asyncio.sleep(10)
    return HOSTS.get(name.lower(), [])


def check(peer, allowed, timeout_s=2.0):
    return asyncio.run(is_allowed(peer, allowed, fake_resolve, timeout_s))


def test_loopback_always_allowed():
    for peer in ("127.0.0.1", "127.5.5.5", "::1"):
        assert check(peer, [])
        assert check(peer, ["other"])


def test_ip_literal_match_and_mismatch():
    assert check("10.0.0.23", ["10.0.0.23"])
    assert not check("10.0.0.23", ["10.0.0.24"])


def test_name_resolves_to_peer():
    assert check("10.0.0.23", ["lab-pc-3.lab.local"])


def test_name_resolves_elsewhere():
    assert not check("10.0.0.23", ["other"])


def test_names_case_insensitive():
    assert check("10.0.0.23", ["LAB-PC-3.LAB.LOCAL"])


def test_failing_name_does_not_block_other():
    assert check("10.0.0.23", ["bad", "lab-pc-3.lab.local"])
    assert not check("10.0.0.23", ["bad"])


def test_slow_name_bounded_other_matches():
    assert check("10.0.0.23", ["slow", "10.0.0.23"], timeout_s=0.1)
    t0 = time.monotonic()
    assert check("10.0.0.23", ["slow", "lab-pc-3.lab.local"], timeout_s=0.1)
    assert not check("10.0.0.23", ["slow"], timeout_s=0.1)
    assert time.monotonic() - t0 < 0.5


def test_ipv4_mapped_peer():
    assert check("::ffff:10.0.0.23", ["10.0.0.23"])
    assert check("::ffff:10.0.0.23", ["lab-pc-3.lab.local"])


def test_normalize_ip():
    assert normalize_ip("::ffff:10.0.0.23") == "10.0.0.23"
    assert normalize_ip("fe80::1%eth0") == "fe80::1"
    assert normalize_ip("lab-pc") is None
