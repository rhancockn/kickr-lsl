"""SharedBackend: one real BLE link shared by several handles."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from wahoo_lsl.apps.shared_ble import SharedBackend
from wahoo_lsl.ble import ScanResult
from wahoo_lsl.cps import CPS_SERVICE_UUID

KICKR = SimpleNamespace(name="KICKR", address="AA:BB:CC:DD:EE:01")
OTHER = SimpleNamespace(name="OTHER", address="AA:BB:CC:DD:EE:02")
CHAR_A = SimpleNamespace(uuid="0000AAAA-0000-1000-8000-00805f9b34fb")
CHAR_B = SimpleNamespace(uuid="0000BBBB-0000-1000-8000-00805f9b34fb")


class RealClient:
    def __init__(self, backend, device, cb):
        self.backend, self.device, self.cb = backend, device, cb
        self.is_connected = False
        self.services = [SimpleNamespace(uuid=CPS_SERVICE_UUID.upper())]
        self.calls = []

    async def connect(self):
        self.backend.connects += 1
        if self.backend.delay:
            await asyncio.sleep(self.backend.delay)
        if self.backend.fail:
            raise OSError("connect failed")
        self.is_connected = True

    async def disconnect(self):
        self.calls.append(("disconnect",))
        self.is_connected = False
        self.cb(self)  # bleak also reports intentional disconnects

    async def read_gatt_char(self, char):
        return b"r"

    async def write_gatt_char(self, char, data, response=None):
        self.calls.append(("write", char, bytes(data), response))

    async def start_notify(self, char, cb):
        self.calls.append(("start", char.uuid, cb))

    async def stop_notify(self, char):
        self.calls.append(("stop", char.uuid))

    def drop(self):
        self.is_connected = False
        self.cb(self)


class Inner:
    def __init__(self):
        self.connects = 0
        self.delay = 0
        self.fail = False
        self.clients = []
        self.scans = 0

    async def discover(self, timeout):
        self.scans += 1
        return [ScanResult("KICKR", KICKR.address, -50, True, KICKR)]

    async def find_by_address(self, address, timeout):
        self.scans += 1
        return KICKR

    def make_client(self, device, cb):
        c = RealClient(self, device, cb)
        self.clients.append(c)
        return c


def run(coro):
    return asyncio.run(coro)


def setup():
    inner = Inner()
    return inner, SharedBackend(inner)


def handles(sb, n=2, device=KICKR):
    got = []
    return [sb.make_client(device, lambda h, got=got: got.append(h)) for _ in range(n)], got


def test_two_handles_share_one_real_connect():
    async def go():
        inner, sb = setup()
        (a, b), _ = handles(sb)
        await a.connect()
        await b.connect()
        assert inner.connects == 1 and len(inner.clients) == 1
        assert a.is_connected and b.is_connected
        assert a.services is inner.clients[0].services
        assert await a.read_gatt_char(CHAR_A) == b"r"
        await b.write_gatt_char(CHAR_B, b"x", response=True)
        assert ("write", CHAR_B, b"x", True) in inner.clients[0].calls
    run(go())


def test_concurrent_connects_coalesce():
    async def go():
        inner, sb = setup()
        inner.delay = 0.01
        (a, b), _ = handles(sb)
        await asyncio.gather(a.connect(), b.connect())
        assert inner.connects == 1 and a.is_connected and b.is_connected
    run(go())


def test_failed_connect_raises_to_all_and_leaves_no_link():
    async def go():
        inner, sb = setup()
        inner.delay, inner.fail = 0.01, True
        (a, b), _ = handles(sb)
        res = await asyncio.gather(a.connect(), b.connect(), return_exceptions=True)
        assert all(isinstance(r, OSError) for r in res)
        assert not a.is_connected and not b.is_connected
        inner.fail = False
        await a.connect()  # a fresh real client
        assert len(inner.clients) == 2 and a.is_connected
    run(go())


def test_different_device_refused_case_insensitive_same_accepted():
    async def go():
        inner, sb = setup()
        (a,), _ = handles(sb, 1)
        await a.connect()
        other = sb.make_client(OTHER, lambda h: None)
        with pytest.raises(BleakError, match="only one device"):
            await other.connect()
        same = sb.make_client(SimpleNamespace(name="K", address=KICKR.address.lower()), lambda h: None)
        await same.connect()
        assert same.is_connected and inner.connects == 1
    run(go())


def test_last_detach_really_disconnects_and_callback_ignored():
    async def go():
        inner, sb = setup()
        (a, b), got = handles(sb)
        await a.connect()
        await b.connect()
        await a.disconnect()
        real = inner.clients[0]
        assert ("disconnect",) not in real.calls and not a.is_connected and b.is_connected
        await b.disconnect()
        assert real.calls.count(("disconnect",)) == 1 and not real.is_connected
        assert got == []  # the real disconnect's callback was ignored
        await a.connect()  # link is down: a fresh connect
        assert len(inner.clients) == 2
    run(go())


def test_drop_calls_every_attached_handle_once():
    async def go():
        inner, sb = setup()
        (a, b), got = handles(sb)
        await a.connect()
        await b.connect()
        inner.clients[0].drop()
        inner.clients[0].drop()  # duplicate callback is ignored
        assert sorted(map(id, got)) == sorted([id(a), id(b)])
        assert not a.is_connected and not b.is_connected
    run(go())


def test_drop_callback_error_does_not_block_others():
    async def go():
        inner, sb = setup()
        got = []

        def bad(_h):
            raise RuntimeError("boom")

        a = sb.make_client(KICKR, bad)
        b = sb.make_client(KICKR, got.append)
        await a.connect()
        await b.connect()
        inner.clients[0].drop()
        assert got == [b]
    run(go())


def test_stale_and_unattached_gatt_calls_raise():
    async def go():
        inner, sb = setup()
        (a, b), _ = handles(sb)
        for call in (
            lambda: a.read_gatt_char(CHAR_A),
            lambda: a.write_gatt_char(CHAR_A, b"x", response=True),
            lambda: a.start_notify(CHAR_A, lambda *_: None),
        ):
            with pytest.raises(BleakError, match="not connected"):
                await call()
        await a.connect()
        inner.clients[0].drop()
        with pytest.raises(BleakError, match="not connected"):
            await a.read_gatt_char(CHAR_A)
        with pytest.raises(BleakError, match="not connected"):
            await a.write_gatt_char(CHAR_A, b"", response=True)
    run(go())


def test_stale_disconnect_is_noop():
    async def go():
        inner, sb = setup()
        (a, b), _ = handles(sb)
        await a.connect()
        inner.clients[0].drop()
        await b.connect()  # new epoch, new real client
        assert len(inner.clients) == 2
        await a.disconnect()  # stale: must not detach b or disconnect anything
        assert b.is_connected and ("disconnect",) not in inner.clients[1].calls
        await b.disconnect()
        await b.disconnect()  # unattached: no-op, no raise
    run(go())


def test_disconnect_never_raises():
    async def go():
        inner, sb = setup()
        (a,), _ = handles(sb, 1)
        await a.connect()

        async def boom():
            raise BleakError("x")

        inner.clients[0].disconnect = boom
        await a.disconnect()
    run(go())


def test_discover_and_find_short_circuit_while_linked():
    async def go():
        inner, sb = setup()
        assert len(await sb.discover(1)) == 1 and inner.scans == 1  # delegates when down
        (a,), _ = handles(sb, 1)
        await a.connect()
        found = await sb.discover(1)
        assert inner.scans == 1 and len(found) == 1
        r = found[0]
        assert (r.name, r.address, r.rssi, r.cps_advertised, r.device) == ("KICKR", KICKR.address, 0, True, KICKR)
        assert CPS_SERVICE_UUID in r.service_uuids
        assert await sb.find_by_address(KICKR.address.lower(), 1) is KICKR
        assert await sb.find_by_address(OTHER.address, 1) is None
        assert inner.scans == 1
    run(go())


def test_discover_waits_for_connect_in_flight():
    async def go():
        inner, sb = setup()
        inner.delay = 0.01
        (a,), _ = handles(sb, 1)
        t = asyncio.create_task(a.connect())
        await asyncio.sleep(0)
        found = await sb.discover(1)
        await t
        assert inner.scans == 0 and found[0].device is KICKR
    run(go())


def test_duplicate_subscription_refused():
    async def go():
        inner, sb = setup()
        (a, b), _ = handles(sb)
        await a.connect()
        await b.connect()
        cb = lambda *_: None  # noqa: E731
        await a.start_notify(CHAR_A, cb)
        assert ("start", CHAR_A.uuid, cb) in inner.clients[0].calls  # subscribed directly with cb
        with pytest.raises(BleakError):
            await b.start_notify(SimpleNamespace(uuid=CHAR_A.uuid.lower()), cb)
        await b.start_notify(CHAR_B, cb)
    run(go())


def test_stop_notify_of_foreign_char_is_noop():
    async def go():
        inner, sb = setup()
        (a, b), _ = handles(sb)
        await a.connect()
        await b.connect()
        await a.start_notify(CHAR_A, lambda *_: None)
        await b.stop_notify(CHAR_A)
        assert ("stop", CHAR_A.uuid) not in inner.clients[0].calls
        await a.stop_notify(CHAR_A)
        assert ("stop", CHAR_A.uuid) in inner.clients[0].calls
    run(go())


def test_detach_stops_only_own_subscriptions():
    async def go():
        inner, sb = setup()
        (a, b), _ = handles(sb)
        await a.connect()
        await b.connect()
        await a.start_notify(CHAR_A, lambda *_: None)
        await b.start_notify(CHAR_B, lambda *_: None)
        await a.disconnect()
        calls = inner.clients[0].calls
        assert ("stop", CHAR_A.uuid) in calls and ("stop", CHAR_B.uuid) not in calls
        await b.start_notify(CHAR_A, lambda *_: None)  # a's subscription was released
    run(go())


def test_reconnect_after_drop_is_new_epoch_and_new_real_client():
    async def go():
        inner, sb = setup()
        (a, b), got = handles(sb)
        await a.connect()
        await b.connect()
        epoch = sb.epoch
        inner.clients[0].drop()
        assert sb.epoch == epoch + 1
        a2, b2 = sb.make_client(KICKR, got.append), sb.make_client(KICKR, got.append)
        await asyncio.gather(a2.connect(), b2.connect())
        assert inner.connects == 2 and len(inner.clients) == 2
        assert a2.is_connected and b2.is_connected and not a.is_connected
    run(go())


def test_cps_client_and_second_consumer_share_one_connect():
    """A real CpsClient plus a second handle attached to the same SharedBackend."""
    from fakes import KICKR as FKICKR, FakeBackend

    from wahoo_lsl.ble import CpsClient
    from wahoo_lsl.config import parse_config
    from wahoo_lsl.warn import WarnOnce

    async def go():
        fb = FakeBackend()
        sb = SharedBackend(fb)
        cps = CpsClient(parse_config({"device": {"name": "KICKR"}, "measurements": ["power"]}), lambda _i: None, WarnOnce(logging.getLogger("test")), backend=sb)
        info = await cps.connect_and_read()
        second = sb.make_client(FKICKR, lambda h: None)
        await second.connect()
        assert len(fb.clients) == 1 and info.name == FKICKR.name
        await cps._safe_disconnect()
        assert second.is_connected  # CPS detached, the second consumer keeps the link
        await second.disconnect()
    run(go())


def test_cancelled_sole_waiter_releases_link_when_connect_succeeds():
    async def go():
        inner, sb = setup()
        inner.delay = 0.02
        (a,), got = handles(sb, 1)
        t = asyncio.create_task(a.connect())
        await asyncio.sleep(0.005)
        t.cancel()
        await asyncio.sleep(0.05)
        real = inner.clients[0]
        assert real.calls == [("disconnect",)] and not real.is_connected
        assert got == []  # the release is intentional: no callbacks
        assert not a.is_connected
        await a.connect()  # a fresh real client afterwards
        assert len(inner.clients) == 2
    run(go())


def test_cancelled_sole_waiter_failed_connect_leaves_nothing():
    async def go():
        import gc
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda _l, ctx: errors.append(ctx))
        inner, sb = setup()
        inner.delay, inner.fail = 0.02, True
        (a,), _ = handles(sb, 1)
        t = asyncio.create_task(a.connect())
        await asyncio.sleep(0.005)
        t.cancel()
        await asyncio.sleep(0.05)
        gc.collect()
        assert errors == []
        inner.fail = False
        await a.connect()
        assert len(inner.clients) == 2 and a.is_connected
    run(go())


def test_cancelled_one_of_two_waiters_keeps_link():
    async def go():
        inner, sb = setup()
        inner.delay = 0.02
        (a, b), _ = handles(sb)
        ta = asyncio.create_task(a.connect())
        tb = asyncio.create_task(b.connect())
        await asyncio.sleep(0.005)
        ta.cancel()
        await tb
        await asyncio.sleep(0.01)
        assert b.is_connected and ("disconnect",) not in inner.clients[0].calls
    run(go())


def test_connect_raises_if_link_dropped_before_waiter_resumes():
    async def go():
        inner, sb = setup()
        (a,), _ = handles(sb, 1)
        real_connect = RealClient.connect

        async def connect_then_drop(self):
            await real_connect(self)
            asyncio.get_running_loop().call_soon(self.drop)

        RealClient.connect = connect_then_drop
        try:
            with pytest.raises(BleakError, match="not connected"):
                await a.connect()
        finally:
            RealClient.connect = real_connect
        assert not a.is_connected
    run(go())


def test_drop_during_detach_does_not_double_disconnect():
    async def go():
        inner, sb = setup()
        (a,), got = handles(sb, 1)
        await a.connect()
        await a.start_notify(CHAR_A, lambda *_: None)
        real = inner.clients[0]

        async def stop_and_drop(char):
            real.drop()

        real.stop_notify = stop_and_drop
        await a.disconnect()
        assert ("disconnect",) not in real.calls
        assert sb.epoch == 1  # only the drop bumped it
    run(go())
