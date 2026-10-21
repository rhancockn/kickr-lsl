import socket
import subprocess
import sys
import time

import pytest
from ftms_stub import StubService, snapshot

from wahoo_lsl.ftms.client import KickrClient, KickrError, PowerReading, Status, TargetState


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
