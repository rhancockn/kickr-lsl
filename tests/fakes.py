"""In-memory stand-ins for bleak, used by BLE and CLI tests."""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

from wahoo_lsl.ble import ScanResult
from wahoo_lsl.cps import CP_FEATURE_UUID, CPS_SERVICE_UUID, SENSOR_LOCATION_UUID

KICKR = SimpleNamespace(name="KICKR CORE 1234", address="AA:BB:CC:DD:EE:01")
WHEEL_AND_CRANK = struct.pack("<I", 0b1100)
ALWAYS = 10**9


class FakeServices:
    def __init__(self, has_cps: bool) -> None:
        self._has_cps = has_cps

    def get_service(self, uuid: str):
        return object() if uuid == CPS_SERVICE_UUID and self._has_cps else None

    def get_characteristic(self, uuid: str):
        return object() if uuid == SENSOR_LOCATION_UUID else None


class FakeClient:
    def __init__(self, backend: "FakeBackend", device, disconnected_callback) -> None:
        self.backend = backend
        self.device = device
        self._disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = FakeServices(backend.has_cps)
        self.notify_callback = None

    async def connect(self) -> None:
        if self.backend.connect_failures > 0:
            self.backend.connect_failures -= 1
            raise OSError("simulated connect failure")
        self.is_connected = True

    async def read_gatt_char(self, uuid: str) -> bytearray:
        if uuid == CP_FEATURE_UUID:
            return bytearray(self.backend.feature_bytes)
        if uuid == SENSOR_LOCATION_UUID:
            return bytearray([13])  # Rear hub
        raise AssertionError(f"unexpected read {uuid}")

    async def start_notify(self, uuid: str, callback) -> None:
        if self.backend.fail_start_notify:
            raise OSError("simulated start_notify failure")
        self.notify_callback = callback
        if self.backend.sessions:
            payloads, drop_after = self.backend.sessions.pop(0)
            asyncio.get_running_loop().call_soon(self._play, payloads, drop_after)

    async def stop_notify(self, uuid: str) -> None:
        self.notify_callback = None

    async def disconnect(self) -> None:
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
        self.clients: list[FakeClient] = []
        self.address_lookups: list[str] = []

    async def discover(self, timeout: float):
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
