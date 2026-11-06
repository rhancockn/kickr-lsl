"""Background thread that owns the dashboard's KickrClient; the Tk thread never blocks on it."""
from __future__ import annotations

import threading
from typing import Any, Callable

from wahoo_lsl.ftms import DEFAULT_HOST, DEFAULT_PORT
from wahoo_lsl.ftms.client import KickrClient, KickrError

Post = Callable[[tuple[str, Any]], None]


def _describe(e: BaseException) -> str:
    return str(e) or type(e).__name__


class PowerWorker:
    """Sends the latest requested target and polls read(), posting results as (kind, payload).

    A value requested while disconnected, or a set that fails, is dropped and never replayed.
    The last requested slew rate is (re)sent after every connect, because the service resets it when a client
    disconnects.
    """

    def __init__(
        self,
        post: Post,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        poll_s: float = 0.5,
        retry_s: float = 1.0,
        timeout_s: float = 1.0,
        client_factory: Callable[..., Any] = KickrClient,
    ) -> None:
        self._post = post
        self._host, self._port = host, port
        self._poll_s, self._retry_s, self._timeout_s = poll_s, retry_s, timeout_s
        self._factory = client_factory
        self._lock = threading.Lock()
        self._pending: int | None = None
        self._slew: float | None = None
        self._slew_dirty = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._client: Any = None
        self._down_msg: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ftms-power-worker", daemon=True)
        self._thread.start()

    def request(self, watts: int) -> None:
        with self._lock:
            self._pending = int(watts)
        self._wake.set()

    def request_slew(self, w_per_s: float) -> None:
        with self._lock:
            self._slew = float(w_per_s)
            self._slew_dirty = True
        self._wake.set()

    def stop(self, join_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(join_s)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._client is None and not self._connect():
                    self._stop.wait(self._retry_s)  # not _wake: requests must not shorten the retry
                    continue
                self._send_pending()
                if self._client is not None:
                    self._read()
                if self._client is None:  # dropped (e.g. busy): retry at the retry rate
                    self._stop.wait(self._retry_s)
                    continue
                self._sleep(self._poll_s)
        finally:
            self._shutdown()

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _connect(self) -> bool:
        try:
            self._client = self._factory(self._host, self._port, self._timeout_s)
        except OSError as e:
            self._down(f"service unreachable ({_describe(e)})")
            return False
        except KickrError as e:  # the connect handshake was refused: busy or not_allowed
            self._post(("service_error", f"{e.code}: {e.detail}"))
            self._down(f"service refused the connection ({e.code})")
            return False
        with self._lock:
            self._pending = None  # never replay a value chosen while disconnected
            self._slew_dirty = self._slew is not None  # the service reset it on our last disconnect
        self._down_msg = None
        self._post(("service_connected", None))
        return True

    def _down(self, msg: str) -> None:
        if msg != self._down_msg:  # each distinct message once, not every retry
            self._down_msg = msg
            self._post(("service_down", msg))

    def _drop(self, msg: str) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None
        self._down(msg)

    def _send_pending(self) -> None:
        with self._lock:
            watts, self._pending = self._pending, None
            slew = self._slew if self._slew_dirty else None
            self._slew_dirty = False
        if slew is not None and not self._call("slew", self._client.set_slew_rate, slew, f"slew {slew:g} W/s"):
            return
        if watts is not None:
            self._call("set", self._client.set_target_power, watts, f"set {watts} W")

    def _call(self, kind: str, fn: Callable[[Any], Any], value: Any, what: str) -> bool:
        """Post <kind>_ok / <kind>_error; drop the connection on busy or I/O errors. True if still connected."""
        try:
            self._post((f"{kind}_ok", fn(value)))
        except KickrError as e:
            self._post((f"{kind}_error", f"{e.code}: {e.detail}"))
            if e.code == "busy":
                self._drop("service busy: another client is connected")
        except Exception as e:
            self._post((f"{kind}_error", f"{what} failed: {_describe(e)}"))
            self._drop(f"connection lost ({_describe(e)})")
        return self._client is not None

    def _read(self) -> None:
        try:
            status = self._client.read()
        except KickrError as e:
            self._post(("service_error", f"{e.code}: {e.detail}"))
            if e.code == "busy":
                self._drop("service busy: another client is connected")
            return
        except Exception as e:
            self._drop(f"connection lost ({_describe(e)})")
            return
        self._post(("status", status))

    def _shutdown(self) -> None:
        if self._client is None:
            return
        try:
            self._client.set_target_power(0)
        except Exception:
            pass  # closing the client makes the service spin down anyway
        try:
            self._client.close()
        except OSError:
            pass
        self._client = None
