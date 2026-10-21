import struct

import pytest

from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms import protocol as p
from wahoo_lsl.ftms.protocol import (
    BikeData,
    CpResponse,
    PowerRange,
    ProtocolError,
    StatusEvent,
    parse_bike_data,
    parse_cp_response,
    parse_feature,
    parse_power_range,
    parse_status,
)


def test_package_defaults():
    assert (DEFAULT_HOST, DEFAULT_PORT) == ("127.0.0.1", 47610)


def test_uuids_are_full_lowercase():
    assert p.FTMS_SERVICE_UUID == "00001826-0000-1000-8000-00805f9b34fb"
    assert p.CONTROL_POINT_UUID == "00002ad9-0000-1000-8000-00805f9b34fb"
    assert p.INDOOR_BIKE_DATA_UUID == "00002ad2-0000-1000-8000-00805f9b34fb"
    assert p.STATUS_UUID == "00002ada-0000-1000-8000-00805f9b34fb"
    assert p.FEATURE_UUID == "00002acc-0000-1000-8000-00805f9b34fb"
    assert p.POWER_RANGE_UUID == "00002ad8-0000-1000-8000-00805f9b34fb"


def test_encoders():
    assert p.request_control() == b"\x00"
    assert p.start_or_resume() == b"\x07"
    assert p.set_target_power(150) == b"\x05\x96\x00"
    assert p.set_target_power(0) == b"\x05\x00\x00"
    assert p.set_target_power(-1) == b"\x05\xff\xff"
    with pytest.raises(ValueError):
        p.set_target_power(40000)


def test_names():
    assert p.opcode_name(0x05) == "set_target_power"
    assert p.opcode_name(0x42) == "0x42"
    assert p.result_name(0x03) == "invalid parameter"
    assert p.result_name(0x05) == "control not permitted"
    assert p.result_name(0x77) == "0x77"


def test_feature_power_target_supported():
    f = parse_feature(struct.pack("<II", 0x5, 1 << 3))
    assert f == p.FtmsFeatures(machine=0x5, target=1 << 3, power_target_supported=True)
    assert parse_feature(struct.pack("<II", 0xFFFF, 0)).power_target_supported is False


def test_feature_short_payload_zero_padded():
    assert parse_feature(b"\x00\x00\x00\x00\x08").power_target_supported is True
    assert parse_feature(b"\x01").machine == 1


def test_feature_empty_is_error():
    with pytest.raises(ProtocolError):
        parse_feature(b"")


def test_cp_response():
    assert parse_cp_response(b"\x80\x05\x01") == CpResponse(0x05, 0x01)
    assert parse_cp_response(bytearray(b"\x80\x00\x05\x00")) == CpResponse(0x00, 0x05)
    with pytest.raises(ProtocolError):
        parse_cp_response(b"\x80\x05")
    with pytest.raises(ProtocolError):
        parse_cp_response(b"\x81\x05\x01")


def test_status():
    assert parse_status(b"\x08\x96\x00") == StatusEvent(0x08, "target power changed", 150)
    assert parse_status(b"\xff") == StatusEvent(0xFF, "control permission lost", None)
    assert parse_status(b"\x02\x01").name == "stopped or paused by user"
    assert parse_status(b"\x42") == StatusEvent(0x42, "0x42", None)
    with pytest.raises(ProtocolError):
        parse_status(b"\x08\x96")
    with pytest.raises(ProtocolError):
        parse_status(b"")


def test_bike_data_speed_cadence_power():
    # flags 0x0044: bit0 = 0 -> speed present; bit2 cadence; bit6 power
    data = struct.pack("<HHHh", 0x0044, 2550, 180, 150)
    assert parse_bike_data(data) == BikeData(power_w=150, cadence_rpm=90.0, speed_kmh=25.5)


def test_bike_data_more_data_bit_means_no_speed():
    assert parse_bike_data(struct.pack("<Hh", 0x0041, 200)) == BikeData(200, None, None)


def test_bike_data_without_power():
    assert parse_bike_data(struct.pack("<H", 0x0001)) == BikeData(None, None, None)


def test_bike_data_skips_distance_uint24():
    data = struct.pack("<H", 0x0051) + (1000).to_bytes(3, "little") + struct.pack("<h", 99)
    assert parse_bike_data(data).power_w == 99


def test_bike_data_skips_average_and_resistance_fields():
    # bits 0,1,3,5,6: no speed, avg speed, avg cadence, resistance, power
    data = struct.pack("<HHHhh", 0x006B, 100, 50, 7, 123)
    assert parse_bike_data(data) == BikeData(123, None, None)


def test_bike_data_negative_power():
    assert parse_bike_data(struct.pack("<Hh", 0x0041, -5)).power_w == -5


def test_bike_data_truncated_or_short_is_error():
    with pytest.raises(ProtocolError):
        parse_bike_data(struct.pack("<HH", 0x0044, 2550))
    with pytest.raises(ProtocolError):
        parse_bike_data(b"\x44")


def test_power_range():
    assert parse_power_range(struct.pack("<hhH", 0, 2000, 1)) == PowerRange(0, 2000, 1)
    assert p.DEFAULT_POWER_RANGE == PowerRange(0, 2000, 1)
    with pytest.raises(ProtocolError):
        parse_power_range(b"\x00\x00\xd0\x07\x01")
