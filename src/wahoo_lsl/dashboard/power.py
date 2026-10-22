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
        with self._lock:
            self._pending = None  # never replay a value chosen while disconnected
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
        if watts is None:
            return
        try:
            self._post(("set_ok", self._client.set_target_power(watts)))
        except KickrError as e:
            self._post(("set_error", f"{e.code}: {e.detail}"))
            if e.code == "busy":
                self._drop("service busy: another client is connected")
        except Exception as e:
            self._post(("set_error", f"set {watts} W failed: {_describe(e)}"))
            self._drop(f"connection lost ({_describe(e)})")

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
