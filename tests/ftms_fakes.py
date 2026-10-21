"""In-memory stand-in for a KICKR's FTMS over bleak (link and CLI tests)."""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

from bleak.exc import BleakError

from wahoo_lsl.ble import ScanResult
from wahoo_lsl.ftms import protocol as p

FTMS_KICKR = SimpleNamespace(name="KICKR V6 1234", address="AA:BB:CC:DD:EE:02")
POWER_TARGET_FEATURE = struct.pack("<II", 0, 1 << 3)
POWER_RANGE = struct.pack("<hhH", 0, 2000, 1)
ALL_CHARS = (p.FEATURE_UUID, p.INDOOR_BIKE_DATA_UUID, p.POWER_RANGE_UUID, p.CONTROL_POINT_UUID, p.STATUS_UUID)


class FakeFtmsService:
    def __init__(self, uuids) -> None:
        self._uuids = set(uuids)

    def get_characteristic(self, uuid: str):
        return SimpleNamespace(uuid=uuid) if uuid in self._uuids else None


class FakeFtmsServices:
    def __init__(self, backend: "FakeFtmsBackend") -> None:
        self._backend = backend

    def get_service(self, uuid: str):
        if uuid == p.FTMS_SERVICE_UUID and self._backend.has_ftms:
            return FakeFtmsService(self._backend.char_uuids)
        return None


class FakeFtmsClient:
    def __init__(self, backend: "FakeFtmsBackend", device, disconnected_callback) -> None:
        self.backend = backend
        self.device = device
        self._disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = FakeFtmsServices(backend)
        self.callbacks: dict = {}

    async def connect(self) -> None:
        if self.backend.connect_failures > 0:
            self.backend.connect_failures -= 1
            raise OSError("simulated connect failure")
        self.is_connected = True

    async def read_gatt_char(self, char) -> bytearray:
        if char.uuid == p.FEATURE_UUID:
            return bytearray(self.backend.feature_bytes)
        if char.uuid == p.POWER_RANGE_UUID:
            return bytearray(self.backend.power_range_bytes)
        raise AssertionError(f"unexpected read {char.uuid}")

    async def start_notify(self, char, callback) -> None:
        self.backend.log.append(("subscribe", char.uuid))
        self.callbacks[char.uuid] = callback

    async def stop_notify(self, char) -> None:
        self.backend.log.append(("unsubscribe", char.uuid))
        self.callbacks.pop(char.uuid, None)

    async def write_gatt_char(self, char, data, response: bool = False) -> None:
        assert char.uuid == p.CONTROL_POINT_UUID and response is True
        data = bytes(data)
        self.backend.log.append(("write", data))
        self.backend.writes.append(data)
        op = data[0]
        behaviour = self.backend.behaviour(op)  # "ok" | "early" | "drop" | "raise" | int result code
        if behaviour == "raise":
            raise BleakError("simulated write failure")
        if behaviour == "drop":
            return
        result = p.RESULT_SUCCESS if behaviour in ("ok", "early") else behaviour
        indication = bytearray([p.OP_RESPONSE, op, result])
        callback = self.callbacks.get(p.CONTROL_POINT_UUID)
        if callback is None:
            return  # not subscribed: a real device sends no indication either
        if behaviour == "early":
            callback(None, indication)  # indication arrives before the write call returns
        else:
            asyncio.get_running_loop().call_soon(callback, None, indication)

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

    def notify(self, uuid: str, payload: bytes) -> None:
        self.callbacks[uuid](None, bytearray(payload))


class FakeFtmsBackend:
    def __init__(
        self,
        scan_results=None,
        *,
        has_ftms: bool = True,
        feature_bytes: bytes = POWER_TARGET_FEATURE,
        power_range_bytes: bytes = POWER_RANGE,
        has_power_range: bool = True,
        connect_failures: int = 0,
        hang_disconnect: bool = False,
        responses=None,
    ) -> None:
        if scan_results is None:
            scan_results = [
                ScanResult(FTMS_KICKR.name, FTMS_KICKR.address, -50, False, FTMS_KICKR, frozenset({p.FTMS_SERVICE_UUID}))
            ]
        self.scan_results = list(scan_results)
        self.has_ftms = has_ftms
        self.feature_bytes = feature_bytes
        self.power_range_bytes = power_range_bytes
        self.char_uuids = [u for u in ALL_CHARS if has_power_range or u != p.POWER_RANGE_UUID]
        self.connect_failures = connect_failures
        self.hang_disconnect = hang_disconnect
        # opcode -> behaviours consumed one per write, in order; "ok" once exhausted
        self.responses = {op: list(seq) for op, seq in (responses or {}).items()}
        self.log: list = []
        self.writes: list[bytes] = []
        self.clients: list[FakeFtmsClient] = []
        self.address_lookups: list[str] = []

    def behaviour(self, op: int):
        queue = self.responses.get(op)
        return queue.pop(0) if queue else "ok"

    async def discover(self, timeout: float):
        return list(self.scan_results)

    async def find_by_address(self, address: str, timeout: float):
        self.address_lookups.append(address)
        for r in self.scan_results:
            if r.address.lower() == address.lower():
                return r.device
        return None

    def make_client(self, device, disconnected_callback) -> FakeFtmsClient:
        client = FakeFtmsClient(self, device, disconnected_callback)
        self.clients.append(client)
        return client
