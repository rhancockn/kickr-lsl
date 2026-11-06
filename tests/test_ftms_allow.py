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


def test_failures_alongside_match_are_logged_not_leaked(caplog):
    caplog.set_level("DEBUG", logger="wahoo_lsl.ftms.allow")
    unretrieved = []

    async def run():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: unretrieved.append(ctx)
        )
        names = ["lab-pc-3.lab.local", "bad", "bad", "bad", "bad"]
        result = await is_allowed("10.0.0.23", names, fake_resolve)
        await asyncio.sleep(0)
        import gc

        gc.collect()
        return result

    assert asyncio.run(run())
    assert not unretrieved
    assert sum("Host lookup failed" in r.message for r in caplog.records) == 4


def test_cached_resolver_single_flight():
    from wahoo_lsl.ftms.allow import CachedResolver

    calls = []

    async def slow_resolve(name):
        calls.append(name)
        await asyncio.sleep(0.05)
        return ["10.0.0.23"]

    async def run():
        cached = CachedResolver(slow_resolve)
        return await asyncio.gather(
            *(is_allowed("10.0.0.23", ["Lab"], cached, 1.0) for _ in range(20))
        )

    assert all(asyncio.run(run()))
    assert calls == ["lab"]


def test_cached_resolver_ttl_reuse_and_refresh():
    from wahoo_lsl.ftms.allow import CachedResolver

    now = [100.0]
    calls = []

    async def resolve(name):
        calls.append(name)
        return []

    async def run():
        cached = CachedResolver(resolve, ttl_s=2.0, clock=lambda: now[0])
        await cached("x")
        now[0] += 1.9
        await cached("x")
        assert calls == ["x"]  # empty result reused within the TTL
        now[0] += 0.2
        await cached("x")
        assert calls == ["x", "x"]  # refreshed after

    asyncio.run(run())


def test_cached_resolver_failure_is_empty_and_cached():
    from wahoo_lsl.ftms.allow import CachedResolver

    calls = []

    async def resolve(name):
        calls.append(name)
        raise OSError("boom")

    async def run():
        cached = CachedResolver(resolve)
        assert await cached("x") == []
        assert await cached("x") == []

    asyncio.run(run())
    assert calls == ["x"]


def test_timeout_is_below_client_default():
    from wahoo_lsl.ftms.allow import CHECK_TIMEOUT_S

    assert CHECK_TIMEOUT_S == 1.5
