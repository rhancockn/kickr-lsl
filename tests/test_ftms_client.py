import fractions
import math
import socket
import subprocess
import sys
import time

import pytest
from ftms_stub import StubService, connect_reply, snapshot

from wahoo_lsl.ftms.client import ConnectInfo, KickrClient, KickrError, PowerReading, SlewState, Status, TargetState


def wait_for(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def default_reply(request):
    if request["cmd"] == "set":
        return {"ok": True, "target_w": request["watts"], "clamped": False}
    return snapshot(requested=150, confirmed=140, pending=True, watts=143)


def test_set_and_reads():
    with StubService(default_reply) as svc, KickrClient(port=svc.port) as client:
        assert client.set_target_power(150) == 150
        status = client.read()
        assert status == Status(
            link="ready",
            link_epoch=1,
            target=TargetState(150, 140, True, 10.0),
            power=PowerReading(143, 11.0, 0.25),
            last_error=None,
            last_error_t_lsl=None,
        )
        assert client.read_target_power() == TargetState(150, 140, True, 10.0)
        assert client.read_power() == PowerReading(143, 11.0, 0.25)
        assert svc.requests[0] == {"cmd": "set", "watts": 150}


def test_clamped_value_returned():
    with StubService(lambda r: {"ok": True, "target_w": 400, "clamped": True}) as svc:
        with KickrClient(port=svc.port) as client:
            assert client.set_target_power(450) == 400


def test_error_reply_raises_kickr_error():
    with StubService(lambda r: {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}) as svc:
        with KickrClient(port=svc.port) as client:
            with pytest.raises(KickrError) as info:
                client.set_target_power(100)
    assert (info.value.code, info.value.detail) == ("link_down", "BLE link is reconnecting")


def test_close_disconnects_and_is_idempotent():
    with StubService(default_reply) as svc:
        client = KickrClient(port=svc.port)
        client.read()
        client.close()
        client.close()
        wait_for(lambda: svc.disconnects == 1)


def test_service_closing_connection_raises_connection_error():
    with StubService(lambda r: None) as svc, KickrClient(port=svc.port) as client:
        with pytest.raises(ConnectionError):
            client.read()


def test_connection_refused_is_oserror():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(OSError):
        KickrClient(port=port)


def test_client_module_imports_only_stdlib():
    code = (
        "import sys, wahoo_lsl.ftms.client; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in {'bleak', 'pylsl'}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_timeout_breaks_client_and_later_calls_do_not_send():
    import threading

    gate = threading.Event()

    def never_replies(request):
        gate.wait(5.0)
        return None

    with StubService(never_replies) as svc:
        with KickrClient(port=svc.port, timeout_s=0.1) as client:
            with pytest.raises(OSError):
                client.set_target_power(100)
            with pytest.raises(ConnectionError, match="closed"):
                client.set_target_power(200)
            with pytest.raises(ConnectionError, match="closed"):
                client.read()
            gate.set()
            assert svc.requests == [{"cmd": "set", "watts": 100}]


def test_malformed_reply_breaks_client():
    with StubService(lambda r: {"ok": True}) as svc:  # a read reply with no target/power keys
        with KickrClient(port=svc.port) as client:
            with pytest.raises(KeyError):
                client.read()
            with pytest.raises(ConnectionError, match="closed"):
                client.read()
        assert len(svc.requests) == 1


def test_non_json_reply_breaks_client():
    import json as _json

    # Raw stub: answer with a line that is not JSON.
    import socketserver
    import threading

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            self.server.n += 1
            self.rfile.readline()  # connect
            self.wfile.write((_json.dumps(connect_reply()) + "\n").encode())
            self.rfile.readline()
            self.wfile.write(b"not json\n")
            self.rfile.readline()

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    srv.n = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with KickrClient(port=srv.server_address[1]) as client:
            with pytest.raises(_json.JSONDecodeError):
                client.read()
            with pytest.raises(ConnectionError, match="closed"):
                client.read()
        assert srv.n == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_numeric_watts_accepted_and_rounded():
    class NpInt:
        def __index__(self):
            return 120

    with StubService(default_reply) as svc, KickrClient(port=svc.port) as client:
        assert client.set_target_power(NpInt()) == 120
        assert client.set_target_power(150.6) == 151
        assert client.set_target_power(fractions.Fraction(301, 2)) == 150  # any numbers.Real; half to even
        assert [r["watts"] for r in svc.requests] == [120, 151, 150]
        assert all(type(r["watts"]) is int for r in svc.requests)


@pytest.mark.parametrize(
    "value, error",
    [(True, TypeError), (False, TypeError), ("150", TypeError), (None, TypeError), (1 + 2j, TypeError),
     (float("nan"), ValueError), (float("inf"), ValueError)],
)
def test_non_numeric_watts_rejected_without_sending(value, error):
    with StubService(default_reply) as svc, KickrClient(port=svc.port) as client:
        with pytest.raises(error):
            client.set_target_power(value)
        assert client.read().link == "ready"  # still usable; nothing was sent
        assert [r["cmd"] for r in svc.requests] == ["read"]


def slew_reply(request):
    if request["cmd"] == "slew":
        return {"ok": True, "slew_w_per_s": float(request["w_per_s"])}
    return snapshot(requested=250, confirmed=200, pending=True, setpoint=210, ramp_end=30.0, slew=10.0)


def test_set_slew_rate_round_trip():
    with StubService(slew_reply) as svc, KickrClient(port=svc.port) as client:
        assert client.set_slew_rate(20) == 20.0
        assert client.set_slew_rate(2.5) == 2.5
        assert svc.requests[:2] == [{"cmd": "slew", "w_per_s": 20}, {"cmd": "slew", "w_per_s": 2.5}]


def test_set_slew_rate_accepts_numpy_like_numbers():
    with StubService(slew_reply) as svc, KickrClient(port=svc.port) as client:
        assert client.set_slew_rate(fractions.Fraction(5, 2)) == 2.5
        assert svc.requests[0] == {"cmd": "slew", "w_per_s": 2.5}


@pytest.mark.parametrize("bad, exc", [(True, TypeError), ("10", TypeError), (None, TypeError), (math.nan, ValueError), (math.inf, ValueError)])
def test_set_slew_rate_rejects_without_sending(bad, exc):
    with StubService(slew_reply) as svc, KickrClient(port=svc.port) as client:
        with pytest.raises(exc):
            client.set_slew_rate(bad)
        assert client.set_slew_rate(1) == 1.0  # still usable
        assert svc.requests == [{"cmd": "slew", "w_per_s": 1}]


def test_invalid_slew_reply_raises_kickr_error():
    with StubService(lambda r: {"ok": False, "error": "invalid_slew", "detail": "w_per_s must be ..."}) as svc:
        with KickrClient(port=svc.port) as client:
            with pytest.raises(KickrError) as info:
                client.set_slew_rate(5000)
    assert info.value.code == "invalid_slew"


def test_read_parses_ramp_and_slew_fields():
    with StubService(slew_reply) as svc, KickrClient(port=svc.port) as client:
        status = client.read()
    assert status.target == TargetState(250, 200, True, 10.0, 210, 30.0)
    assert status.slew == SlewState(10.0, 10.0)


def test_read_from_older_service_leaves_new_fields_none():
    with StubService(default_reply) as svc, KickrClient(port=svc.port) as client:
        status = client.read()
    assert (status.target.setpoint_w, status.target.ramp_end_t_lsl, status.slew) == (None, None, None)


# ---- connect / disconnect -------------------------------------------------------------


def test_constructor_sends_connect_and_records_service_view():
    with StubService(default_reply, lambda r: connect_reply("10.0.0.23", "reconnecting", 4)) as svc:
        with KickrClient(port=svc.port) as client:
            assert svc.connects == [{"cmd": "connect"}]
            assert (client.peer, client.link, client.link_epoch) == ("10.0.0.23", "reconnecting", 4)


@pytest.mark.parametrize("code", ["not_allowed", "busy"])
def test_connect_refusal_raises_and_closes_socket(code):
    err = {"ok": False, "error": code, "detail": "nope"}
    with StubService(default_reply, lambda r: err) as svc:
        with pytest.raises(KickrError) as info:
            KickrClient(port=svc.port)
        assert (info.value.code, info.value.detail) == (code, "nope")
        wait_for(lambda: svc.disconnects == 1)


def test_connect_rechecks_and_updates_fields():
    replies = iter([connect_reply("10.0.0.23", "ready", 1), connect_reply("10.0.0.23", "reconnecting", 2)])
    with StubService(default_reply, lambda r: next(replies)) as svc, KickrClient(port=svc.port) as client:
        assert client.connect() == ConnectInfo("10.0.0.23", "reconnecting", 2)
        assert (client.peer, client.link, client.link_epoch) == ("10.0.0.23", "reconnecting", 2)
        assert len(svc.connects) == 2


def test_disconnect_returns_none_and_closes():
    with StubService(default_reply) as svc:
        with KickrClient(port=svc.port) as client:
            assert client.disconnect() is None
            assert svc.requests == [{"cmd": "disconnect"}]
            wait_for(lambda: svc.disconnects == 1)
            client.close()  # no-ops after disconnect
            with pytest.raises(ConnectionError):
                client.read()
        assert svc.disconnects == 1
