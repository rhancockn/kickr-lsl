import math

import pytest

from wahoo_lsl.ftms.slew import MAX_SLEW_W_PER_S, Ramp


def test_max_slew_constant():
    assert MAX_SLEW_W_PER_S == 1000.0


def test_ramp_up_values_and_end():
    r = Ramp(100, 250, 10.0, 0.0, 10.0)
    assert r.end_t == 15.0
    assert r.value_at(-1.0) == 100.0
    assert r.value_at(0.0) == 100.0
    assert r.value_at(10.0) == 200.0
    assert r.value_at(15.0) == 250.0
    assert r.value_at(99.0) == 250.0


def test_ramp_down_values():
    r = Ramp(250, 100, 10.0, 0.0, 10.0)
    assert r.end_t == 15.0
    assert r.value_at(10.0) == 150.0
    assert r.value_at(20.0) == 100.0


def test_next_update_on_interval_grid_capped_at_end():
    r = Ramp(100, 250, 10.0, 0.0, 10.0)
    assert r.next_update_at(0.0) == 10.0
    assert r.next_update_at(3.0) == 10.0
    assert r.next_update_at(10.0) == 15.0
    assert r.next_update_at(15.0) == 15.0


def test_next_update_tolerates_float_error_at_grid_points():
    r = Ramp(0, 1000, 1.0, 0.1, 0.1)
    assert r.next_update_at(0.1 + 0.1) == pytest.approx(0.3)  # not 0.2 again


def test_ramp_shorter_than_interval_updates_once_at_end():
    r = Ramp(100, 130, 10.0, 5.0, 10.0)
    assert r.end_t == 8.0
    assert r.next_update_at(5.0) == 8.0


def test_fractional_start():
    r = Ramp(52.5, 100, 10.0, 0.0, 1.0)
    assert r.end_t == pytest.approx(4.75)
    assert r.value_at(1.0) == pytest.approx(62.5)


@pytest.mark.parametrize("rate, interval", [(0, 10), (-1, 10), (math.nan, 10), (math.inf, 10), (10, 0), (10, -1), (10, math.inf)])
def test_invalid_rate_or_interval(rate, interval):
    with pytest.raises(ValueError):
        Ramp(0, 100, rate, 0.0, interval)
