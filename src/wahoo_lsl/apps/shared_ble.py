"""One real BLE link shared by several clients in one process.

Windows forbids two processes holding the same BLE device, so the FTMS service and the CPS
logger run in one process and each gets a ``Handle`` that looks like a bleak client. All
handles of a device ride one real client; a drop is reported to every attached handle once.
Used from a single event loop.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from bleak.exc import BleakError

from wahoo_lsl.ble import SHUTDOWN_TIMEOUT_S, BleBackend, BleakBackend, ScanResult
from wahoo_lsl.cps import CPS_SERVICE_UUID

log = logging.getLogger(__name__)

_NOT_CONNECTED = "shared link: not connected"


def _address(device: Any) -> str:
    return str(getattr(device, "address", device)).lower()


def _label(device: Any) -> str:
    return str(getattr(device, "name", None) or getattr(device, "address", device))


def _uuid(char: Any) -> str:
    return str(getattr(char, "uuid", char)).lower()


class SharedBackend:
    """A ``BleBackend`` whose clients share one real connection."""

    def __init__(self, inner: BleBackend | None = None) -> None:
        self._inner = inner or BleakBackend()
        self._device: Any = None  # device of the link (up or connecting)
        self._real: Any = None
        self._up = False
        self._connecting: asyncio.Task | None = None
        self._epoch = 0
        self._attached: set[Handle] = set()
        self._subs: dict[str, tuple[Handle, Any]] = {}  # char uuid -> (owner, char)
        self._scan_lock = asyncio.Lock()

    @property
    def epoch(self) -> int:
        return self._epoch

    # ---- BleBackend ----------------------------------------------------------

    async def discover(self, timeout: float) -> list[ScanResult]:
        async with self._scan_lock:
            if await self._linked():
                uuids = frozenset(str(s.uuid).lower() for s in self._real.services)
                device = self._device
                return [
                    ScanResult(
                        name=getattr(device, "name", None),
                        address=getattr(device, "address", str(device)),
                        rssi=0,
                        cps_advertised=CPS_SERVICE_UUID in uuids,
                        device=device,
                        service_uuids=uuids,
                    )
                ]
            return await self._inner.discover(timeout)

    async def find_by_address(self, address: str, timeout: float) -> Any | None:
        async with self._scan_lock:
            if await self._linked():
                return self._device if _address(self._device) == address.lower() else None
            return await self._inner.find_by_address(address, timeout)

    def make_client(self, device: Any, disconnected_callback: Callable[[Any], None]) -> Handle:
        return Handle(self, device, disconnected_callback)

    # ---- link ------------------------------------------------------------------

    async def _linked(self) -> bool:
        """True when a link is up, waiting for one that is connecting."""
        if self._connecting is not None:
            try:
                await asyncio.shield(self._connecting)
            except Exception:
                pass
        return self._up

    async def _attach(self, handle: Handle) -> None:
        if self._device is not None and _address(handle.device) != _address(self._device):
            raise BleakError(
                f"shared link: already connected to {_label(self._device)}; only one device is supported"
            )
        if handle in self._attached and handle._epoch == self._epoch:
            return
        if not self._up:
            if self._connecting is None:
                self._device = handle.device
                self._connecting = asyncio.ensure_future(self._connect_real(handle.device))
            await asyncio.shield(self._connecting)  # a failure raises to every waiter
        handle._epoch = self._epoch
        self._attached.add(handle)

    async def _connect_real(self, device: Any) -> None:
        real = self._inner.make_client(device, self._on_real_disconnect)
        try:
            self._real = real
            await real.connect()
            self._up = True
        except BaseException:
            self._real = self._device = None
            raise
        finally:
            self._connecting = None

    def _on_real_disconnect(self, real: Any) -> None:
        if not self._up or real is not self._real:
            return  # intentional disconnect, a failed connect, or a stale client
        handles = list(self._attached)
        self._reset()
        for handle in handles:
            try:
                handle._disconnected_callback(handle)
            except Exception:
                log.exception("disconnected callback failed")

    def _reset(self) -> Any:
        """Mark the link down for a new epoch; return the real client that was up."""
        real = self._real
        self._up = False
        self._real = self._device = None
        self._epoch += 1
        self._attached.clear()
        self._subs.clear()
        return real

    async def _detach(self, handle: Handle) -> None:
        if handle not in self._attached or handle._epoch != self._epoch:
            return
        real = self._real
        for uuid, (owner, char) in list(self._subs.items()):
            if owner is handle:
                del self._subs[uuid]
                await _bounded(real.stop_notify(char))
        self._attached.discard(handle)
        if not self._attached:
            self._reset()  # before the real disconnect, so its callback is ignored
            await _bounded(real.disconnect())

    def _live(self, handle: Handle) -> Any:
        if not (self._up and handle in self._attached and handle._epoch == self._epoch):
            raise BleakError(_NOT_CONNECTED)
        return self._real


async def _bounded(coro: Any) -> None:
    try:
        await asyncio.wait_for(coro, SHUTDOWN_TIMEOUT_S)
    except Exception as e:  # includes TimeoutError; never raise from a detach
        log.debug("shared link shutdown step failed: %r", e)


class Handle:
    """What a service sees as its bleak client."""

    def __init__(self, shared: SharedBackend, device: Any, disconnected_callback: Callable[[Any], None]) -> None:
        self._shared = shared
        self.device = device
        self._disconnected_callback = disconnected_callback
        self._epoch = -1

    @property
    def is_connected(self) -> bool:
        s = self._shared
        return s._up and self in s._attached and self._epoch == s._epoch

    @property
    def services(self) -> Any:
        return self._shared._live(self).services

    async def connect(self) -> None:
        await self._shared._attach(self)

    async def disconnect(self) -> None:
        await self._shared._detach(self)

    async def read_gatt_char(self, char: Any, **kwargs: Any) -> Any:
        return await self._shared._live(self).read_gatt_char(char, **kwargs)

    async def write_gatt_char(self, char: Any, data: Any, response: bool | None = None) -> None:
        await self._shared._live(self).write_gatt_char(char, data, response=response)

    async def start_notify(self, char: Any, callback: Callable[..., Any], **kwargs: Any) -> None:
        s = self._shared
        real = s._live(self)
        uuid = _uuid(char)
        if uuid in s._subs:
            raise BleakError(f"shared link: {uuid} is already subscribed")
        s._subs[uuid] = (self, char)
        try:
            await real.start_notify(char, callback, **kwargs)
        except BaseException:
            if s._subs.get(uuid, (None,))[0] is self:
                del s._subs[uuid]
            raise

    async def stop_notify(self, char: Any) -> None:
        s = self._shared
        real = s._live(self)
        uuid = _uuid(char)
        if s._subs.get(uuid, (None,))[0] is not self:
            return
        del s._subs[uuid]
        await real.stop_notify(char)
