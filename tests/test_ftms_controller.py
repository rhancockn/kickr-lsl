import asyncio
import logging
import math
import struct

import pytest

from wahoo_lsl.ble import ReconnectExhausted
from wahoo_lsl.ftms.config import parse_ftms_config
from wahoo_lsl.ftms.controller import SHUTDOWN_SPIN_DOWN_ATTEMPTS, Controller, LinkDown
from wahoo_lsl.ftms.link import CommandRejected, CommandTimeout, FtmsSetupError, LinkError
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
            "ftms": {"slew_w_per_s": 0, **(ftms or {})},
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
        "target": {
            "requested_w": 0,
            "confirmed_w": 0,
            "pending": False,
            "t_lsl": clock.t,
            "setpoint_w": 0,
            "ramp_end_t_lsl": None,
        },
        "power": {"watts": 143, "t_lsl": 99.5, "age_s": 0.5},
        "last_error": None,
        "last_error_t_lsl": None,
        "slew": {"w_per_s": 0.0, "update_interval_s": 10.0},
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


def test_close_retries_failed_spin_down(caplog):
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(150)
        await until(lambda: ctl.state.confirmed_w == 150)
        link.command_results = [LinkError("lost indication"), CommandTimeout("no response")]
        await asyncio.wait_for(ctl.close(), 1.0)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert watts(link) == [150, 0, 0, 0]  # two failures, third attempt confirmed
    assert link.closes == 1
    assert any("spin-down (shutdown): 0 W confirmed" in r.getMessage() for r in caplog.records)


def test_close_gives_up_after_bounded_spin_down_attempts(caplog):
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(150)
        await until(lambda: ctl.state.confirmed_w == 150)
        link.command_results = [LinkError("gone")] * 10
        await asyncio.wait_for(ctl.close(), 1.0)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert watts(link) == [150] + [0] * SHUTDOWN_SPIN_DOWN_ATTEMPTS
    assert link.closes == 1
    assert ctl.state.link == "closed"
    assert any("may still hold" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_close_does_not_retry_when_link_not_ready():
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        link.on_link_lost("connection lost")
        await asyncio.wait_for(ctl.close(), 1.0)

    asyncio.run(scenario())
    assert link.sent == []
    assert link.closes == 1


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


@pytest.mark.parametrize("error", [LinkError("gone"), CommandTimeout("no reply")])
def test_failed_spin_down_is_retried_by_the_sender(error):
    ctl, link, _ = make()

    async def scenario():
        await ctl.start()
        ctl.request_target(150)
        await until(lambda: ctl.state.confirmed_w == 150)
        link.command_results = [error]
        assert await ctl.spin_down("test") is False
        await until(lambda: ctl.state.confirmed_w == 0)
        await ctl.close()

    asyncio.run(scenario())
    assert watts(link)[-1] == 0
    assert watts(link).count(0) >= 2  # the failed attempt and the sender's retry


SLEW = {"slew_w_per_s": 10}


def run(scenario):
    asyncio.run(scenario())


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_ramp_sends_on_interval_grid_and_final_target():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()  # clock 100.0, at 0 W
        assert ctl.request_target(100) == (100, False)  # 0 -> 100 at 10 W/s: one update at +10 s
        await until(lambda: ctl.state.confirmed_w == 100)
        assert clock.t == 110.0
        ctl.request_target(250)  # 100 -> 250: 200 W at +10 s, 250 W at +15 s
        await until(lambda: ctl.state.confirmed_w == 250)

    run(scenario)
    assert link.sent == [(110.0, 100), (120.0, 200), (125.0, 250)]
    assert ctl.pending is False


def test_short_ramp_single_send_at_end():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(30)
        await until(lambda: ctl.state.confirmed_w == 30)

    run(scenario)
    assert link.sent == [(103.0, 30)]


def test_decrease_is_sent_directly_without_ramp():
    ctl, link, clock = make(ftms={"slew_w_per_s": 10, "slew_update_interval_s": 5})

    async def scenario():
        await ctl.start()
        ctl.set_slew_rate(0)
        ctl.request_target(200)  # direct (slew 0)
        await until(lambda: ctl.state.confirmed_w == 200)
        ctl.set_slew_rate(10)
        ctl.request_target(180)  # a decrease: not slewed, only rate-limited by max_rate_hz
        assert ctl._ramp is None
        await until(lambda: ctl.state.confirmed_w == 180)

    run(scenario)
    assert link.sent == [(100.0, 200), (100.5, 180)]


def test_decrease_mid_ramp_is_immediate_and_cancels_ramp():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        clock.t = 105.0  # setpoint 50 W
        ctl.request_target(30)
        await until(lambda: ctl.state.confirmed_w == 30)

    run(scenario)
    assert link.sent == [(105.0, 30)]
    assert ctl._ramp is None


def test_decrease_while_sender_parked_is_sent():
    ctl, link, clock = make_parked(SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        await settle()  # sender parked waiting for the first ramp update
        clock.t = 105.0
        ctl.request_target(30)
        await until(lambda: ctl.state.confirmed_w == 30)

    run(scenario)
    assert link.sent == [(105.0, 30)]


def test_decrease_below_setpoint_but_above_last_sent_is_immediate():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.set_slew_rate(0)
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)  # sent at 100.0
        ctl.set_slew_rate(10)
        ctl.request_target(250)  # ramp 100 -> 250
        clock.t = 103.0  # setpoint 130
        ctl.request_target(120)  # below setpoint, above last sent: direct
        await until(lambda: ctl.state.confirmed_w == 120)

    run(scenario)
    assert link.sent == [(100.0, 100), (103.0, 120)]
    assert ctl._ramp is None


def test_new_target_mid_ramp_starts_from_interpolated_value():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)  # 0 -> 250 starting at 100.0
        clock.t = 105.0  # interpolated value now 50 W (nothing sent yet)
        ctl.request_target(100)  # 50 -> 100: D = 5 s, end 110.0
        assert ctl.snapshot()["target"]["setpoint_w"] == 50
        await until(lambda: ctl.state.confirmed_w == 100)

    run(scenario)
    assert link.sent == [(110.0, 100)]


def test_rate_change_mid_ramp_restarts_from_interpolated_value():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        clock.t = 105.0  # at 50 W
        assert ctl.set_slew_rate(50) == 50.0  # 50 -> 250 at 50 W/s: end 109.0
        await until(lambda: ctl.state.confirmed_w == 250)

    run(scenario)
    assert link.sent == [(109.0, 250)]


def test_rate_change_to_zero_sends_target_directly():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        clock.t = 105.0
        ctl.set_slew_rate(0)
        await until(lambda: ctl.state.confirmed_w == 250)

    run(scenario)
    assert link.sent == [(105.0, 250)]


def test_ramp_values_rounded_to_step_and_duplicates_skipped():
    ctl, link, clock = make(ftms={"slew_w_per_s": 10, "slew_update_interval_s": 2}, power_range=PowerRange(0, 1500, 50))

    async def scenario():
        await ctl.start()
        ctl.request_target(100)  # due at +2 (20->0 skip), +4 (40->50), +6 (60->50 skip), +8 (80->100), +10 (100 skip)
        await until(lambda: ctl.state.confirmed_w == 100 and ctl._ramp is None)

    run(scenario)
    assert link.sent == [(104.0, 50), (108.0, 100)]


def test_max_rate_floor_applies_to_ramp_updates():
    ctl, link, clock = make(ftms={"slew_w_per_s": 10, "slew_update_interval_s": 1, "max_rate_hz": 0.1})

    async def scenario():
        await ctl.start()
        ctl.request_target(30)  # updates due at +1, +2, +3, but sends must be >= 10 s apart
        await until(lambda: ctl.state.confirmed_w == 30)

    run(scenario)
    assert link.sent == [(101.0, 10), (111.0, 30)]


def test_spin_down_cancels_ramp():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        assert await ctl.spin_down("test") is True
        await settle()

    run(scenario)
    assert link.sent == [(100.0, 0)]
    assert ctl._ramp is None and ctl.state.requested_w == 0


def test_link_loss_cancels_ramp_and_keeps_rate():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.set_slew_rate(20)
        ctl.request_target(250)
        ctl._on_link_lost("test")
        await settle()

    run(scenario)
    assert link.sent == []
    assert ctl._ramp is None
    assert ctl.slew_w_per_s == 20.0


def test_reset_slew_restores_default_and_cancels_ramp():
    ctl, link, clock = make(ftms={"slew_w_per_s": 7})

    async def scenario():
        await ctl.start()
        ctl.set_slew_rate(30)
        ctl.request_target(250)
        ctl.reset_slew()

    run(scenario)
    assert ctl.slew_w_per_s == 7.0
    assert ctl._ramp is None


def test_set_slew_rate_validation():
    ctl, _, _ = make(ftms=SLEW)
    for bad in (-1, 1001, math.nan, math.inf):
        with pytest.raises(ValueError):
            ctl.set_slew_rate(bad)
    assert ctl.set_slew_rate(0) == 0.0
    assert ctl.set_slew_rate(1000) == 1000.0


def test_set_slew_rate_allowed_while_link_down():
    ctl, _, _ = make(ftms=SLEW)  # never started: link "connecting"
    assert ctl.set_slew_rate(25) == 25.0


def test_snapshot_reports_ramp():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        clock.t = 105.0
        return ctl.snapshot()

    snap = asyncio.run(scenario())
    assert snap["target"]["requested_w"] == 250
    assert snap["target"]["setpoint_w"] == 50
    assert snap["target"]["ramp_end_t_lsl"] == 125.0
    assert snap["target"]["pending"] is True
    assert snap["slew"] == {"w_per_s": 10.0, "update_interval_s": 10.0}


def make_parked(ftms=None):
    """Controller whose injected sleep blocks forever, so the sender stays parked in _sleep_or_wake."""
    ctl, link, clock = make(ftms=ftms)
    never = asyncio.Event()

    async def blocked_sleep(seconds: float) -> None:
        await never.wait()

    ctl._sleep = blocked_sleep
    return ctl, link, clock


def test_rate_to_zero_while_sender_parked_sends_target():
    ctl, link, clock = make_parked(SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        await settle()  # sender is now parked waiting for the first ramp update
        clock.t = 105.0
        ctl.set_slew_rate(0)
        await until(lambda: ctl.state.confirmed_w == 250)

    run(scenario)
    assert link.sent == [(105.0, 250)]


def test_retarget_to_current_setpoint_while_parked_sends_target():
    ctl, link, clock = make_parked(SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        await settle()
        clock.t = 105.0  # setpoint 50 W
        ctl.request_target(50)
        assert ctl._ramp is None and ctl.pending
        await until(lambda: ctl.state.confirmed_w == 50)

    run(scenario)
    assert link.sent == [(105.0, 50)]


def test_frequent_retargets_still_update_every_interval():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()  # t=100, 0 W
        ctl.request_target(400)  # ramp from idle: update anchor 100
        clock.t = 104.0
        ctl.request_target(410)  # setpoint 40; anchor stays 100
        clock.t = 108.0
        ctl.request_target(420)  # setpoint 80; ramp 80 -> 420
        await until(lambda: link.sent)

    run(scenario)
    assert link.sent[0] == (110.0, 100)


def test_rejected_ramp_update_ramp_continues():
    ctl, link, clock = make(ftms={"slew_w_per_s": 10, "slew_update_interval_s": 2})

    async def scenario():
        await ctl.start()
        link.command_results = [CommandRejected(0x05, 0x04)]
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)

    run(scenario)
    assert link.sent == [(102.0, 20), (104.0, 40), (106.0, 60), (108.0, 80), (110.0, 100)]


def test_rejections_spread_across_a_ramp_do_not_exhaust_the_final_retry():
    ctl, link, clock = make(ftms={"slew_w_per_s": 10, "slew_update_interval_s": 2})
    rej = CommandRejected(0x05, 0x04)

    async def scenario():
        await ctl.start()
        link.command_results = [rej, None, rej, None, rej]  # updates 20, 60, and the final 100 rejected
        ctl.request_target(100)
        await until(lambda: ctl.state.confirmed_w == 100)

    run(scenario)
    assert watts(link)[-2:] == [100, 100]
    assert len(link.sent) == 6


def test_ramp_step_with_link_not_ready_drops_ramp():
    ctl, link, clock = make(ftms=SLEW)

    async def scenario():
        await ctl.start()
        ctl.request_target(250)
        ctl.state.link = "reconnecting"
        await ctl._ramp_step()

    run(scenario)
    assert ctl._ramp is None
    assert link.sent == []
