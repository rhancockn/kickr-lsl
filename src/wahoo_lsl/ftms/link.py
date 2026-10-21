"""FTMS BLE link (spec §7.2–7.3): own connection, control-point commands, notifications.

The only ftms module that touches bleak; discovery and bounded shutdown come from ble.py.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from bleak.exc import BleakError

from .. import clock
from ..ble import BleBackend, BleakBackend, BleSetupError, _bounded, find_device
from . import protocol as p
from .config import FtmsConfig

log = logging.getLogger(__name__)


class FtmsSetupError(BleSetupError):
    """Could not connect to / set up FTMS control. CLI exit code 3."""


class FtmsUnsupported(FtmsSetupError):
    """The device does not support power target setting."""


class LinkError(Exception):
    """Transport failure: not connected, write failed, or link closed while waiting."""


class CommandTimeout(LinkError):
    """No control-point response within ack_timeout_s."""


class CommandRejected(Exception):
    """The device answered a control-point command with a result other than success."""

    def __init__(self, opcode: int, result: int) -> None:
        super().__init__(f"{p.opcode_name(opcode)} rejected: {p.result_name(result)}")
        self.opcode = opcode
        self.result = result


class FtmsLink:
    """One BLE connection to the trainer's Fitness Machine Service."""

    def __init__(self, cfg: FtmsConfig, *, backend: BleBackend | None = None, platform: str | None = None) -> None:
        self._cfg = cfg
        self._backend = backend or BleakBackend()
        self._platform = platform or sys.platform
        self._client: Any = None  # None means "ignore disconnect callbacks"
        self._chars: dict[str, Any] = {}
        self._subscribed: list[Any] = []
        self._lock = asyncio.Lock()
        self._waiters: dict[int, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.power_range: p.PowerRange = p.DEFAULT_POWER_RANGE
        self.features: p.FtmsFeatures | None = None
        self.device_label = "device"
        # Handlers, assigned by the Controller. All are called on the event-loop thread.
        self.on_bike_data = lambda data, t_lsl: None
        self.on_status = lambda event, t_lsl: None
        self.on_link_lost = lambda reason: None

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "is_connected", False))

    # ---- setup ---------------------------------------------------------------

    async def open(self) -> None:
        """Connect, check features, subscribe, take control, start, set 0 W (spec §7.3 steps 1–4).

        Raises BleSetupError (FtmsSetupError for FTMS-specific failures); leaves nothing connected.
        """
        self._loop = asyncio.get_running_loop()
        try:
            await self._open()
        except BleSetupError:
            await self.close()
            raise
        except (p.ProtocolError, LinkError, CommandRejected, BleakError, OSError) as e:
            await self.close()
            raise FtmsSetupError(f"FTMS setup failed: {e}") from e
        except BaseException:
            await self.close()
            raise

    async def _open(self) -> None:
        device = await find_device(
            self._backend, self._cfg.device, self._platform, lambda r: p.FTMS_SERVICE_UUID in r.service_uuids
        )
        self.device_label = getattr(device, "name", None) or getattr(device, "address", str(device))
        client = self._backend.make_client(device, self._on_disconnect)
        self._client = client
        await client.connect()
        service = client.services.get_service(p.FTMS_SERVICE_UUID)
        if service is None:
            raise FtmsSetupError(f"{self.device_label} does not expose the Fitness Machine Service")
        # Resolve on the service, not the collection: other services may repeat a UUID.
        chars = {}
        for key, uuid in (
            ("feature", p.FEATURE_UUID),
            ("control_point", p.CONTROL_POINT_UUID),
            ("status", p.STATUS_UUID),
            ("bike_data", p.INDOOR_BIKE_DATA_UUID),
        ):
            char = service.get_characteristic(uuid)
            if char is None:
                raise FtmsSetupError(f"Fitness Machine Service has no {key} characteristic")
            chars[key] = char
        self._chars = chars
        self.features = p.parse_feature(await client.read_gatt_char(chars["feature"]))
        if not self.features.power_target_supported:
            raise FtmsUnsupported(
                f"{self.device_label} does not support power target setting "
                f"(target-setting features 0x{self.features.target:08x})"
            )
        range_char = service.get_characteristic(p.POWER_RANGE_UUID)
        if range_char is None:
            self.power_range = p.DEFAULT_POWER_RANGE
            log.warning("No Supported Power Range characteristic; assuming %s", self.power_range)
        else:
            self.power_range = p.parse_power_range(await client.read_gatt_char(range_char))
        # Control point first: indications must be enabled before the first write.
        for key, handler in (("control_point", self._on_cp), ("status", self._on_status), ("bike_data", self._on_bike_data)):
            await client.start_notify(chars[key], handler)
            self._subscribed.append(chars[key])
        for payload in (p.request_control(), p.start_or_resume(), p.set_target_power(0)):
            await self.command(payload)
        r = self.power_range
        log.info("FTMS control of %s at 0 W (power range %d..%d W, step %d)", self.device_label, r.min_w, r.max_w, r.step_w)

    # ---- commands ------------------------------------------------------------

    async def command(self, payload: bytes) -> p.CpResponse:
        """Write one control-point op and wait for its response (one op in flight at a time)."""
        opcode = payload[0]
        name = p.opcode_name(opcode)
        async with self._lock:
            client = self._client
            if client is None:
                raise LinkError(f"{name}: not connected")
            future = asyncio.get_running_loop().create_future()
            self._waiters[opcode] = future  # before writing: the indication may beat the write's return
            started = time.perf_counter()
            log.debug("CP write %s %s", name, payload.hex())
            timeout = self._cfg.ftms.ack_timeout_s
            try:
                response = await asyncio.wait_for(self._write_and_wait(client, payload, future), timeout)
            except asyncio.TimeoutError:  # before OSError: TimeoutError subclasses OSError on 3.11+
                raise CommandTimeout(f"{name}: no response within {timeout:g}s") from None
            except (BleakError, OSError) as e:
                raise LinkError(f"{name} write failed: {e}") from e
            finally:
                self._waiters.pop(opcode, None)
            log.debug(
                "CP %s -> %s in %.1f ms", name, p.result_name(response.result), (time.perf_counter() - started) * 1000
            )
            if response.result != p.RESULT_SUCCESS:
                raise CommandRejected(opcode, response.result)
            return response

    async def _write_and_wait(self, client: Any, payload: bytes, future: asyncio.Future) -> p.CpResponse:
        await client.write_gatt_char(self._chars["control_point"], payload, response=True)
        return await future

    # ---- callbacks (event-loop thread) ---------------------------------------

    def _on_cp(self, _sender: Any, data: bytearray) -> None:
        raw = bytes(data)
        try:
            response = p.parse_cp_response(raw)
        except p.ProtocolError as e:
            log.warning("Bad control point indication %s: %s", raw.hex(), e)
            return
        log.debug("CP indication %s", raw.hex())
        future = self._waiters.get(response.request_opcode)
        if future is None or future.done():
            log.debug("Unsolicited control point response %s ignored", raw.hex())
            return
        future.set_result(response)

    def _on_status(self, _sender: Any, data: bytearray) -> None:
        t_lsl = clock.now_lsl()
        raw = bytes(data)
        try:
            event = p.parse_status(raw)
        except p.ProtocolError as e:
            log.warning("Bad machine status %s: %s", raw.hex(), e)
            return
        log.debug("status %s -> %s", raw.hex(), event)
        if event.opcode == p.STATUS_CONTROL_PERMISSION_LOST:
            log.warning("Trainer reports control permission lost (another app took control?)")
            self.on_link_lost("control permission lost")
        self.on_status(event, t_lsl)

    def _on_bike_data(self, _sender: Any, data: bytearray) -> None:
        t_lsl = clock.now_lsl()
        raw = bytes(data)
        try:
            bike = p.parse_bike_data(raw)
        except p.ProtocolError as e:
            log.warning("Bad indoor bike data %s: %s", raw.hex(), e)
            return
        log.debug("bike data %s -> %s", raw.hex(), bike)
        self.on_bike_data(bike, t_lsl)

    def _on_disconnect(self, client: Any) -> None:
        if client is not self._client:
            return  # intentional close (we cleared _client first) or a stale client
        log.warning("FTMS connection to %s lost", self.device_label)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.on_link_lost, "connection lost")

    # ---- shutdown --------------------------------------------------------------

    async def close(self) -> None:
        """Fail pending commands, unsubscribe and disconnect; each step bounded. Idempotent."""
        client, self._client = self._client, None  # clear first so the callback ignores this disconnect
        for future in self._waiters.values():
            if not future.done():
                future.set_exception(LinkError("link closed"))
        subscribed, self._subscribed = self._subscribed, []
        if client is None:
            return
        if getattr(client, "is_connected", False):
            for char in subscribed:
                await _bounded(client.stop_notify(char), "stop_notify")
        await _bounded(client.disconnect(), "disconnect")
