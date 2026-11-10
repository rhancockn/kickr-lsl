"""LSL outlet sink: the regular CPS stream.

Runs on its own SinkRunner thread; each drained queue batch becomes one push_chunk.
The outlet is created when the device's features are known and kept across BLE
reconnects to the same device, so LSL resolvers see a gap rather than a new stream.
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pylsl

from .. import derive
from ..config import Config
from ..cps import DERIVED_KEYS, FIELD_INFO, MEASUREMENTS
from ..lsl import Channel, add_dict, sanitize, stream_info
from ..pipeline import Event, Sample, Sink

log = logging.getLogger(__name__)

STREAM_TYPE = "CPS"
_HEX = re.compile(r"[^0-9A-Fa-f]")
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")


def _model(name: str | None, device_info: dict) -> str:
    model = sanitize(device_info.get("model_number") or "")
    if model:
        return model
    words = (name or "").split()
    if words and words[0].lower() == "wahoo":
        words = words[1:]
    if words and _HEX4.fullmatch(words[-1]):
        words = words[:-1]
    return sanitize("".join(words)) or "DEVICE"


def stream_id(name: str | None, address: str, device_info: dict) -> str:
    """`<MODEL>-CPS-<last 6 hex digits of the MAC/UUID>`, used as both stream name and source_id."""
    return f"{_model(name, device_info)}-{STREAM_TYPE}-{_HEX.sub('', address)[-6:].lower()}"


def channel_keys(measurements: Sequence[str], supported: Iterable[str]) -> list[str]:
    """Configured + supported measurement keys in config order, then derived metrics the device can feed."""
    supported = set(supported)
    keys = [k for m in measurements if m in supported for k in MEASUREMENTS[m].keys]
    if "wheel_revolutions" in supported:
        keys.append("speed_kmh")
    if "crank_revolutions" in supported:
        keys.append("cadence_rpm")
    return keys


def _channel(key: str) -> Channel:
    info = FIELD_INFO[key]
    description = info.description
    if info.encoding:
        codes = ", ".join(f"{str(k).lower()}={v:g}" for k, v in info.encoding.items())
        description = f"{description} ({codes})"
    return Channel(key, info.unit, "derived" if key in DERIVED_KEYS else "measured", description)


def _row(sample: Sample, keys: Sequence[str]) -> list[float]:
    values = {**sample.fields, **sample.derived}
    row = []
    for key in keys:
        value = values.get(key)
        encoding = FIELD_INFO[key].encoding
        if value is None:
            row.append(math.nan)
        elif encoding is not None:
            row.append(encoding.get(value, math.nan))
        else:
            row.append(float(value))
    return row


class LslSink(Sink):
    name = "lsl"
    batched = True

    def __init__(self, cfg: Config, outlet_factory: Callable[[pylsl.StreamInfo], Any] | None = None) -> None:
        self._cfg = cfg
        self._factory = outlet_factory or pylsl.StreamOutlet
        self._connected: dict = {}
        self._models: dict[str, str] = {}  # address -> MODEL resolved on first sight, so source_id is stable
        self._outlet: Any = None
        self._identity: tuple[str, tuple[str, ...]] | None = None  # (source_id, channel keys)
        self._keys: list[str] = []
        self._rows: list[list[float]] = []
        self._stamps: list[float] = []

    def handle(self, item: Sample | Event) -> None:
        self.handle_batch([item])

    def handle_batch(self, items: list[Sample | Event]) -> None:
        # Every item is processed even if one fails, so the outlet identity always
        # follows the device; the first error is re-raised for the SinkRunner to log.
        first: Exception | None = None
        for item in items:
            try:
                self._handle_item(item)
            except Exception as exc:
                first = first or exc
        try:
            self._flush()
        except Exception as exc:
            first = first or exc
        if first is not None:
            raise first

    def _handle_item(self, item: Sample | Event) -> None:
        if isinstance(item, Sample):
            if self._outlet is not None and item.parse_error is None:
                self._rows.append(_row(item, self._keys))
                self._stamps.append(item.t_lsl)
        elif item.kind == "connected":
            self._connected = item.detail
        elif item.kind == "features":
            try:
                self._flush()
            finally:
                self._open(item.detail)

    def stop(self) -> None:
        try:
            self._flush()
        finally:
            self._outlet = None
            self._identity = None

    def _flush(self) -> None:
        if not self._rows:
            return
        rows, stamps = self._rows, self._stamps
        self._rows, self._stamps = [], []  # never re-sent, even if the push fails
        self._outlet.push_chunk(rows, stamps)

    def _open(self, features: dict) -> None:
        c = self._connected
        supported = sorted(features.get("supported", ()))
        keys = channel_keys(self._cfg.measurements, supported)
        address = c.get("address") or ""
        if address not in self._models:
            self._models[address] = _model(c.get("name"), c.get("device_info") or {})
        source_id = f"{self._models[address]}-{STREAM_TYPE}-{_HEX.sub('', address)[-6:].lower()}"
        identity = (source_id, tuple(keys))
        if self._outlet is not None and identity == self._identity:
            return  # same device, same layout: keep the stream alive across the reconnect
        if self._outlet is not None:
            log.info("Replacing LSL outlet %s", self._identity[0])
        self._outlet, self._identity, self._keys = None, None, []
        try:
            info = self._stream_info(source_id, keys, features, supported)
            self._outlet = self._factory(info)
        except Exception:
            log.exception("Cannot create LSL outlet %s; LSL samples dropped until the device reconnects", source_id)
            return
        self._identity, self._keys = identity, keys
        log.info("LSL outlet %s: %d channels (%s)", source_id, len(keys), ", ".join(keys))

    def _stream_info(self, source_id: str, keys: list[str], features: dict, supported: list[str]) -> pylsl.StreamInfo:
        c = self._cfg
        info = stream_info(
            source_id,
            STREAM_TYPE,
            source_id,
            [_channel(k) for k in keys],
            c.lsl.nominal_srate,
            acquisition={
                "clock": "pylsl.local_clock at BLE notify entry",
                "timestamps": "notification arrival time (BLE delivery), not device measurement time",
                "nominal_srate": c.lsl.nominal_srate,
            },
        )
        desc = info.desc()
        conn = self._connected
        add_dict(
            desc,
            "device",
            {
                "name": conn.get("name"),
                "address": conn.get("address"),
                "address_kind": conn.get("address_kind"),
                "sensor_location": features.get("sensor_location"),
                "features_raw": features.get("raw"),
                "supported": ",".join(supported),
                **(conn.get("device_info") or {}),
            },
        )
        add_dict(
            desc,
            "derivation",
            {
                "wheel_circumference_m": c.derive.wheel_circumference_m,
                "zero_after_s": derive.ZERO_AFTER_S,
                "method": "delta revolutions / delta event time between successive notifications",
                "wheel_ticks_per_s": derive.WHEEL_TICKS_PER_S,
                "crank_ticks_per_s": derive.CRANK_TICKS_PER_S,
                "wheel_rev_wrap": derive.WHEEL_REV_WRAP,
                "crank_rev_wrap": derive.CRANK_REV_WRAP,
                "event_time_wrap": derive.EVENT_T_WRAP,
            },
        )
        return info
