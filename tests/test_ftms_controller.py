import asyncio
import struct

import pytest

from wahoo_lsl.ble import ReconnectExhausted
from wahoo_lsl.ftms.config import parse_ftms_config
from wahoo_lsl.ftms.controller import Controller, LinkDown
from wahoo_lsl.ftms.link import CommandRejected, FtmsSetupError, LinkError
from wahoo_lsl.ftms.protocol import BikeData, CpResponse, PowerRange, StatusEvent


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class FakeLink:
    """Controller-facing stand-in for FtmsLink. command_results entries: None (success),
    an exception instance (raised), or an asyncio.Future (awaited, then treated the same)."""

    def __init__(self, clock: FakeClock, power_range: PowerRange = PowerRange(0, 1500, 1)) -> None:
        self.clock = clock
        self.power_range = power_range
        self.sent: list[tuple[float, int]] = []
        self.opens = 0
        self.closes = 0
        self.open_errors: list[BaseException] = []
        self.command_results: list = []
        self.on_bike_data = self.on_status = self.on_link_lost = None

    async def open(self) -> None:
        self.opens += 1
        if self.open_errors:
            raise self.open_errors.pop(0)

    async def command(self, payload: bytes) -> CpResponse:
        watts = struct.unpack_from("<h", payload, 1)[0]
        self.sent.append((self.clock.t, watts))
        outcome = self.command_results.pop(0) if self.command_results else None
        if isinstance(outcome, asyncio.Future):
            outcome = await outcome
        if isinstance(outcome, BaseException):
            raise outcome
        await asyncio.sleep(0)
        return CpResponse(payload[0], 1)

    async def close(self) -> None:
        self.closes += 1


def make(ftms=None, reconnect=None, power_range=PowerRange(0, 1500, 1)):
    cfg = parse_ftms_config(
        {
            "device": {"name": "KICKR"},
            "reconnect": {"backoff_s": [0.01], **(reconnect or {})},
            "ftms": ftms or {},
        }
    )
    clock = FakeClock()
    link = FakeLink(clock, power_range)
    return Controller(cfg, link, now=clock.now, sleep=clock.sleep), link, clock


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def watts(link):
    return [w for _, w in link.sent]


def test_clamp_and_step():
    ctl, _, _ = make(ftms={"max_watts": 798}, power_range=PowerRange(0, 1000, 5))
    assert ctl.clamp(-5) == 0
    assert ctl.clamp(1203) == 795
    assert ctl.clamp(152) == 150
    assert ctl.clamp(153) == 155
    assert ctl.clamp(799) == 795
    ctl2, _, _ = make(power_range=PowerRange(50, 1000, 0))
    assert ctl2.clamp(0) == 50
    assert ctl2.clamp(77) == 77


def test_request_before_start_raises_link_down():
    ctl, _, _ = make()
    with pytest.raises(LinkDown):
        ctl.request_target(100)


def test_start_marks_ready_at_zero():
    ctl, link, clock = make()
    asyncio.run(ctl.start())
    s = ctl.state
    assert (s.link, s.link_epoch, s.requested_w, s.confirmed_w, s.confirmed_t_lsl) == ("ready", 1, 0, 0, clock.t)
    assert ctl.pending is False
    assert link.sent == []


def test_single_set_sent_immediately():
    ctl, link, clock = make()

    async def scenario():
        await ctl.start()
        clock.t = 200.0
        assert ctl.request_target(150) == (150, False)
        await until(lambda: ctl.state.confirmed_w == 150)
        await ctl.close()

    asyncio.run(scenario())
    assert link.sent[0] == (200.0, 150)
    assert ctl.state.confirmed_t_lsl is not None


def test_burst_coalesced_and_rate_limited():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        ctl.request_target(150)
        ctl.request_target(200)
        await until(lambda: ctl.state.confirmed_w == 200)
        await asyncio.sleep(0.01)
        sent = list(link.sent)
        await ctl.close()
        return sent

    sent = asyncio.run(scenario())
    assert [w for _, w in sent] == [100, 200]
    assert sent[1][0] - sent[0][0] == pytest.approx(0.5)


def test_set_equal_to_confirmed_is_not_sent():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        assert ctl.request_target(0) == (0, False)
        await asyncio.sleep(0.01)
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link) == [0]  # only the shutdown spin-down


def test_clamped_request_reports_clamped():
    ctl, _, _ = make(ftms={"max_watts": 400})

    async def scenario():
        await ctl.start()
        result = ctl.request_target(450)
        await ctl.close()
        return result

    assert asyncio.run(scenario()) == (400, True)


def test_rejection_retried_then_abandoned_until_new_request():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [CommandRejected(0x05, 0x04)] * 5
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 3)
        await asyncio.sleep(0.02)
        assert len(link.sent) == 3
        assert "rejected" in ctl.state.last_error
        assert ctl.pending
        link.command_results = []
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link)[:4] == [100, 100, 100, 100]


def test_link_failures_trigger_link_lost():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [LinkError("write failed")] * 3
        ctl.request_target(100)
        await until(lambda: ctl.state.link == "reconnecting")
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link) == [100, 100, 100]  # no spin-down send: the link is not ready
    assert "link lost: 3 consecutive control-point failures" in ctl.state.last_error


def test_status_target_power_changed_confirms():
    ctl, link, _ = make()
    asyncio.run(ctl.start())
    link.on_status(StatusEvent(0x08, "target power changed", 180), 5.0)
    assert (ctl.state.confirmed_w, ctl.state.confirmed_t_lsl) == (180, 5.0)
    link.on_status(StatusEvent(0x02, "stopped or paused by user", None), 6.0)
    assert ctl.state.confirmed_w == 180


def test_bike_data_cached_and_snapshot():
    ctl, link, clock = make()
    asyncio.run(ctl.start())
    link.on_bike_data(BikeData(143, 90.0, 30.0), 99.5)
    link.on_bike_data(BikeData(None, None, 30.0), 99.9)
    snap = ctl.snapshot()
    assert snap == {
        "link": "ready",
        "link_epoch": 1,
        "target": {"requested_w": 0, "confirmed_w": 0, "pending": False, "t_lsl": clock.t},
        "power": {"watts": 143, "t_lsl": 99.5, "age_s": 0.5},
        "last_error": None,
        "last_error_t_lsl": None,
    }


def test_snapshot_before_any_power():
    ctl, _, _ = make()
    assert ctl.snapshot()["power"] == {"watts": None, "t_lsl": None, "age_s": None}
    assert ctl.snapshot()["link"] == "connecting"


def test_spin_down_bypasses_rate_limit():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)
        t_first = link.sent[0][0]
        assert await ctl.spin_down("test") is True
        assert link.sent[-1] == (t_first, 0)
        await ctl.close()

    asyncio.run(scenario())
    assert ctl.state.requested_w == 0


@pytest.mark.parametrize("error", [LinkError("gone"), CommandRejected(0x05, 0x04), RuntimeError("bug")])
def test_spin_down_never_raises(error):
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [error]
        ok = await ctl.spin_down("test")
        await ctl.close()
        return ok

    assert asyncio.run(scenario()) is False
    assert "spin-down (test) failed" in ctl.state.last_error


def test_spin_down_when_not_ready_sends_nothing():
    ctl, link, _ = make()
    assert asyncio.run(ctl.spin_down("test")) is False
    assert link.sent == []


def test_link_lost_then_reconnect_resets_to_zero():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)
        link.on_link_lost("connection lost")
        assert ctl.state.link == "reconnecting"
        assert (ctl.state.requested_w, ctl.state.confirmed_w) == (0, None)
        with pytest.raises(LinkDown):
            ctl.request_target(120)
        await until(lambda: ctl.state.link_epoch == 2)
        stop.set()
        await task
        await ctl.close()

    asyncio.run(scenario())
    s = ctl.state
    assert link.opens == 2 and link.closes >= 1
    assert "link lost: connection lost" in s.last_error
    assert s.last_error_t_lsl is not None


def test_stale_confirmation_ignored_after_reconnect():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        gate = asyncio.get_running_loop().create_future()
        link.command_results = [gate]
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        link.on_link_lost("connection lost")
        await until(lambda: ctl.state.link_epoch == 2)
        gate.set_result(None)  # the old in-flight set "succeeds" after the reconnect
        await asyncio.sleep(0.01)
        confirmed = ctl.state.confirmed_w
        stop.set()
        await task
        await ctl.close()
        return confirmed

    assert asyncio.run(scenario()) == 0


def test_reconnect_exhausted():
    ctl, link, _ = make(reconnect={"max_attempts": 2})

    async def scenario():
        await ctl.start()
        link.open_errors = [FtmsSetupError("nope"), FtmsSetupError("nope")]
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        link.on_link_lost("connection lost")
        with pytest.raises(ReconnectExhausted):
            await task
        await ctl.close()

    asyncio.run(scenario())
    assert link.opens == 3


def test_reconnect_disabled():
    ctl, link, _ = make(reconnect={"enabled": False})

    async def scenario():
        await ctl.start()
        task = asyncio.create_task(ctl.run(asyncio.Event()))
        link.on_link_lost("connection lost")
        with pytest.raises(ReconnectExhausted, match="disabled"):
            await task
        await ctl.close()

    asyncio.run(scenario())


def test_stop_during_reconnect_returns():
    ctl, link, _ = make(reconnect={"backoff_s": [5]})

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        link.on_link_lost("connection lost")
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 1.0)
        await ctl.close()

    asyncio.run(scenario())
    assert link.opens == 1


def test_sender_crash_surfaces_from_run():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [RuntimeError("boom")]
        task = asyncio.create_task(ctl.run(asyncio.Event()))
        ctl.request_target(100)
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(task, 1.0)
        await ctl.close("internal error")

    asyncio.run(scenario())
    assert watts(link)[-1] == 0


def test_close_cancels_inflight_and_spins_down():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.command_results = [asyncio.get_running_loop().create_future()]  # never resolves
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        await asyncio.wait_for(ctl.close(), 1.0)

    asyncio.run(scenario())
    assert watts(link) == [100, 0]
    assert link.closes == 1
    assert ctl.state.link == "closed"


@pytest.mark.parametrize("outcome", [LinkError("link closed"), CommandRejected(0x05, 0x04)])
def test_inflight_failure_after_link_lost_keeps_link_lost_error(outcome):
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        stop = asyncio.Event()
        task = asyncio.create_task(ctl.run(stop))
        gate = asyncio.get_running_loop().create_future()
        link.command_results = [gate]
        ctl.request_target(100)
        await until(lambda: len(link.sent) == 1)
        link.on_link_lost("connection lost")
        gate.set_result(outcome)  # the in-flight set fails after the link dropped
        await until(lambda: ctl.state.link_epoch == 2)
        await asyncio.sleep(0.01)
        err = ctl.state.last_error
        stop.set()
        await task
        await ctl.close()
        return err

    assert asyncio.run(scenario()).startswith("link lost: connection lost")
