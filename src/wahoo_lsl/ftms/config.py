"""FTMS controller config (spec §6). A separate file from the logger config."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    ConfigError,
    DeviceConfig,
    ReconnectConfig,
    parse_device,
    parse_reconnect,
    pos_number,
    section,
)
from . import DEFAULT_HOST, DEFAULT_PORT
from .slew import MAX_SLEW_W_PER_S

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_TOP_KEYS = {"device", "reconnect", "ftms"}
_FTMS_KEYS = {"host", "port", "max_rate_hz", "ack_timeout_s", "max_watts", "slew_w_per_s", "slew_update_interval_s"}


@dataclass(frozen=True)
class FtmsSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    max_rate_hz: float = 2.0
    ack_timeout_s: float = 1.0
    max_watts: int | None = None
    slew_w_per_s: float = 10.0  # increases ramp at this rate; decreases and 0 = sent directly
    slew_update_interval_s: float = 10.0


@dataclass(frozen=True)
class FtmsConfig:
    device: DeviceConfig
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    ftms: FtmsSettings = field(default_factory=FtmsSettings)


def load_ftms_config(path: str | Path) -> FtmsConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from None
    return parse_ftms_config(obj)


def parse_ftms_config(obj: Any) -> FtmsConfig:
    if not isinstance(obj, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(obj) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")
    if "device" not in obj:
        raise ConfigError("missing required section 'device'")
    return FtmsConfig(
        device=parse_device(section(obj, "device", {"name", "mac", "uuid", "scan_timeout_s"})),
        reconnect=parse_reconnect(section(obj, "reconnect", {"enabled", "max_attempts", "backoff_s"})),
        ftms=_parse_ftms(section(obj, "ftms", _FTMS_KEYS)),
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_ftms(d: dict) -> FtmsSettings:
    host = d.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        raise ConfigError(f"ftms.host must be one of {sorted(LOOPBACK_HOSTS)} (localhost only), got {host!r}")
    port = d.get("port", DEFAULT_PORT)
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ConfigError(f"ftms.port must be an integer 1..65535, got {port!r}")
    max_watts = d.get("max_watts")
    if max_watts is not None and (not _is_int(max_watts) or max_watts <= 0):
        raise ConfigError(f"ftms.max_watts must be a positive integer or null, got {max_watts!r}")
    slew = d.get("slew_w_per_s", 10.0)
    if (
        isinstance(slew, bool)
        or not isinstance(slew, (int, float))
        or not 0 <= slew <= MAX_SLEW_W_PER_S
    ):
        raise ConfigError(f"ftms.slew_w_per_s must be a number 0..{MAX_SLEW_W_PER_S:g} (0 = no slew), got {slew!r}")
    interval = pos_number(d.get("slew_update_interval_s", 10.0), "ftms.slew_update_interval_s")
    if not math.isfinite(interval):
        raise ConfigError(f"ftms.slew_update_interval_s must be a finite positive number, got {interval!r}")
    return FtmsSettings(
        host=host,
        port=port,
        max_rate_hz=pos_number(d.get("max_rate_hz", 2), "ftms.max_rate_hz"),
        ack_timeout_s=pos_number(d.get("ack_timeout_s", 1.0), "ftms.ack_timeout_s"),
        max_watts=max_watts,
        slew_w_per_s=float(slew),
        slew_update_interval_s=interval,
    )
