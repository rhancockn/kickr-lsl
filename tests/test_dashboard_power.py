import queue
import socket
import threading
import time

from ftms_stub import StubService, snapshot

from wahoo_lsl.dashboard.power import PowerWorker
from wahoo_lsl.ftms.client import PowerReading, Status, TargetState

STATUS = Status("ready", 1, TargetState(0, 0, False, None), PowerReading(None, None, None), None, None)


class Events:
    def __init__(self):
        self.q = queue.Queue()
        self.seen = []

    def __call__(self, item):
        self.q.put(item)

    def wait(self, kind, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {kind!r} event; saw {[k for k, _ in self.seen]}")
            try:
                k, p = self.q.get(timeout=remaining)
            except queue.Empty:
                continue
            self.seen.append((k, p))
            if k == kind:
                return p

    def kinds(self):
        while True:
            try:
                self.seen.append(self.q.get_nowait())
            except queue.Empty:
                return [k for k, _ in self.seen]


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def default_reply(request):
    if request["cmd"] == "set":
        return {"ok": True, "target_w": request["watts"], "clamped": False}
    return snapshot(requested=150, confirmed=150)


def sets(svc):
    return [r["watts"] for r in svc.requests if r["cmd"] == "set"]


def test_connects_and_posts_status():
    ev = Events()
    with StubService(default_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            assert ev.wait("service_connected") is None
            status = ev.wait("status")
            assert isinstance(status, Status) and status.target.requested_w == 150
        finally:
            w.stop()


def test_request_sends_set_and_posts_set_ok():
    ev = Events()
    with StubService(default_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            w.request(180)
            assert ev.wait("set_ok") == 180
            assert sets(svc)[0] == 180
        finally:
            w.stop()


def test_rapid_requests_coalesce_to_latest():
    ev = Events()
    gate, entered = threading.Event(), threading.Event()

    def reply(request):
        if request["cmd"] == "read" and not gate.is_set():
            entered.set()
            gate.wait(3.0)
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            assert entered.wait(3.0)  # the worker is blocked inside read()
            for watts in (100, 150, 200):
                w.request(watts)
            gate.set()
            assert ev.wait("set_ok") == 200
            time.sleep(0.1)
            assert sets(svc) == [200]
        finally:
            gate.set()
            w.stop()


def test_failed_set_is_dropped_not_retried():
    ev = Events()

    def reply(request):
        if request["cmd"] == "set":
            return {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            w.request(120)
            assert ev.wait("set_error") == "link_down: BLE link is reconnecting"
            for _ in range(3):
                ev.wait("status")
            assert sets(svc) == [120]
        finally:
            w.stop()
        assert sets(svc) == [120, 0]  # only the shutdown 0 W follows


def test_reconnect_after_connection_loss_posts_connected_again():
    ev = Events()
    reads = {"n": 0}

    def reply(request):
        if request["cmd"] == "read":
            reads["n"] += 1
            if reads["n"] == 1:
                return None  # the stub closes the connection
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02, retry_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            assert ev.wait("service_down").startswith("connection lost")
            ev.wait("service_connected")
            ev.wait("status")
        finally:
            w.stop()


def test_unreachable_posts_service_down_once():
    ev = Events()
    w = PowerWorker(ev, port=closed_port(), retry_s=0.01)
    w.start()
    try:
        assert "unreachable" in ev.wait("service_down")
        time.sleep(0.2)
        assert ev.kinds().count("service_down") == 1
    finally:
        w.stop()


class FakeClient:
    def __init__(self):
        self.sets = []
        self.closed = False

    def set_target_power(self, watts):
        self.sets.append(watts)
        return watts

    def read(self):
        return STATUS

    def close(self):
        self.closed = True


def test_pending_value_cleared_on_connect():
    ev = Events()
    client, allow = FakeClient(), threading.Event()

    def factory(host, port, timeout_s):
        if not allow.is_set():
            raise ConnectionRefusedError("refused")
        return client

    w = PowerWorker(ev, poll_s=0.01, retry_s=0.01, client_factory=factory)
    w.start()
    try:
        ev.wait("service_down")
        w.request(300)  # chosen while the service is down
        allow.set()
        ev.wait("service_connected")
        ev.wait("status")
        ev.wait("status")
        assert client.sets == []
    finally:
        w.stop()
    assert client.sets == [0] and client.closed


def test_busy_reply_drops_connection_and_retries():
    ev = Events()
    with StubService(lambda r: {"ok": False, "error": "busy", "detail": "another client is connected"}) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02, retry_s=0.02)
        w.start()
        try:
            ev.wait("service_connected")
            assert ev.wait("service_error") == "busy: another client is connected"
            assert "busy" in ev.wait("service_down")
            ev.wait("service_connected")  # retried
        finally:
            w.stop()


def test_stop_sends_zero_and_closes():
    ev = Events()
    with StubService(default_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        ev.wait("status")
        w.stop()
        assert not w._thread.is_alive()
        assert svc.requests[-1] == {"cmd": "set", "watts": 0}
        deadline = time.monotonic() + 2.0
        while svc.disconnects < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_stop_while_unreachable_is_prompt():
    w = PowerWorker(Events(), port=closed_port(), retry_s=10.0)
    w.start()
    time.sleep(0.05)
    started = time.monotonic()
    w.stop()
    assert time.monotonic() - started < 2.0
    assert not w._thread.is_alive()


def test_busy_service_is_retried_at_the_retry_rate_even_if_ui_requests_on_connect():
    connects = []

    def post(item):
        if item[0] == "service_connected":
            connects.append(time.monotonic())
            w.request(0)  # what the UI does on every service_connected

    with StubService(lambda r: {"ok": False, "error": "busy", "detail": "another client is connected"}) as svc:
        w = PowerWorker(post, port=svc.port, poll_s=0.02, retry_s=0.5)
        w.start()
        try:
            time.sleep(1.6)
        finally:
            w.stop()
    assert 2 <= len(connects) <= 5, len(connects)


def slew_reply(request):
    if request["cmd"] == "slew":
        return {"ok": True, "slew_w_per_s": float(request["w_per_s"])}
    return default_reply(request)


def test_slew_requested_before_connect_is_sent_on_connect_before_targets():
    ev = Events()
    with StubService(slew_reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.request_slew(20)
        w.start()
        try:
            assert ev.wait("slew_ok") == 20.0
            w.request(180)
            ev.wait("set_ok")
        finally:
            w.stop()
        writes = [r for r in svc.requests if r["cmd"] != "read"]
        assert writes[0] == {"cmd": "slew", "w_per_s": 20.0}
        assert writes[1] == {"cmd": "set", "watts": 180}


def test_slew_resent_after_reconnect():
    ev = Events()
    state = {"dropped": False}

    def reply(request):
        if request["cmd"] == "read" and not state["dropped"]:
            state["dropped"] = True
            return None  # close the connection
        return slew_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02, retry_s=0.02)
        w.request_slew(15)
        w.start()
        try:
            ev.wait("slew_ok")
            ev.wait("service_down")
            ev.wait("service_connected")
            assert ev.wait("slew_ok") == 15.0
        finally:
            w.stop()
        assert [r for r in svc.requests if r["cmd"] == "slew"] == [{"cmd": "slew", "w_per_s": 15.0}] * 2


def test_slew_error_posted_and_connection_kept():
    ev = Events()

    def reply(request):
        if request["cmd"] == "slew":
            return {"ok": False, "error": "invalid_slew", "detail": "w_per_s must be a number 0..1000"}
        return default_reply(request)

    with StubService(reply) as svc:
        w = PowerWorker(ev, port=svc.port, poll_s=0.02)
        w.start()
        try:
            ev.wait("status")
            w.request_slew(5000)
            assert ev.wait("slew_error") == "invalid_slew: w_per_s must be a number 0..1000"
            ev.wait("status")  # still connected and polling
        finally:
            w.stop()
