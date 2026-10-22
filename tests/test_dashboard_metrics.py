import math

import pytest

from wahoo_lsl.dashboard.metrics import (
    DEFAULT_CIRCUMFERENCE_M,
    CpsDeriver,
    CpsMetrics,
    History,
    SweepClock,
)


def sample(t, power=None, wheel=None, crank=None):
    fields = {}
    if power is not None:
        fields["power_w"] = power
    if wheel is not None:
        fields["wheel_revs"], fields["wheel_event_t"] = wheel
    if crank is not None:
        fields["crank_revs"], fields["crank_event_t"] = crank
    return {"type": "sample", "t_lsl": t, "fields": fields}


def test_first_sample_primes_only():
    d = CpsDeriver()
    assert d.update(sample(1.0, power=150, wheel=(10, 0), crank=(5, 0))) == CpsMetrics(1.0, 150, None, None)


def test_speed_and_cadence_from_deltas():
    d = CpsDeriver()
    d.update(sample(1.0, wheel=(10, 0), crank=(5, 0)))
    m = d.update(sample(2.0, power=200, wheel=(11, 2048), crank=(6, 1024)))
    assert m.power_w == 200
    assert m.speed_kmh == pytest.approx(DEFAULT_CIRCUMFERENCE_M * 3.6)  # 1 rev/s
    assert m.cadence_rpm == pytest.approx(60.0)


def test_custom_circumference():
    d = CpsDeriver(circumference_m=2.0)
    d.update(sample(1.0, wheel=(0, 0)))
    assert d.update(sample(2.0, wheel=(2, 2048))).speed_kmh == pytest.approx(2 * 2.0 * 3.6)


def test_counter_and_event_time_wrap():
    d = CpsDeriver()
    d.update(sample(1.0, wheel=(2**32 - 1, 65000), crank=(65535, 65000)))
    m = d.update(sample(2.0, wheel=(1, 1000), crank=(0, 488)))
    # wheel: 2 revs in (1000 - 65000) % 65536 = 1536 ticks of 1/2048 s
    assert m.speed_kmh == pytest.approx(2 * 2048 / 1536 * DEFAULT_CIRCUMFERENCE_M * 3.6)
    # crank: 1 rev in (488 - 65000) % 65536 = 1024 ticks of 1/1024 s
    assert m.cadence_rpm == pytest.approx(60.0)


def test_no_new_revolution_holds_then_zeroes_after_3s():
    d = CpsDeriver()
    d.update(sample(0.0, crank=(0, 0)))
    assert d.update(sample(1.0, crank=(1, 1024))).cadence_rpm == pytest.approx(60.0)
    assert d.update(sample(2.0, crank=(1, 1024))).cadence_rpm == pytest.approx(60.0)  # held
    assert d.update(sample(3.9, crank=(1, 1024))).cadence_rpm == pytest.approx(60.0)  # 2.9 s: held
    assert d.update(sample(4.0, crank=(1, 1024))).cadence_rpm == 0.0  # 3 s since the last change


def test_stationary_from_start_reads_zero_after_3s():
    d = CpsDeriver()
    d.update(sample(0.0, wheel=(0, 0)))
    assert d.update(sample(2.0, wheel=(0, 0))).speed_kmh is None
    assert d.update(sample(3.0, wheel=(0, 0))).speed_kmh == 0.0


def test_revs_without_event_time_change_is_not_an_event():
    d = CpsDeriver()
    d.update(sample(0.0, crank=(0, 100)))
    assert d.update(sample(1.0, crank=(3, 100))).cadence_rpm is None


def test_missing_fields_give_none_and_reprime():
    d = CpsDeriver()
    d.update(sample(0.0, wheel=(0, 0)))
    m = d.update(sample(1.0, power=100))
    assert (m.power_w, m.speed_kmh, m.cadence_rpm) == (100, None, None)
    assert d.update(sample(2.0, wheel=(5, 2048))).speed_kmh is None  # re-primed, not a 5-rev delta


@pytest.mark.parametrize(
    "bad",
    [{"type": "sample"}, {"t_lsl": "x", "fields": {}}, {"t_lsl": 1.0, "fields": []}, {"t_lsl": True, "fields": {}}],
)
def test_malformed_sample_returns_none(bad):
    assert CpsDeriver().update(bad) is None


def test_non_int_fields_are_ignored():
    d = CpsDeriver()
    m = d.update({"t_lsl": 1.0, "fields": {"power_w": True, "wheel_revs": 1.5, "wheel_event_t": 0}})
    assert (m.power_w, m.speed_kmh) == (None, None)


def test_reset_forgets_previous_counters():
    d = CpsDeriver()
    d.update(sample(0.0, wheel=(1000, 0)))
    d.reset()
    assert d.update(sample(1.0, wheel=(0, 2048))).speed_kmh is None


@pytest.mark.parametrize("c", [0, -1, math.nan, math.inf, True])
def test_invalid_circumference_rejected(c):
    with pytest.raises(ValueError):
        CpsDeriver(circumference_m=c)


def test_history_points_are_relative_to_cycle_start():
    h = History(SweepClock(100.0))
    h.add(100.0, 1)
    h.add(150.5, None)
    h.add(399.0, 3)
    assert h.points() == [(0.0, 1), (50.5, None), (299.0, 3)]


def test_history_clears_when_cycle_ends_and_skips_whole_cycles():
    clock = SweepClock(0.0)
    h = History(clock)
    h.add(10.0, 1)
    h.add(910.0, 2)  # cycles 1 and 2 had no data at all
    assert clock.cycle_start == 900.0 and clock.cycle == 3
    assert h.points() == [(10.0, 2)]


def test_history_drops_points_before_cycle_start():
    clock = SweepClock(0.0)
    h = History(clock)
    h.add(301.0, 1)
    h.add(299.0, 2)  # a late point from the previous cycle
    assert h.points() == [(1.0, 1)]


def test_shared_clock_clears_all_histories_together():
    clock = SweepClock(0.0)
    a, b = History(clock), History(clock)
    a.add(5.0, 1)
    b.add(6.0, 2)
    assert clock.advance(300.0) is True
    assert a.points() == [] and b.points() == []
    assert clock.advance(301.0) is False
