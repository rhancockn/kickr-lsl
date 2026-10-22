import math

import pytest

from wahoo_lsl.derive import DEFAULT_CIRCUMFERENCE_M, CpsDeriver


def fields(wheel=None, crank=None, **extra):
    out = dict(extra)
    if wheel is not None:
        out["wheel_revs"], out["wheel_event_t"] = wheel
    if crank is not None:
        out["crank_revs"], out["crank_event_t"] = crank
    return out


def test_first_sample_primes_only():
    d = CpsDeriver()
    assert d.update(1.0, fields(wheel=(10, 0), crank=(5, 0))) == {"speed_kmh": None, "cadence_rpm": None}


def test_speed_and_cadence_from_deltas():
    d = CpsDeriver()
    d.update(1.0, fields(wheel=(10, 0), crank=(5, 0)))
    out = d.update(2.0, fields(wheel=(11, 2048), crank=(6, 1024)))
    assert out["speed_kmh"] == pytest.approx(DEFAULT_CIRCUMFERENCE_M * 3.6)  # 1 rev/s
    assert out["cadence_rpm"] == pytest.approx(60.0)


def test_custom_circumference():
    d = CpsDeriver(circumference_m=2.0)
    assert d.circumference_m == 2.0
    d.update(1.0, fields(wheel=(0, 0)))
    assert d.update(2.0, fields(wheel=(2, 2048)))["speed_kmh"] == pytest.approx(2 * 2.0 * 3.6)


def test_counter_and_event_time_wrap():
    d = CpsDeriver()
    d.update(1.0, fields(wheel=(2**32 - 1, 65000), crank=(65535, 65000)))
    out = d.update(2.0, fields(wheel=(1, 1000), crank=(0, 488)))
    # wheel: 2 revs in (1000 - 65000) % 65536 = 1536 ticks of 1/2048 s
    assert out["speed_kmh"] == pytest.approx(2 * 2048 / 1536 * DEFAULT_CIRCUMFERENCE_M * 3.6)
    # crank: 1 rev in (488 - 65000) % 65536 = 1024 ticks of 1/1024 s
    assert out["cadence_rpm"] == pytest.approx(60.0)


def test_no_new_revolution_holds_then_zeroes_after_3s():
    d = CpsDeriver()
    d.update(0.0, fields(crank=(0, 0)))
    assert d.update(1.0, fields(crank=(1, 1024)))["cadence_rpm"] == pytest.approx(60.0)
    assert d.update(2.0, fields(crank=(1, 1024)))["cadence_rpm"] == pytest.approx(60.0)  # held
    assert d.update(3.9, fields(crank=(1, 1024)))["cadence_rpm"] == pytest.approx(60.0)  # 2.9 s: held
    assert d.update(4.0, fields(crank=(1, 1024)))["cadence_rpm"] == 0.0  # 3 s since the last change


def test_stationary_from_start_reads_zero_after_3s():
    d = CpsDeriver()
    d.update(0.0, fields(wheel=(0, 0)))
    assert d.update(2.0, fields(wheel=(0, 0)))["speed_kmh"] is None
    assert d.update(3.0, fields(wheel=(0, 0)))["speed_kmh"] == 0.0


def test_revs_without_event_time_change_is_not_an_event():
    d = CpsDeriver()
    d.update(0.0, fields(crank=(0, 100)))
    assert d.update(1.0, fields(crank=(3, 100)))["cadence_rpm"] is None


def test_missing_fields_give_none_and_reprime():
    d = CpsDeriver()
    d.update(0.0, fields(wheel=(0, 0)))
    assert d.update(1.0, fields(power_w=100)) == {"speed_kmh": None, "cadence_rpm": None}
    assert d.update(2.0, fields(wheel=(5, 2048)))["speed_kmh"] is None  # re-primed, not a 5-rev delta


def test_non_int_fields_are_ignored():
    d = CpsDeriver()
    assert d.update(1.0, {"wheel_revs": 1.5, "wheel_event_t": 0, "crank_revs": True, "crank_event_t": 0}) == {
        "speed_kmh": None,
        "cadence_rpm": None,
    }


def test_reset_forgets_previous_counters():
    d = CpsDeriver()
    d.update(0.0, fields(wheel=(1000, 0)))
    d.reset()
    assert d.update(1.0, fields(wheel=(0, 2048)))["speed_kmh"] is None


@pytest.mark.parametrize("c", [0, -1, math.nan, math.inf, True])
def test_invalid_circumference_rejected(c):
    with pytest.raises(ValueError):
        CpsDeriver(circumference_m=c)
