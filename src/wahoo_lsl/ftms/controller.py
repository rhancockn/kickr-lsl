"""FTMS controller (spec §7.4–7.7): target state, rate-limited sender, spin-down, reconnect.

All methods run on the event loop; link callbacks also arrive there, so no locks are needed.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import clock
from ..ble import BleSetupError, ReconnectExhausted, ReconnectPolicy, _sleep_unless, _unless_stopped
from . import protocol as p
from .config import FtmsConfig
from .link import CommandRejected, LinkError

log = logging.getLogger(__name__)

SPIN_DOWN_TIMEOUT_S = 2.0
SHUTDOWN_SPIN_DOWN_ATTEMPTS = 3  # at shutdown nothing retries later, so try a few times before releasing the link
SHUTDOWN_RETRY_DELAY_S = 0.2
MAX_LINK_FAILURES = 3  # consecutive LinkErrors from the sender -> treat the link as lost
MAX_REJECTIONS = 3  # rejected sends of one requested target before giving up on it


class LinkDown(Exception):
    """A target was requested while the BLE link is not ready."""


@dataclass
class State:
    link: str  # "connecting" | "ready" | "reconnecting" | "closed"
    link_epoch: int
    requested_w: int
    confirmed_w: int | None
    confirmed_t_lsl: float | None
    power_w: int | None
    power_t_lsl: float | None
    last_error: str | None
    last_error_t_lsl: float | None


class Link(Protocol):
    power_range: p.PowerRange
    on_bike_data: Callable[[p.BikeData, float], None]
    on_status: Callable[[p.StatusEvent, float], None]
    on_link_lost: Callable[[str], None]

    async def open(self) -> None: ...

    async def command(self, payload: bytes) -> p.CpResponse: ...

    async def close(self) -> None: ...


class Controller:
    def __init__(
        self,
        cfg: FtmsConfig,
        link: Link,
        *,
        now: Callable[[], float] = clock.now_lsl,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._link = link
        self._now = now
        self._sleep = sleep
        self._min_interval = 1.0 / cfg.ftms.max_rate_hz
        self.state = State("connecting", 0, 0, None, None, None, None, None, None)
        self._wake = asyncio.Event()
        self._lost = asyncio.Event()
        self._sender_task: asyncio.Task | None = None
        self._last_send = -math.inf
        self._link_failures = 0
        self._rejections = 0
        self._requests_since_send = 0
        link.on_bike_data = self._on_bike_data
        link.on_status = self._on_status
        link.on_link_lost = self._on_link_lost

    @property
    def pending(self) -> bool:
        return self.state.link == "ready" and self.state.requested_w != self.state.confirmed_w

    # ---- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """First connection (spec §7.3). Raises BleSetupError; starts the sender on success."""
        await self._link.open()
        self._mark_ready()
        self._sender_task = asyncio.create_task(self._sender(), name="ftms-sender")

    async def run(self, stop: asyncio.Event) -> None:
        """Supervise until ``stop`` is set, reconnecting on link loss.

        Raises ReconnectExhausted, or re-raises an unexpected exception from the sender.
        """
        assert self._sender_task is not None, "start() must succeed before run()"
        policy = ReconnectPolicy(self._cfg.reconnect.backoff_s, self._cfg.reconnect.max_attempts)
        while True:
            waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._lost.wait())]
            try:
                await asyncio.wait([*waits, self._sender_task], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
            if self._sender_task.done():
                self._sender_task.result()  # re-raises the sender's exception
                raise RuntimeError("FTMS sender stopped unexpectedly")
            if stop.is_set():
                return
            await self._reconnect(policy, stop)

    async def close(self, reason: str = "shutdown") -> None:
        """Stop the sender, spin down to 0 W (bounded retries), release the link. Bounded; never raises."""
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
        for attempt in range(1, SHUTDOWN_SPIN_DOWN_ATTEMPTS + 1):
            if await self.spin_down(reason) or self.state.link != "ready":
                break
            if attempt == SHUTDOWN_SPIN_DOWN_ATTEMPTS:
                log.error(
                    "spin-down (%s) failed %d times; the trainer may still hold its last target",
                    reason,
                    attempt,
                )
            else:
                await self._sleep(SHUTDOWN_RETRY_DELAY_S)
        self.state.link = "closed"
        try:
            await self._link.close()
        except Exception as e:
            log.warning("Closing the FTMS link failed: %s", e)

    async def _reconnect(self, policy: ReconnectPolicy, stop: asyncio.Event) -> None:
        if not self._cfg.reconnect.enabled:
            raise ReconnectExhausted("FTMS link lost and reconnect is disabled")
        await self._link.close()  # release the dead client
        failures = 0
        while True:
            delay = policy.delay(failures)
            if await _sleep_unless(stop, delay):
                return
            attempt = failures + 1
            log.warning("FTMS reconnect attempt %d (after %.1fs)", attempt, delay)
            try:
                await _unless_stopped(self._link.open(), stop)
            except BleSetupError as e:
                failures += 1
                log.warning("FTMS reconnect attempt %d failed: %s", attempt, e)
                if policy.exhausted(failures):
                    raise ReconnectExhausted(f"gave up after {failures} failed FTMS reconnect attempt(s)") from e
                continue
            if stop.is_set():
                return
            self._mark_ready()
            log.warning(
                "FTMS reconnected; target is 0 W until the client sends a new one (link_epoch %d)",
                self.state.link_epoch,
            )
            return

    # ---- client-facing ---------------------------------------------------------

    def request_target(self, watts: int) -> tuple[int, bool]:
        """Store a new target (clamped) and wake the sender. No BLE I/O; returns at once."""
        if self.state.link != "ready":
            raise LinkDown(f"BLE link is {self.state.link}")
        accepted = self.clamp(watts)
        self.state.requested_w = accepted
        self._rejections = 0
        self._requests_since_send += 1
        self._wake.set()
        return accepted, accepted != watts

    def clamp(self, watts: int) -> int:
        r = self._link.power_range
        lo = max(0, r.min_w)
        hi = r.max_w if self._cfg.ftms.max_watts is None else min(r.max_w, self._cfg.ftms.max_watts)
        hi = max(hi, lo)
        step = max(1, r.step_w)
        w = min(max(watts, lo), hi)
        w = lo + (w - lo + step // 2) // step * step
        return w if w <= hi else w - step

    async def spin_down(self, reason: str) -> bool:
        """Send 0 W now (bypassing the rate limit). Returns success; never raises."""
        self.state.requested_w = 0
        if self.state.link != "ready":
            level = logging.WARNING if self.state.link == "reconnecting" else logging.DEBUG
            log.log(level, "spin-down (%s): link is %s, cannot send 0 W", reason, self.state.link)
            return False
        epoch = self.state.link_epoch
        try:
            await asyncio.wait_for(self._link.command(p.set_target_power(0)), SPIN_DOWN_TIMEOUT_S)
        except Exception as e:
            self._set_error(f"spin-down ({reason}) failed: {e or type(e).__name__}")
            self._wake.set()  # requested_w is 0: let the rate-limited sender retry 0 W
            return False
        self._confirm(0, epoch)
        log.info("spin-down (%s): 0 W confirmed", reason)
        return True

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        age = None if s.power_t_lsl is None else round(self._now() - s.power_t_lsl, 3)
        return {
            "link": s.link,
            "link_epoch": s.link_epoch,
            "target": {
                "requested_w": s.requested_w,
                "confirmed_w": s.confirmed_w,
                "pending": self.pending,
                "t_lsl": s.confirmed_t_lsl,
            },
            "power": {"watts": s.power_w, "t_lsl": s.power_t_lsl, "age_s": age},
            "last_error": s.last_error,
            "last_error_t_lsl": s.last_error_t_lsl,
        }

    # ---- sender ------------------------------------------------------------------

    async def _sender(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self.pending:
                continue
            wait = self._last_send + self._min_interval - self._now()
            if wait > 0:
                await self._sleep(wait)
            if not self.pending:
                continue  # link lost, or the latest request equals what is confirmed
            target = self.state.requested_w  # read after the wait: latest wins
            if self._requests_since_send > 1:
                log.debug("coalesced %d target(s); sending %d W", self._requests_since_send - 1, target)
            self._requests_since_send = 0
            self._last_send = self._now()
            await self._send(target)

    async def _send(self, target: int) -> None:
        epoch = self.state.link_epoch
        try:
            await self._link.command(p.set_target_power(target))
        except CommandRejected as e:
            if self.state.link != "ready" or self.state.link_epoch != epoch:
                return  # superseded by a link loss; keep the "link lost" error
            self._rejections += 1
            self._set_error(f"set {target} W rejected: {e}")
            if self._rejections < MAX_REJECTIONS:
                self._wake.set()
            else:
                log.warning("Giving up on %d W after %d rejections; waiting for a new target", target, self._rejections)
            return
        except LinkError as e:
            if self.state.link != "ready" or self.state.link_epoch != epoch:
                return  # superseded by a link loss; keep the "link lost" error
            self._link_failures += 1
            self._set_error(f"set {target} W failed: {e}")
            if self._link_failures >= MAX_LINK_FAILURES:
                self._on_link_lost(f"{self._link_failures} consecutive control-point failures")
            else:
                self._wake.set()
            return
        self._link_failures = 0
        self._confirm(target, epoch)
        log.debug("target %d W confirmed", target)

    # ---- state helpers and link callbacks ----------------------------------------

    def _confirm(self, watts: int, epoch: int) -> None:
        # A success from before a reconnect must not mark a stale value confirmed.
        if self.state.link == "ready" and self.state.link_epoch == epoch:
            self.state.confirmed_w = watts
            self.state.confirmed_t_lsl = self._now()

    def _mark_ready(self) -> None:
        s = self.state
        s.link = "ready"
        s.link_epoch += 1
        s.requested_w = 0
        s.confirmed_w = 0
        s.confirmed_t_lsl = self._now()
        self._link_failures = 0
        self._rejections = 0
        self._requests_since_send = 0
        self._lost.clear()
        log.info("FTMS control ready at 0 W (link_epoch %d)", s.link_epoch)

    def _set_error(self, message: str) -> None:
        self.state.last_error = message
        self.state.last_error_t_lsl = self._now()
        log.warning("%s", message)

    def _on_link_lost(self, reason: str) -> None:
        if self.state.link != "ready":
            return  # already reconnecting or closed
        s = self.state
        s.link = "reconnecting"
        s.requested_w = 0
        s.confirmed_w = None
        s.confirmed_t_lsl = None
        self._set_error(f"link lost: {reason}; target reset to 0 W")
        self._lost.set()

    def _on_status(self, event: p.StatusEvent, t_lsl: float) -> None:
        if event.opcode == p.STATUS_TARGET_POWER_CHANGED and event.target_power_w is not None:
            if self.state.link == "ready":
                self.state.confirmed_w = event.target_power_w
                self.state.confirmed_t_lsl = t_lsl
        elif event.opcode != p.STATUS_CONTROL_PERMISSION_LOST:
            log.info("Trainer status: %s", event.name)

    def _on_bike_data(self, data: p.BikeData, t_lsl: float) -> None:
        if data.power_w is not None:
            self.state.power_w = data.power_w
            self.state.power_t_lsl = t_lsl
