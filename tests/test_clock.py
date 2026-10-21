import time

import pylsl

from wahoo_lsl import clock


def test_now_lsl_is_monotonic_float():
    a = clock.now_lsl()
    b = clock.now_lsl()
    assert isinstance(a, float)
    assert b >= a


def test_now_lsl_is_pylsl_local_clock():
    before = pylsl.local_clock()
    t = clock.now_lsl()
    after = pylsl.local_clock()
    assert before <= t <= after


def test_now_wall_ns_is_system_realtime():
    before = time.time_ns()
    t = clock.now_wall_ns()
    after = time.time_ns()
    assert before <= t <= after


def test_wall_iso_epoch():
    assert clock.wall_iso(0) == "1970-01-01T00:00:00.000000+00:00"


def test_wall_iso_truncates_to_microseconds():
    assert clock.wall_iso(1_500_000_000 * 10**9 + 123_456_789) == "2017-07-14T02:40:00.123456+00:00"
