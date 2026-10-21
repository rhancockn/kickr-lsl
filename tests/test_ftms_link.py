import asyncio
import logging
import struct
from types import SimpleNamespace

import pytest
from ftms_fakes import FTMS_KICKR, FakeFtmsBackend

import wahoo_lsl.ble as ble_mod
from wahoo_lsl.ble import ScanResult
from wahoo_lsl.ftms import protocol as p
from wahoo_lsl.ftms.config import parse_ftms_config
from wahoo_lsl.ftms.link import (
    CommandRejected,
    CommandTimeout,
    FtmsLink,
    FtmsSetupError,
    FtmsUnsupported,
    LinkError,
)


def make_cfg(ack_timeout_s=0.05):
    return parse_ftms_config(
        {"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01]}, "ftms": {"ack_timeout_s": ack_timeout_s}}
    )


def make_link(backend, ack_timeout_s=0.05):
    link = FtmsLink(make_cfg(ack_timeout_s), backend=backend, platform="linux")
    lost: list[str] = []
    link.on_link_lost = lost.append
    return link, lost


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def test_open_subscribes_then_takes_control_at_zero_watts():
    backend = FakeFtmsBackend()
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        assert link.connected
        await link.close()

    asyncio.run(scenario())
    kinds = [entry[0] for entry in backend.log]
    first_write = kinds.index("write")
    subscribed_before_write = [e[1] for e in backend.log[:first_write] if e[0] == "subscribe"]
    assert subscribed_before_write == [p.CONTROL_POINT_UUID, p.STATUS_UUID, p.INDOOR_BIKE_DATA_UUID]
    assert backend.writes == [b"\x00", b"\x07", b"\x05\x00\x00"]
    assert link.power_range == p.PowerRange(0, 2000, 1)
    assert link.features.power_target_supported


def test_open_prefers_device_advertising_ftms():
    cps_only = SimpleNamespace(name="KICKR V6 1234", address="AA:BB:CC:DD:EE:09")
    backend = FakeFtmsBackend(
        [
            ScanResult(cps_only.name, cps_only.address, -30, True, cps_only),
            ScanResult(FTMS_KICKR.name, FTMS_KICKR.address, -50, False, FTMS_KICKR, frozenset({p.FTMS_SERVICE_UUID})),
        ]
    )
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        await link.close()

    asyncio.run(scenario())
    assert backend.clients[0].device is FTMS_KICKR


def test_open_without_ftms_service_is_setup_error():
    backend = FakeFtmsBackend(has_ftms=False)
    link, lost = make_link(backend)
    with pytest.raises(FtmsSetupError, match="Fitness Machine Service"):
        asyncio.run(link.open())
    assert backend.clients[0].is_connected is False
    assert lost == []


def test_open_power_target_unsupported():
    backend = FakeFtmsBackend(feature_bytes=struct.pack("<II", 0xFF, 0))
    link, _ = make_link(backend)
    with pytest.raises(FtmsUnsupported):
        asyncio.run(link.open())
    assert backend.writes == []


def test_open_missing_power_range_uses_default(caplog):
    backend = FakeFtmsBackend(has_power_range=False)
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        await link.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert link.power_range == p.DEFAULT_POWER_RANGE
    assert any("Supported Power Range" in r.getMessage() for r in caplog.records)


def test_open_request_control_rejected_is_setup_error():
    backend = FakeFtmsBackend(responses={0x00: [0x05]})
    link, _ = make_link(backend)
    with pytest.raises(FtmsSetupError, match="control not permitted"):
        asyncio.run(link.open())
    assert backend.clients[0].is_connected is False


def test_open_connect_failure_is_setup_error():
    backend = FakeFtmsBackend(connect_failures=1)
    link, _ = make_link(backend)
    with pytest.raises(FtmsSetupError, match="simulated connect failure"):
        asyncio.run(link.open())


def test_command_success_and_early_indication():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "ok", "early"]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        first = await link.command(p.set_target_power(150))
        second = await link.command(p.set_target_power(160))
        await link.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == p.CpResponse(0x05, 0x01)
    assert second == p.CpResponse(0x05, 0x01)


def test_command_timeout_then_next_command_works():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "drop"]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        with pytest.raises(CommandTimeout):
            await link.command(p.set_target_power(150))
        assert (await link.command(p.set_target_power(150))).result == p.RESULT_SUCCESS
        await link.close()

    asyncio.run(scenario())


def test_command_rejected():
    backend = FakeFtmsBackend(responses={0x05: ["ok", 0x03]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        with pytest.raises(CommandRejected, match="invalid parameter") as info:
            await link.command(p.set_target_power(9999))
        assert (info.value.opcode, info.value.result) == (0x05, 0x03)
        await link.close()

    asyncio.run(scenario())


def test_command_write_failure_is_link_error_not_timeout():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "raise"]})
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        with pytest.raises(LinkError) as info:
            await link.command(p.set_target_power(150))
        assert not isinstance(info.value, CommandTimeout)
        await link.close()

    asyncio.run(scenario())


def test_command_when_not_connected():
    link, _ = make_link(FakeFtmsBackend())
    with pytest.raises(LinkError, match="not connected"):
        asyncio.run(link.command(p.set_target_power(100)))


def test_unsolicited_and_malformed_indications(caplog):
    backend = FakeFtmsBackend()
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        client = backend.clients[0]
        client.notify(p.CONTROL_POINT_UUID, b"\x80\x05\x01")  # nobody waiting: ignored
        client.notify(p.CONTROL_POINT_UUID, b"\x01")  # malformed: warning
        await link.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert any("control point" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_status_and_bike_data_forwarded_with_timestamps():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)
    statuses, bikes = [], []
    link.on_status = lambda ev, t: statuses.append((ev, t))
    link.on_bike_data = lambda data, t: bikes.append((data, t))

    async def scenario():
        await link.open()
        client = backend.clients[0]
        client.notify(p.STATUS_UUID, b"\x08\x96\x00")
        client.notify(p.INDOOR_BIKE_DATA_UUID, struct.pack("<Hh", 0x0041, 150))
        client.notify(p.INDOOR_BIKE_DATA_UUID, b"\x44")  # malformed: dropped with a warning
        await link.close()

    asyncio.run(scenario())
    assert statuses[0][0] == p.StatusEvent(0x08, "target power changed", 150)
    assert bikes[0][0] == p.BikeData(150, None, None)
    assert isinstance(statuses[0][1], float) and isinstance(bikes[0][1], float)
    assert len(bikes) == 1
    assert lost == []


def test_control_permission_lost_reports_link_lost():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)

    async def scenario():
        await link.open()
        backend.clients[0].notify(p.STATUS_UUID, b"\xff")
        await until(lambda: lost)
        await link.close()

    asyncio.run(scenario())
    assert lost == ["control permission lost"]


def test_unexpected_disconnect_reports_link_lost():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)

    async def scenario():
        await link.open()
        backend.clients[0].drop()
        await until(lambda: lost)
        await link.close()

    asyncio.run(scenario())
    assert lost == ["connection lost"]


def test_close_is_quiet_and_idempotent():
    backend = FakeFtmsBackend()
    link, lost = make_link(backend)

    async def scenario():
        await link.open()
        await link.close()
        await link.close()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert lost == []
    assert not link.connected
    assert ("unsubscribe", p.CONTROL_POINT_UUID) in backend.log


def test_close_fails_pending_command():
    backend = FakeFtmsBackend(responses={0x05: ["ok", "drop"]})
    link, _ = make_link(backend, ack_timeout_s=5.0)

    async def scenario():
        await link.open()
        pending = asyncio.create_task(link.command(p.set_target_power(150)))
        await until(lambda: len(backend.writes) == 4)
        await link.close()
        with pytest.raises(LinkError, match="link closed"):
            await pending

    asyncio.run(scenario())


def test_close_bounded_when_disconnect_hangs(monkeypatch):
    monkeypatch.setattr(ble_mod, "SHUTDOWN_TIMEOUT_S", 0.05)
    backend = FakeFtmsBackend()
    link, _ = make_link(backend)

    async def scenario():
        await link.open()
        backend.hang_disconnect = True
        await asyncio.wait_for(link.close(), 1.0)

    asyncio.run(scenario())
