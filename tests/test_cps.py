import struct

import pytest

from wahoo_lsl.cps import MEASUREMENTS, ParseError, parse_measurement


def payload(flags: int, power: int, *parts: bytes) -> bytes:
    return struct.pack("<Hh", flags, power) + b"".join(parts)


def test_power_only():
    m = parse_measurement(payload(0, 250))
    assert m.flags == 0
    assert m.values == {"power_w": 250, "offset_compensation": False}
    assert m.present == {"power", "offset_compensation"}


def test_negative_power():
    assert parse_measurement(payload(0, -5)).values["power_w"] == -5


def test_accepts_bytearray():
    assert parse_measurement(bytearray(payload(0, 1))).values["power_w"] == 1


def test_pedal_power_balance_left_reference():
    m = parse_measurement(payload(0b11, 100, bytes([101])))
    assert m.values["pedal_power_balance_pct"] == 50.5
    assert m.values["pedal_power_balance_ref"] == "left"
    assert "pedal_power_balance" in m.present


def test_pedal_power_balance_unknown_reference():
    m = parse_measurement(payload(0b01, 100, bytes([100])))
    assert m.values["pedal_power_balance_ref"] == "unknown"


def test_accumulated_torque_crank_source():
    m = parse_measurement(payload(0b1100, 0, struct.pack("<H", 320)))
    assert m.values["accumulated_torque_nm"] == 10.0
    assert m.values["torque_source"] == "crank"
    assert "accumulated_torque" in m.present


def test_accumulated_torque_wheel_source():
    m = parse_measurement(payload(0b0100, 0, struct.pack("<H", 32)))
    assert m.values["accumulated_torque_nm"] == 1.0
    assert m.values["torque_source"] == "wheel"


def test_wheel_revolutions():
    m = parse_measurement(payload(1 << 4, 0, struct.pack("<IH", 100_000, 40_321)))
    assert m.values["wheel_revs"] == 100_000
    assert m.values["wheel_event_t"] == 40_321
    assert "wheel_revolutions" in m.present


def test_crank_revolutions():
    m = parse_measurement(payload(1 << 5, 0, struct.pack("<HH", 77, 1024)))
    assert m.values["crank_revs"] == 77
    assert m.values["crank_event_t"] == 1024
    assert "crank_revolutions" in m.present


def test_extreme_force():
    m = parse_measurement(payload(1 << 6, 0, struct.pack("<hh", 500, -20)))
    assert (m.values["max_force_n"], m.values["min_force_n"]) == (500, -20)
    assert "extreme_force" in m.present


def test_extreme_torque():
    m = parse_measurement(payload(1 << 7, 0, struct.pack("<hh", 64, -32)))
    assert (m.values["max_torque_nm"], m.values["min_torque_nm"]) == (2.0, -1.0)
    assert "extreme_torque" in m.present


def test_extreme_angles_bit_packing():
    packed = 270 | (90 << 12)
    m = parse_measurement(payload(1 << 8, 0, packed.to_bytes(3, "little")))
    assert (m.values["max_angle_deg"], m.values["min_angle_deg"]) == (270, 90)
    assert "extreme_angles" in m.present


def test_dead_spot_angles_both():
    m = parse_measurement(payload((1 << 9) | (1 << 10), 0, struct.pack("<HH", 10, 190)))
    assert m.values["top_dead_spot_deg"] == 10
    assert m.values["bottom_dead_spot_deg"] == 190
    assert "dead_spot_angles" in m.present


def test_dead_spot_angles_top_only():
    m = parse_measurement(payload(1 << 9, 0, struct.pack("<H", 10)))
    assert m.values["top_dead_spot_deg"] == 10
    assert "bottom_dead_spot_deg" not in m.values
    assert "dead_spot_angles" in m.present


def test_accumulated_energy():
    m = parse_measurement(payload(1 << 11, 0, struct.pack("<H", 1234)))
    assert m.values["accumulated_energy_kj"] == 1234
    assert "accumulated_energy" in m.present


def test_offset_compensation_indicator():
    assert parse_measurement(payload(1 << 12, 0)).values["offset_compensation"] is True


def test_combined_fields_follow_spec_order():
    flags = (1 << 2) | (1 << 4) | (1 << 5) | (1 << 11)
    data = payload(
        flags,
        300,
        struct.pack("<H", 64),
        struct.pack("<IH", 5, 2048),
        struct.pack("<HH", 3, 1024),
        struct.pack("<H", 7),
    )
    m = parse_measurement(data)
    assert m.values == {
        "power_w": 300,
        "accumulated_torque_nm": 2.0,
        "torque_source": "wheel",
        "wheel_revs": 5,
        "wheel_event_t": 2048,
        "crank_revs": 3,
        "crank_event_t": 1024,
        "accumulated_energy_kj": 7,
        "offset_compensation": False,
    }
    assert m.present == {
        "power",
        "accumulated_torque",
        "wheel_revolutions",
        "crank_revolutions",
        "accumulated_energy",
        "offset_compensation",
    }


@pytest.mark.parametrize(
    "bit, name",
    [
        (0, "pedal_power_balance"),
        (2, "accumulated_torque"),
        (4, "wheel_revolutions"),
        (5, "crank_revolutions"),
        (6, "extreme_force"),
        (7, "extreme_torque"),
        (8, "extreme_angles"),
        (9, "top_dead_spot_angle"),
        (10, "bottom_dead_spot_angle"),
        (11, "accumulated_energy"),
    ],
)
def test_truncated_optional_field_names_field_and_keeps_flags(bit, name):
    with pytest.raises(ParseError) as exc:
        parse_measurement(payload(1 << bit, 0))
    assert name in str(exc.value)
    assert exc.value.flags == 1 << bit


def test_empty_payload():
    with pytest.raises(ParseError) as exc:
        parse_measurement(b"")
    assert "flags" in str(exc.value)
    assert exc.value.flags is None


def test_missing_power():
    with pytest.raises(ParseError) as exc:
        parse_measurement(struct.pack("<H", 0))
    assert "power" in str(exc.value)
    assert exc.value.flags == 0


def test_trailing_bytes_reported_not_error():
    m = parse_measurement(payload(0, 1, b"\xaa\xbb"))
    assert m.values["_trailing_bytes"] == 2


def test_vocabulary_names_and_keys_unique():
    assert set(MEASUREMENTS) == {
        "power",
        "pedal_power_balance",
        "accumulated_torque",
        "wheel_revolutions",
        "crank_revolutions",
        "extreme_force",
        "extreme_torque",
        "extreme_angles",
        "dead_spot_angles",
        "accumulated_energy",
        "offset_compensation",
    }
    keys = [k for spec in MEASUREMENTS.values() for k in spec.keys]
    assert len(keys) == len(set(keys))


def test_vocabulary_keys_match_parser_output():
    data = payload(
        0x1FFF,
        1,
        bytes([1]),
        struct.pack("<H", 1),
        struct.pack("<IH", 1, 1),
        struct.pack("<HH", 1, 1),
        struct.pack("<hh", 1, 1),
        struct.pack("<hh", 1, 1),
        (1).to_bytes(3, "little"),
        struct.pack("<H", 1),
        struct.pack("<H", 1),
        struct.pack("<H", 1),
    )
    m = parse_measurement(data)
    assert set(m.values) == {k for spec in MEASUREMENTS.values() for k in spec.keys}
    assert m.present == set(MEASUREMENTS)
