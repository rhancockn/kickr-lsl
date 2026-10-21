"""BLE layer: the only module that imports bleak (spec §8)."""
from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import clock
from .config import Config, address_kind, select_identifier
from .cps import (
    CP_FEATURE_UUID,
    CP_MEASUREMENT_UUID,
    CPS_SERVICE_UUID,
    SENSOR_LOCATION_UUID,
    Features,
    ParseError,
    parse_features,
    sensor_location_name,
)
from .pipeline import Event, RawSample, make_event
from .warn import WarnOnce

log = logging.getLogger(__name__)

# Errors bleak or the OS may raise during BLE operations on any backend.
COMMS_ERRORS = (BleakError, asyncio.TimeoutError, OSError)


class BleSetupError(Exception):
    """Could not find, connect to, or read the device. CLI exit code 3."""


class DeviceNotFound(BleSetupError):
    pass


class AmbiguousDevice(BleSetupError):
    pass


class NoCpsService(BleSetupError):
    pass


class ReconnectExhausted(Exception):
    """Connection lost and could not be (or may not be) restored. CLI exit code 4."""


@dataclass(frozen=True)
class ScanResult:
    name: str | None
    address: str
    rssi: int | None
    cps_advertised: bool
    device: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class DeviceInfo:
    name: str | None
    address: str
    address_kind: str  # "mac" or "uuid"
    features: Features
    sensor_location: str | None


class ReconnectPolicy:
    def __init__(self, backoff_s: Sequence[float], max_attempts: int) -> None:
        if not backoff_s:
            raise ValueError("backoff_s must not be empty")
        self._backoff = tuple(backoff_s)
        self._max_attempts = max_attempts

    def delay(self, attempt: int) -> float:
        """Seconds to wait before 0-based reconnect attempt ``attempt``."""
        return self._backoff[min(attempt, len(self._backoff) - 1)]

    def exhausted(self, failures: int) -> bool:
        return self._max_attempts > 0 and failures >= self._max_attempts


class BleBackend(Protocol):
    async def discover(self, timeout: float) -> list[ScanResult]: ...

    async def find_by_address(self, address: str, timeout: float) -> Any | None: ...

    def make_client(self, device: Any, disconnected_callback: Callable[[Any], None]) -> Any: ...


class BleakBackend:
    """Production backend (bleak on macOS, Windows and Linux)."""

    async def discover(self, timeout: float) -> list[ScanResult]:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results = []
        for device, adv in found.values():
            uuids = {u.lower() for u in (adv.service_uuids or ())}
            results.append(
                ScanResult(
                    name=adv.local_name or device.name,
                    address=device.address,
                    rssi=adv.rssi,
                    cps_advertised=CPS_SERVICE_UUID in uuids,
                    device=device,
                )
            )
        return results

    async def find_by_address(self, address: str, timeout: float) -> Any | None:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    def make_client(self, device: Any, disconnected_callback: Callable[[Any], None]) -> Any:
        return BleakClient(device, disconnected_callback=disconnected_callback)


async def scan_cps(timeout: float, backend: BleBackend | None = None) -> list[ScanResult]:
    """Devices advertising the CPS service, or whose name contains 'KICKR'."""
    results = await (backend or BleakBackend()).discover(timeout)
    return [r for r in results if r.cps_advertised or (r.name and "kickr" in r.name.lower())]


class CpsClient:
    """Finds, connects to and streams from one CPS device, reconnecting per config."""

    def __init__(
        self,
        cfg: Config,
        emit: Callable[[RawSample | Event], None],
        warn: WarnOnce,
        *,
        backend: BleBackend | None = None,
        platform: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._emit = emit
        self._warn = warn
        self._backend = backend or BleakBackend()
        self._platform = platform or sys.platform
        self._client: Any = None  # current client; None means "ignore disconnect callbacks"
        self._info: DeviceInfo | None = None
        self._measurement_char: Any = None
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._disconnected: asyncio.Event | None = None

    # ---- discovery & setup -------------------------------------------------

    async def find_device(self) -> Any:
        kind, value = select_identifier(self._cfg.device, self._platform)
        timeout = self._cfg.device.scan_timeout_s
        if kind != "name":
            device = await self._backend.find_by_address(value, timeout)
            if device is None:
                raise DeviceNotFound(f"no device with {kind} {value} found within {timeout:g}s")
            return device
        needle = value.lower()
        matches = [r for r in await self._backend.discover(timeout) if r.name and needle in r.name.lower()]
        if not matches:
            raise DeviceNotFound(f"no device with name containing '{value}' found within {timeout:g}s")
        candidates = [r for r in matches if r.cps_advertised] or matches
        if len(candidates) > 1:
            lines = "\n".join(f"  {r.name}  {r.address}  rssi={r.rssi}" for r in candidates)
            raise AmbiguousDevice(
                f"{len(candidates)} devices match name '{value}'; set device.{address_kind(self._platform)} "
                f"in the config to choose one:\n{lines}"
            )
        return candidates[0].device

    async def connect_and_read(self) -> DeviceInfo:
        """Connect and read CP Feature / Sensor Location. Leaves the client connected."""
        device = await self.find_device()
        client = self._backend.make_client(device, self._on_disconnect)
        self._client = client
        try:
            await client.connect()
            service = client.services.get_service(CPS_SERVICE_UUID)
            if service is None:
                raise NoCpsService(f"{getattr(device, 'name', device)} does not expose the Cycling Power Service")
            # Resolve on the service, not the collection: other services (e.g. CSC) may repeat a UUID.
            feature_char = service.get_characteristic(CP_FEATURE_UUID)
            if feature_char is None:
                raise NoCpsService("Cycling Power Service has no CP Feature characteristic")
            measurement_char = service.get_characteristic(CP_MEASUREMENT_UUID)
            if measurement_char is None:
                raise NoCpsService("Cycling Power Service has no CP Measurement characteristic")
            self._measurement_char = measurement_char
            features = parse_features(await client.read_gatt_char(feature_char))
            location = None
            location_char = service.get_characteristic(SENSOR_LOCATION_UUID)
            if location_char is not None:
                loc_raw = await client.read_gatt_char(location_char)
                location = sensor_location_name(loc_raw[0]) if loc_raw else None
        except BaseException:
            await self._safe_disconnect()
            raise
        info = DeviceInfo(
            name=getattr(device, "name", None),
            address=getattr(device, "address", str(device)),
            address_kind=address_kind(self._platform),
            features=features,
            sensor_location=location,
        )
        self._info = info
        return info

    async def read_info(self) -> DeviceInfo:
        """Connect, read device info, disconnect. Used by `list-features`."""
        try:
            return await self.connect_and_read()
        except (ParseError, *COMMS_ERRORS) as e:
            raise BleSetupError(f"BLE setup failed: {e}") from e
        finally:
            await self._safe_disconnect()

    async def _open_session(self) -> DeviceInfo:
        """Connect, report features, subscribe. Any failure -> BleSetupError, nothing left connected."""
        try:
            info = await self.connect_and_read()
            self._emit(make_event("connected", name=info.name, address=info.address, address_kind=info.address_kind))
            self._report_features(info)  # before subscribing, so it precedes all samples
            await self._client.start_notify(self._measurement_char, self._on_notify)
        except BleSetupError:
            raise
        except (ParseError, *COMMS_ERRORS) as e:
            await self._safe_disconnect()
            raise BleSetupError(f"BLE setup failed: {e}") from e
        return info

    def _report_features(self, info: DeviceInfo) -> None:
        unavailable = [m for m in self._cfg.measurements if m not in info.features.supported]
        for name in unavailable:
            self._warn.warn(
                ("unavailable", name),
                "Measurement '%s' is not supported by %s (CP Feature 0x%08x); continuing without it",
                name,
                info.name,
                info.features.raw,
            )
        self._emit(
            make_event(
                "features",
                raw=info.features.raw,
                supported=sorted(info.features.supported),
                unavailable=unavailable,
                sensor_location=info.sensor_location,
            )
        )

    # ---- streaming ---------------------------------------------------------

    async def run(self, stop_requested: asyncio.Event) -> None:
        """Stream until ``stop_requested`` is set.

        Raises BleSetupError if the first connection fails and ReconnectExhausted if the
        link is lost and cannot be restored. Always disconnects before returning.
        """
        self._loop = asyncio.get_running_loop()
        self._disconnected = asyncio.Event()
        policy = ReconnectPolicy(self._cfg.reconnect.backoff_s, self._cfg.reconnect.max_attempts)
        try:
            info = await self._open_session()
            log.info("Streaming from %s (%s %s)", info.name, info.address_kind, info.address)
            while True:
                await _wait_first(stop_requested, self._disconnected)
                if stop_requested.is_set():
                    return
                if not self._cfg.reconnect.enabled:
                    raise ReconnectExhausted("connection lost and reconnect is disabled")
                await self._reconnect(policy, stop_requested)
        finally:
            await self._shutdown()

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        t_lsl = clock.now_lsl()  # FIRST statement: minimal timing error
        t_wall_ns = clock.now_wall_ns()  # SECOND statement
        seq = self._seq
        self._seq += 1
        self._emit(RawSample(seq, t_lsl, t_wall_ns, bytes(data)))

    def _on_disconnect(self, client: Any) -> None:
        if client is not self._client:
            return  # intentional disconnect (we cleared _client first) or a stale client
        info = self._info
        log.warning("Connection to %s lost", info.name if info else "device")
        self._emit(
            make_event(
                "disconnected",
                name=info.name if info else None,
                address=info.address if info else None,
            )
        )
        if self._loop is not None and self._disconnected is not None:
            self._loop.call_soon_threadsafe(self._disconnected.set)

    async def _reconnect(self, policy: ReconnectPolicy, stop_requested: asyncio.Event) -> None:
        await self._safe_disconnect()  # release the dead client
        failures = 0
        while True:
            delay = policy.delay(failures)
            if await _sleep_unless(stop_requested, delay):
                return
            attempt = failures + 1
            log.warning("Reconnect attempt %d (after %.1fs)", attempt, delay)
            self._emit(make_event("reconnect_attempt", attempt=attempt, delay_s=delay))
            assert self._disconnected is not None
            self._disconnected.clear()
            try:
                info = await self._open_session()
            except BleSetupError as e:
                failures += 1
                log.warning("Reconnect attempt %d failed: %s", attempt, e)
                if policy.exhausted(failures):
                    raise ReconnectExhausted(f"gave up after {failures} failed reconnect attempt(s)") from e
                continue
            log.info("Reconnected to %s", info.name)
            self._emit(make_event("reconnected", attempt=attempt, name=info.name, address=info.address))
            return

    async def _shutdown(self) -> None:
        client = self._client
        if client is not None and getattr(client, "is_connected", False):
            try:
                await client.stop_notify(self._measurement_char)
            except Exception as e:
                log.debug("stop_notify failed during shutdown: %s", e)
        await self._safe_disconnect()

    async def _safe_disconnect(self) -> None:
        client, self._client = self._client, None  # clear first so the callback ignores this disconnect
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("disconnect failed: %s", e)


async def _wait_first(*events: asyncio.Event) -> None:
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _sleep_unless(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``. Return True if ``stop`` is (or becomes) set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
