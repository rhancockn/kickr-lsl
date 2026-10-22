"""In-memory stand-ins for bleak, used by BLE and CLI tests."""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

from bleak.exc import BleakError

from wahoo_lsl.ble import ScanResult
from wahoo_lsl.cps import (
    CP_FEATURE_UUID,
    CP_MEASUREMENT_UUID,
    CPS_SERVICE_UUID,
    DEVICE_INFORMATION_SERVICE_UUID,
    SENSOR_LOCATION_UUID,
)

KICKR = SimpleNamespace(name="KICKR CORE 1234", address="AA:BB:CC:DD:EE:01")
WHEEL_AND_CRANK = struct.pack("<I", 0b1100)
ALWAYS = 10**9


class FakeService:
    def get_characteristic(self, uuid: str):
        if uuid in (CP_FEATURE_UUID, CP_MEASUREMENT_UUID, SENSOR_LOCATION_UUID):
            return SimpleNamespace(uuid=uuid)
        return None


class FakeDisService:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get_characteristic(self, uuid: str):
        return SimpleNamespace(uuid=uuid) if uuid in self._values else None


class FakeServices:
    def __init__(self, backend: "FakeBackend") -> None:
        self._backend = backend

    def get_service(self, uuid: str):
        if uuid == CPS_SERVICE_UUID and self._backend.has_cps:
            return FakeService()
        if uuid == DEVICE_INFORMATION_SERVICE_UUID and self._backend.dis_service_error:
            raise BleakError("Multiple Services with this UUID")
        if uuid == DEVICE_INFORMATION_SERVICE_UUID and self._backend.device_info is not None:
            return FakeDisService(self._backend.device_info)
        return None

    def get_characteristic(self, uuid: str):
        if uuid == SENSOR_LOCATION_UUID:  # CSC + CPS both carry Sensor Location, as on real trainers
            raise BleakError("Multiple Characteristics with this UUID")
        return None


class FakeClient:
    def __init__(self, backend: "FakeBackend", device, disconnected_callback) -> None:
        self.backend = backend
        self.device = device
        self._disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = FakeServices(backend)
        self.notify_callback = None

    async def connect(self) -> None:
        if self.backend.connect_delay:
            await asyncio.sleep(self.backend.connect_delay)
        if self.backend.connect_failures > 0:
            self.backend.connect_failures -= 1
            raise OSError("simulated connect failure")
        self.is_connected = True

    async def read_gatt_char(self, char) -> bytearray:
        uuid = char.uuid
        if uuid == CP_FEATURE_UUID:
            return bytearray(self.backend.feature_bytes)
        if uuid == SENSOR_LOCATION_UUID:
            return bytearray([13])  # Rear hub
        dis = self.backend.device_info or {}
        if uuid in dis:
            if uuid in self.backend.dis_hang:
                await asyncio.Event().wait()
            if uuid in self.backend.dis_fail:
                raise BleakError("simulated read failure")
            return bytearray(dis[uuid])
        raise AssertionError(f"unexpected read {uuid}")

    async def start_notify(self, char, callback) -> None:
        if self.backend.fail_start_notify:
            raise OSError("simulated start_notify failure")
        self.notify_callback = callback
        if self.backend.sessions:
            payloads, drop_after = self.backend.sessions.pop(0)
            asyncio.get_running_loop().call_soon(self._play, payloads, drop_after)

    async def stop_notify(self, char) -> None:
        if self.backend.hang_stop_notify:
            await asyncio.Event().wait()
        self.notify_callback = None

    async def disconnect(self) -> None:
        if self.backend.hang_disconnect:
            await asyncio.Event().wait()
        was_connected = self.is_connected
        self.is_connected = False
        if was_connected:
            self._disconnected_callback(self)  # bleak also calls it on intentional disconnects

    def drop(self) -> None:
        """Simulate link loss."""
        self.is_connected = False
        self._disconnected_callback(self)

    def _play(self, payloads, drop_after: bool) -> None:
        for payload in payloads:
            self.notify_callback(None, bytearray(payload))
        if drop_after:
            self.drop()


class FakeBackend:
    def __init__(
        self,
        scan_results=None,
        *,
        feature_bytes: bytes = WHEEL_AND_CRANK,
        has_cps: bool = True,
        connect_failures: int = 0,
        fail_start_notify: bool = False,
        sessions=None,
        discover_delay: float = 0,
        connect_delay: float = 0,
        hang_stop_notify: bool = False,
        hang_disconnect: bool = False,
        on_discover=None,
        device_info=None,
        dis_fail=(),
        dis_hang=(),
        dis_service_error: bool = False,
    ) -> None:
        if scan_results is None:
            scan_results = [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR)]
        self.scan_results = list(scan_results)
        self.feature_bytes = feature_bytes
        self.has_cps = has_cps
        self.connect_failures = connect_failures
        self.fail_start_notify = fail_start_notify
        # Each session: (payloads to notify right after subscribe, drop link afterwards?)
        self.sessions = list(sessions or [])
        self.discover_delay = discover_delay
        self.connect_delay = connect_delay
        self.hang_stop_notify = hang_stop_notify
        self.hang_disconnect = hang_disconnect
        self.on_discover = on_discover  # called when a scan starts (lets tests sync a signal)
        self.device_info = device_info  # char UUID -> bytes; None = service absent
        self.dis_fail = set(dis_fail)
        self.dis_hang = set(dis_hang)
        self.dis_service_error = dis_service_error  # get_service(DIS) raises, as with duplicate services
        self.clients: list[FakeClient] = []
        self.address_lookups: list[str] = []

    async def discover(self, timeout: float):
        if self.on_discover:
            self.on_discover()
        if self.discover_delay:
            await asyncio.sleep(self.discover_delay)
        return list(self.scan_results)

    async def find_by_address(self, address: str, timeout: float):
        self.address_lookups.append(address)
        for r in self.scan_results:
            if r.address.lower() == address.lower():
                return r.device
        return None

    def make_client(self, device, disconnected_callback) -> FakeClient:
        client = FakeClient(self, device, disconnected_callback)
        self.clients.append(client)
        return client


class FakeOutlet:
    """Stand-in for pylsl.StreamOutlet: records pushed chunks."""

    def __init__(self, info) -> None:
        self.info = info
        self.chunks: list = []

    def push_chunk(self, rows, timestamps) -> None:
        self.chunks.append(([list(r) for r in rows], list(timestamps)))
