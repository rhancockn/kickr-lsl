"""Load and validate the JSON config (spec §5)."""
from __future__ import annotations

import json
import logging
import re
import sys
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .cps import MEASUREMENTS

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")
_TOP_KEYS = {"device", "measurements", "log", "reconnect", "queue_warn_depth"}


class ConfigError(ValueError):
    """Invalid or unreadable config. CLI exit code 2."""


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    mac: str | None = None  # used on Windows/Linux only
    uuid: str | None = None  # used on macOS only
    scan_timeout_s: float = 10.0


@dataclass(frozen=True)
class LogConfig:
    path: str = "logs/cps_{start:%Y%m%d_%H%M%S}.jsonl"
    flush_every_n: int = 10


@dataclass(frozen=True)
class ReconnectConfig:
    enabled: bool = True
    max_attempts: int = 0  # 0 = unlimited
    backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)


@dataclass(frozen=True)
class Config:
    device: DeviceConfig
    measurements: tuple[str, ...] = ()
    log: LogConfig = field(default_factory=LogConfig)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    queue_warn_depth: int = 100


def load_config(path: str | Path, *, require_measurements: bool = True) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from None
    return parse_config(obj, require_measurements=require_measurements)


def parse_config(obj: Any, *, require_measurements: bool = True) -> Config:
    if not isinstance(obj, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(obj) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")
    if "device" not in obj:
        raise ConfigError("missing required section 'device'")
    return Config(
        device=_parse_device(_section(obj, "device", {"name", "mac", "uuid", "scan_timeout_s"})),
        measurements=_parse_measurements(obj.get("measurements"), require_measurements),
        log=_parse_log(_section(obj, "log", {"path", "flush_every_n"})),
        reconnect=_parse_reconnect(_section(obj, "reconnect", {"enabled", "max_attempts", "backoff_s"})),
        queue_warn_depth=_int(obj.get("queue_warn_depth", 100), "queue_warn_depth", minimum=1),
    )


def address_kind(platform: str | None = None) -> str:
    """'uuid' on macOS (CoreBluetooth hides MAC addresses), 'mac' everywhere else."""
    return "uuid" if (platform or sys.platform) == "darwin" else "mac"


def select_identifier(device: DeviceConfig, platform: str | None = None) -> tuple[str, str]:
    """Pick how to find the device on this platform: ('mac'|'uuid', value) or ('name', name)."""
    kind = address_kind(platform)
    if kind == "uuid":
        value, other_kind, other = device.uuid, "mac", device.mac
    else:
        value, other_kind, other = device.mac, "uuid", device.uuid
    if other is not None:
        log.debug("ignoring device.%s=%s on platform %s", other_kind, other, platform or sys.platform)
    if value:
        return kind, value
    return "name", device.name


def _section(obj: dict, key: str, allowed: set[str]) -> dict:
    section = obj.get(key, {})
    if not isinstance(section, dict):
        raise ConfigError(f"'{key}' must be an object")
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in '{key}': {sorted(unknown)}")
    return section


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pos_number(value: Any, where: str) -> float:
    if not _is_number(value) or value <= 0:
        raise ConfigError(f"{where} must be a positive number, got {value!r}")
    return float(value)


def _int(value: Any, where: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}, got {value!r}")
    return value


def _opt_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string or null, got {value!r}")
    return value.strip()


def _parse_device(d: dict) -> DeviceConfig:
    name = d.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("device.name must be a non-empty string")
    mac = _opt_str(d.get("mac"), "device.mac")
    if mac is not None and not _MAC_RE.fullmatch(mac):
        raise ConfigError(f"device.mac must look like AA:BB:CC:DD:EE:FF, got {mac!r}")
    uuid = _opt_str(d.get("uuid"), "device.uuid")
    if uuid is not None:
        try:
            uuidlib.UUID(uuid)
        except ValueError:
            raise ConfigError(f"device.uuid is not a valid UUID: {uuid!r}") from None
    return DeviceConfig(
        name=name.strip(),
        mac=mac,
        uuid=uuid,
        scan_timeout_s=_pos_number(d.get("scan_timeout_s", 10), "device.scan_timeout_s"),
    )


def _parse_measurements(m: Any, required: bool) -> tuple[str, ...]:
    if m is None:
        if required:
            raise ConfigError("missing required 'measurements' list")
        return ()
    if not isinstance(m, list) or not m or not all(isinstance(x, str) for x in m):
        raise ConfigError("measurements must be a non-empty list of strings")
    unknown = [x for x in m if x not in MEASUREMENTS]
    if unknown:
        raise ConfigError(f"unknown measurement(s) {unknown}; valid names: {sorted(MEASUREMENTS)}")
    if len(set(m)) != len(m):
        raise ConfigError(f"duplicate measurement names in {m}")
    return tuple(m)


def _parse_log(lg: dict) -> LogConfig:
    path = lg.get("path", LogConfig.path)
    if not isinstance(path, str) or not path.strip():
        raise ConfigError("log.path must be a non-empty string")
    try:
        path.format(start=datetime.now())
    except (KeyError, IndexError, ValueError, AttributeError, TypeError) as e:
        raise ConfigError(f"log.path template is invalid ({e!r}); only {{start:<strftime>}} is supported") from None
    return LogConfig(path=path, flush_every_n=_int(lg.get("flush_every_n", 10), "log.flush_every_n", minimum=1))


def _parse_reconnect(rc: dict) -> ReconnectConfig:
    enabled = rc.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("reconnect.enabled must be true or false")
    backoff = rc.get("backoff_s", [1, 2, 5, 10])
    if not isinstance(backoff, list) or not backoff:
        raise ConfigError("reconnect.backoff_s must be a non-empty list of positive numbers")
    return ReconnectConfig(
        enabled=enabled,
        max_attempts=_int(rc.get("max_attempts", 0), "reconnect.max_attempts", minimum=0),
        backoff_s=tuple(_pos_number(b, "reconnect.backoff_s[]") for b in backoff),
    )
