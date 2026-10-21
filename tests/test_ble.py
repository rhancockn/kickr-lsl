import asyncio
import logging
import struct
from types import SimpleNamespace

import pytest
from fakes import ALWAYS, KICKR, FakeBackend

from wahoo_lsl.ble import (
    AmbiguousDevice,
    BleSetupError,
    CpsClient,
    DeviceNotFound,
    NoCpsService,
    ReconnectExhausted,
    ReconnectPolicy,
    ScanResult,
    scan_cps,
)
from wahoo_lsl.config import parse_config
from wahoo_lsl.pipeline import Event, RawSample
from wahoo_lsl.warn import WarnOnce

POWER = struct.pack("<Hh", 0, 200)
UUID = "12345678-1234-5678-1234-567812345678"


def make_cfg(device=None, reconnect=None):
    return parse_config(
        {
            "device": {"name": "KICKR", **(device or {})},
            "measurements": ["power", "wheel_revolutions", "accumulated_torque"],
            "reconnect": {"backoff_s": [0.01], **(reconnect or {})},
        }
    )


def make_client(backend, cfg=None, platform="linux"):
    items: list = []
    warn = WarnOnce(logging.getLogger("wahoo_lsl.test"))
    client = CpsClient(cfg or make_cfg(), items.append, warn, backend=backend, platform=platform)
    return client, items


def kinds(items):
    return [i.kind for i in items if isinstance(i, Event)]


def raws(items):
    return [i for i in items if isinstance(i, RawSample)]


def warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def test_reconnect_policy():
    p = ReconnectPolicy([1, 2, 5], 0)
    assert [p.delay(i) for i in range(5)] == [1, 2, 5, 5, 5]
    assert p.exhausted(1000) is False
    q = ReconnectPolicy([1], 3)
    assert q.exhausted(2) is False
    assert q.exhausted(3) is True


def test_find_by_name_single_match():
    client, _ = make_client(FakeBackend())
    assert asyncio.run(client.find_device()) is KICKR


def test_find_by_name_prefers_cps_advertisers():
    snap = SimpleNamespace(name="KICKR SNAP", address="AA:BB:CC:DD:EE:02")
    backend = FakeBackend(
        [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR), ScanResult(snap.name, snap.address, -40, False, snap)]
    )
    client, _ = make_client(backend)
    assert asyncio.run(client.find_device()) is KICKR


@pytest.mark.parametrize("platform, field", [("linux", "device.mac"), ("darwin", "device.uuid")])
def test_ambiguous_name_lists_candidates(platform, field):
    other = SimpleNamespace(name="KICKR BIKE", address="AA:BB:CC:DD:EE:03")
    backend = FakeBackend(
        [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR), ScanResult(other.name, other.address, -60, True, other)]
    )
    client, _ = make_client(backend, platform=platform)
    with pytest.raises(AmbiguousDevice) as exc:
        asyncio.run(client.find_device())
    msg = str(exc.value)
    assert KICKR.address in msg and other.address in msg and field in msg


def test_name_not_found():
    client, _ = make_client(FakeBackend([]))
    with pytest.raises(DeviceNotFound):
        asyncio.run(client.find_device())


def test_mac_used_on_linux():
    backend = FakeBackend()
    client, _ = make_client(backend, make_cfg({"mac": KICKR.address, "uuid": UUID}), platform="linux")
    assert asyncio.run(client.find_device()) is KICKR
    assert backend.address_lookups == [KICKR.address]


def test_uuid_used_on_darwin():
    mac_dev = SimpleNamespace(name="KICKR CORE 1234", address=UUID)
    backend = FakeBackend([ScanResult(mac_dev.name, UUID, -50, True, mac_dev)])
    client, _ = make_client(backend, make_cfg({"mac": KICKR.address, "uuid": UUID}), platform="darwin")
    assert asyncio.run(client.find_device()) is mac_dev
    assert backend.address_lookups == [UUID]


def test_address_not_found():
    client, _ = make_client(FakeBackend(), make_cfg({"mac": "11:22:33:44:55:66"}))
    with pytest.raises(DeviceNotFound):
        asyncio.run(client.find_device())


def test_read_info_reads_features_and_disconnects():
    backend = FakeBackend()
    client, items = make_client(backend)
    info = asyncio.run(client.read_info())
    assert info.name == KICKR.name
    assert info.address == KICKR.address
    assert info.address_kind == "mac"
    assert info.features.supported == {"power", "wheel_revolutions", "crank_revolutions"}
    assert info.sensor_location == "Rear hub"
    assert backend.clients[0].is_connected is False
    assert items == []  # intentional disconnect emits nothing


def test_read_info_no_cps_service():
    backend = FakeBackend(has_cps=False)
    client, _ = make_client(backend)
    with pytest.raises(NoCpsService):
        asyncio.run(client.read_info())
    assert backend.clients[0].is_connected is False


def test_read_info_comms_error_is_setup_error():
    client, _ = make_client(FakeBackend(connect_failures=1))
    with pytest.raises(BleSetupError):
        asyncio.run(client.read_info())


def test_read_info_bad_feature_payload_is_setup_error():
    client, _ = make_client(FakeBackend(feature_bytes=b"\x01"))
    with pytest.raises(BleSetupError):
        asyncio.run(client.read_info())


def test_run_streams_and_stops_cleanly(caplog):
    backend = FakeBackend(sessions=[([POWER, POWER], False)])
    client, items = make_client(backend)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: len(raws(items)) == 2)
        stop.set()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    samples = raws(items)
    assert [s.seq for s in samples] == [0, 1]
    assert samples[0].data == POWER
    assert samples[0].t_lsl <= samples[1].t_lsl
    assert kinds(items) == ["connected", "features"]
    features = next(i for i in items if isinstance(i, Event) and i.kind == "features")
    assert features.detail["unavailable"] == ["accumulated_torque"]
    assert backend.clients[0].is_connected is False
    ws = warnings(caplog)
    assert sum("accumulated_torque" in w for w in ws) == 1
    assert not any("lost" in w for w in ws)


def test_disconnect_triggers_reconnect_and_seq_continues(caplog):
    backend = FakeBackend(sessions=[([POWER], True), ([POWER], False)])
    client, items = make_client(backend)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: len(raws(items)) == 2 and "reconnected" in kinds(items))
        stop.set()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert [s.seq for s in raws(items)] == [0, 1]
    assert kinds(items) == [
        "connected",
        "features",
        "disconnected",
        "reconnect_attempt",
        "connected",
        "features",
        "reconnected",
    ]
    assert len(backend.clients) == 2
    ws = warnings(caplog)
    assert sum("accumulated_torque" in w for w in ws) == 1  # not repeated on reconnect
    assert sum("lost" in w for w in ws) == 1


def test_reconnect_gives_up_after_max_attempts():
    backend = FakeBackend()
    client, items = make_client(backend, make_cfg(reconnect={"max_attempts": 2}))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: backend.clients and backend.clients[0].notify_callback is not None)
        backend.connect_failures = ALWAYS
        backend.clients[0].drop()
        await task

    with pytest.raises(ReconnectExhausted):
        asyncio.run(scenario())
    assert kinds(items).count("reconnect_attempt") == 2


def test_reconnect_disabled_raises():
    backend = FakeBackend()
    client, items = make_client(backend, make_cfg(reconnect={"enabled": False}))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: backend.clients and backend.clients[0].notify_callback is not None)
        backend.clients[0].drop()
        await task

    with pytest.raises(ReconnectExhausted):
        asyncio.run(scenario())
    assert "reconnect_attempt" not in kinds(items)


def test_stop_during_backoff_returns_promptly():
    backend = FakeBackend()
    client, items = make_client(backend, make_cfg(reconnect={"backoff_s": [30]}))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await until(lambda: backend.clients and backend.clients[0].notify_callback is not None)
        backend.clients[0].drop()
        await until(lambda: "disconnected" in kinds(items))
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert "reconnect_attempt" not in kinds(items)
    assert len(backend.clients) == 1


def test_setup_failure_when_subscribe_fails():
    backend = FakeBackend(fail_start_notify=True)
    client, _ = make_client(backend)

    async def scenario():
        await client.run(asyncio.Event())

    with pytest.raises(BleSetupError):
        asyncio.run(scenario())
    assert backend.clients[0].is_connected is False


def test_scan_cps_filters_to_cps_or_kickr():
    phone = SimpleNamespace(name="Phone", address="AA:BB:CC:DD:EE:09")
    foo = SimpleNamespace(name="Foo", address="AA:BB:CC:DD:EE:08")
    bike = SimpleNamespace(name="KICKR BIKE", address="AA:BB:CC:DD:EE:07")
    backend = FakeBackend(
        [
            ScanResult(foo.name, foo.address, -50, True, foo),
            ScanResult(bike.name, bike.address, -60, False, bike),
            ScanResult(phone.name, phone.address, -70, False, phone),
        ]
    )
    names = [r.name for r in asyncio.run(scan_cps(1.0, backend))]
    assert names == ["Foo", "KICKR BIKE"]
