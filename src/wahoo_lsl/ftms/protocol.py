"""Pure FTMS codec: UUIDs, control-point encoders, decoders. No I/O."""
from __future__ import annotations

import struct
from dataclasses import dataclass


def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


FTMS_SERVICE_UUID = _uuid16(0x1826)
FEATURE_UUID = _uuid16(0x2ACC)
INDOOR_BIKE_DATA_UUID = _uuid16(0x2AD2)
POWER_RANGE_UUID = _uuid16(0x2AD8)
CONTROL_POINT_UUID = _uuid16(0x2AD9)
STATUS_UUID = _uuid16(0x2ADA)


class ProtocolError(ValueError):
    """A payload from the device could not be decoded."""


OP_REQUEST_CONTROL = 0x00
OP_SET_TARGET_POWER = 0x05
OP_START_OR_RESUME = 0x07
OP_RESPONSE = 0x80

OPCODE_NAMES = {
    0x00: "request_control",
    0x01: "reset",
    0x05: "set_target_power",
    0x07: "start_or_resume",
    0x08: "stop_or_pause",
}

RESULT_SUCCESS = 0x01
RESULT_NAMES = {
    0x01: "success",
    0x02: "op code not supported",
    0x03: "invalid parameter",
    0x04: "operation failed",
    0x05: "control not permitted",
}

STATUS_TARGET_POWER_CHANGED = 0x08
STATUS_CONTROL_PERMISSION_LOST = 0xFF
STATUS_NAMES = {
    0x01: "reset",
    0x02: "stopped or paused by user",
    0x04: "started or resumed by user",
    0x08: "target power changed",
    0xFF: "control permission lost",
}

POWER_TARGET_SETTING_BIT = 3  # in target_setting_features


def opcode_name(op: int) -> str:
    return OPCODE_NAMES.get(op, f"0x{op:02X}")


def result_name(code: int) -> str:
    return RESULT_NAMES.get(code, f"0x{code:02X}")


@dataclass(frozen=True)
class FtmsFeatures:
    machine: int
    target: int
    power_target_supported: bool


@dataclass(frozen=True)
class CpResponse:
    request_opcode: int
    result: int


@dataclass(frozen=True)
class StatusEvent:
    opcode: int
    name: str
    target_power_w: int | None


@dataclass(frozen=True)
class BikeData:
    power_w: int | None
    cadence_rpm: float | None
    speed_kmh: float | None


@dataclass(frozen=True)
class PowerRange:
    min_w: int
    max_w: int
    step_w: int


DEFAULT_POWER_RANGE = PowerRange(0, 2000, 1)


# ---- encoders --------------------------------------------------------------


def request_control() -> bytes:
    return bytes([OP_REQUEST_CONTROL])


def start_or_resume() -> bytes:
    return bytes([OP_START_OR_RESUME])


def set_target_power(watts: int) -> bytes:
    if not -32768 <= watts <= 32767:
        raise ValueError(f"target power {watts} W does not fit in sint16")
    return struct.pack("<Bh", OP_SET_TARGET_POWER, watts)


# ---- decoders --------------------------------------------------------------


def parse_feature(data: bytes | bytearray) -> FtmsFeatures:
    """Two uint32 (machine, target-setting). Short payloads are zero-padded; empty is an error."""
    data = bytes(data)
    if not data:
        raise ProtocolError("Fitness Machine Feature is empty")
    machine, target = struct.unpack("<II", data[:8].ljust(8, b"\x00"))
    return FtmsFeatures(machine, target, bool((target >> POWER_TARGET_SETTING_BIT) & 1))


def parse_cp_response(data: bytes | bytearray) -> CpResponse:
    data = bytes(data)
    if len(data) < 3:
        raise ProtocolError(f"control point response needs 3 bytes, got {len(data)}")
    if data[0] != OP_RESPONSE:
        raise ProtocolError(f"control point response must start with 0x80, got 0x{data[0]:02X}")
    return CpResponse(data[1], data[2])


def parse_status(data: bytes | bytearray) -> StatusEvent:
    data = bytes(data)
    if not data:
        raise ProtocolError("Fitness Machine Status is empty")
    op = data[0]
    target = None
    if op == STATUS_TARGET_POWER_CHANGED:
        if len(data) < 3:
            raise ProtocolError("target power changed status needs 3 bytes")
        (target,) = struct.unpack_from("<h", data, 1)
    return StatusEvent(op, STATUS_NAMES.get(op, f"0x{op:02X}"), target)


def parse_bike_data(data: bytes | bytearray) -> BikeData:
    """Walk Indoor Bike Data fields in spec order up to instantaneous power."""
    data = bytes(data)
    if len(data) < 2:
        raise ProtocolError(f"Indoor Bike Data needs at least 2 bytes, got {len(data)}")
    (flags,) = struct.unpack_from("<H", data, 0)
    offset = 2

    def take(fmt: str, what: str) -> int:
        nonlocal offset
        size = struct.calcsize(fmt)
        if offset + size > len(data):
            raise ProtocolError(f"Indoor Bike Data truncated at {what} (flags 0x{flags:04X})")
        (value,) = struct.unpack_from(fmt, data, offset)
        offset += size
        return value

    speed = cadence = power = None
    if not flags & 0x0001:  # bit 0 "More Data" is inverted: 0 means speed present
        speed = take("<H", "instantaneous speed") / 100
    if flags & 0x0002:
        take("<H", "average speed")
    if flags & 0x0004:
        cadence = take("<H", "instantaneous cadence") / 2
    if flags & 0x0008:
        take("<H", "average cadence")
    if flags & 0x0010:
        take("<H", "total distance")
        take("<B", "total distance")  # uint24 = uint16 + uint8
    if flags & 0x0020:
        take("<h", "resistance level")
    if flags & 0x0040:
        power = take("<h", "instantaneous power")
    return BikeData(power, cadence, speed)


def parse_power_range(data: bytes | bytearray) -> PowerRange:
    data = bytes(data)
    if len(data) < 6:
        raise ProtocolError(f"Supported Power Range needs 6 bytes, got {len(data)}")
    min_w, max_w, step_w = struct.unpack_from("<hhH", data, 0)
    return PowerRange(min_w, max_w, step_w)
