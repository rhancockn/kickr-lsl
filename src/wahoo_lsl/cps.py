"""Cycling Power Service constants and pure parsers (spec §6).

No I/O, no bleak, no threading: everything here is testable with byte fixtures.
All multi-byte values are little-endian.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


CPS_SERVICE_UUID = _uuid16(0x1818)
CP_MEASUREMENT_UUID = _uuid16(0x2A63)
CP_FEATURE_UUID = _uuid16(0x2A65)
SENSOR_LOCATION_UUID = _uuid16(0x2A5D)


class ParseError(ValueError):
    """Payload could not be decoded. ``flags`` is set if the flags field was read."""

    def __init__(self, message: str, flags: int | None = None) -> None:
        super().__init__(message)
        self.flags = flags


@dataclass(frozen=True)
class MeasurementSpec:
    name: str
    keys: tuple[str, ...]  # output keys in Measurement.values
    flag_bits: tuple[int, ...]  # CP Measurement flag bits; () = always present
    feature_bit: int | None  # CP Feature bit; None = always supported


# Single source of truth for the config vocabulary (spec §6.3).
MEASUREMENTS: dict[str, MeasurementSpec] = {
    spec.name: spec
    for spec in (
        MeasurementSpec("power", ("power_w",), (), None),
        MeasurementSpec("pedal_power_balance", ("pedal_power_balance_pct", "pedal_power_balance_ref"), (0,), 0),
        MeasurementSpec("accumulated_torque", ("accumulated_torque_nm", "torque_source"), (2,), 1),
        MeasurementSpec("wheel_revolutions", ("wheel_revs", "wheel_event_t"), (4,), 2),
        MeasurementSpec("crank_revolutions", ("crank_revs", "crank_event_t"), (5,), 3),
        MeasurementSpec("extreme_force", ("max_force_n", "min_force_n"), (6,), 4),
        MeasurementSpec("extreme_torque", ("max_torque_nm", "min_torque_nm"), (7,), 4),
        MeasurementSpec("extreme_angles", ("max_angle_deg", "min_angle_deg"), (8,), 5),
        MeasurementSpec("dead_spot_angles", ("top_dead_spot_deg", "bottom_dead_spot_deg"), (9, 10), 6),
        MeasurementSpec("accumulated_energy", ("accumulated_energy_kj",), (11,), 7),
        MeasurementSpec("offset_compensation", ("offset_compensation",), (), 8),
    )
}


@dataclass(frozen=True)
class Measurement:
    flags: int
    values: dict[str, object]  # every output key decoded from this payload
    present: frozenset[str]  # measurement names present in this payload


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self.pos

    def read(self, fmt: str, what: str) -> tuple:
        size = struct.calcsize(fmt)
        if size > self.remaining:
            raise ParseError(
                f"truncated payload reading {what}: need {size} bytes at offset {self.pos}, have {self.remaining}"
            )
        values = struct.unpack_from(fmt, self._data, self.pos)
        self.pos += size
        return values


def parse_measurement(data: bytes | bytearray) -> Measurement:
    """Decode a CP Measurement (0x2A63) payload. Raises ParseError if truncated."""
    reader = _Reader(bytes(data))
    (flags,) = reader.read("<H", "flags")
    try:
        return _parse_body(reader, flags)
    except ParseError as e:
        raise ParseError(str(e), flags=flags) from None


def _parse_body(r: _Reader, flags: int) -> Measurement:
    def has(bit: int) -> bool:
        return bool((flags >> bit) & 1)

    (power,) = r.read("<h", "power")
    values: dict[str, object] = {"power_w": power}
    present = {"power", "offset_compensation"}

    if has(0):
        (balance,) = r.read("<B", "pedal_power_balance")
        values["pedal_power_balance_pct"] = balance / 2
        values["pedal_power_balance_ref"] = "left" if has(1) else "unknown"
        present.add("pedal_power_balance")
    if has(2):
        (torque,) = r.read("<H", "accumulated_torque")
        values["accumulated_torque_nm"] = torque / 32
        values["torque_source"] = "crank" if has(3) else "wheel"
        present.add("accumulated_torque")
    if has(4):
        revs, event_t = r.read("<IH", "wheel_revolutions")
        values["wheel_revs"] = revs
        values["wheel_event_t"] = event_t
        present.add("wheel_revolutions")
    if has(5):
        revs, event_t = r.read("<HH", "crank_revolutions")
        values["crank_revs"] = revs
        values["crank_event_t"] = event_t
        present.add("crank_revolutions")
    if has(6):
        f_max, f_min = r.read("<hh", "extreme_force")
        values["max_force_n"] = f_max
        values["min_force_n"] = f_min
        present.add("extreme_force")
    if has(7):
        t_max, t_min = r.read("<hh", "extreme_torque")
        values["max_torque_nm"] = t_max / 32
        values["min_torque_nm"] = t_min / 32
        present.add("extreme_torque")
    if has(8):
        b0, b1, b2 = r.read("<BBB", "extreme_angles")
        packed = b0 | (b1 << 8) | (b2 << 16)
        values["max_angle_deg"] = packed & 0xFFF
        values["min_angle_deg"] = packed >> 12
        present.add("extreme_angles")
    if has(9):
        (top,) = r.read("<H", "top_dead_spot_angle")
        values["top_dead_spot_deg"] = top
    if has(10):
        (bottom,) = r.read("<H", "bottom_dead_spot_angle")
        values["bottom_dead_spot_deg"] = bottom
    if has(9) or has(10):
        present.add("dead_spot_angles")
    if has(11):
        (energy,) = r.read("<H", "accumulated_energy")
        values["accumulated_energy_kj"] = energy
        present.add("accumulated_energy")
    values["offset_compensation"] = has(12)
    if r.remaining:
        values["_trailing_bytes"] = r.remaining
    return Measurement(flags, values, frozenset(present))
