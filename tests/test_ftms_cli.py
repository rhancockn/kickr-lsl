import asyncio
import json
import logging
import os
import signal
import socket
import sys
from importlib.metadata import entry_points

import pytest
from ftms_fakes import FakeFtmsBackend
from ftms_stub import StubService, snapshot

from wahoo_lsl.ftms.cli import _serve, build_parser, format_status, main
from wahoo_lsl.ftms.client import KickrClient, PowerReading, Status, TargetState
from wahoo_lsl.ftms.config import parse_ftms_config


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_cfg(tmp_path, port, **extra):
    path = tmp_path / "ftms.json"
    path.write_text(
        json.dumps({"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01]}, "ftms": {"port": port, **extra}}),
        encoding="utf-8",
    )
    return path


def test_console_script_registered():
    names = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert names.get("wahoo-ftms") == "wahoo_lsl.ftms.cli:main"


def test_parser():
    args = build_parser().parse_args(["set", "150", "--hold", "2"])
    assert (args.command, args.watts, args.hold, args.timeout, args.port) == ("set", 150, 2.0, 5.0, 47610)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["set", "abc"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve"])  # --config required


def test_format_status():
    st = Status("ready", 2, TargetState(150, 100, True, 1.0), PowerReading(143, 2.0, 0.42), "boom", 3.0)
    assert format_status(st) == (
        "link=ready epoch=2 target=150 W (confirmed 100 W, pending) power=143 W (age 0.4 s) last_error=boom"
    )
    st2 = Status("reconnecting", 1, TargetState(0, None, False, None), PowerReading(None, None, None), None, None)
    assert format_status(st2) == "link=reconnecting epoch=1 target=0 W (confirmed n/a) power=n/a last_error=-"
    st3 = Status("ready", 1, TargetState(0, 0, False, 1.0), PowerReading(None, None, None), None, None)
    assert format_status(st3) == "link=ready epoch=1 target=0 W power=n/a last_error=-"


def test_read(capsys):
    with StubService(lambda r: snapshot(watts=143)) as svc:
        assert main(["read", "--port", str(svc.port)]) == 0
    assert "link=ready" in capsys.readouterr().out


def confirming_service():
    state = {"target": 0}

    def reply(request):
        if request["cmd"] == "set":
            state["target"] = min(request["watts"], 400)
            return {"ok": True, "target_w": state["target"], "clamped": state["target"] != request["watts"]}
        return snapshot(requested=state["target"], confirmed=state["target"])

    return StubService(reply)


def test_set_confirmed(capsys):
    with confirming_service() as svc:
        assert main(["set", "150", "--port", str(svc.port)]) == 0
        assert [r["cmd"] for r in svc.requests] == ["read", "set", "read"]
    out = capsys.readouterr().out
    assert "before:" in out and "after:" in out and "target=150 W" in out


def test_set_clamped_note_and_hold(capsys):
    with confirming_service() as svc:
        assert main(["set", "450", "--port", str(svc.port), "--hold", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "clamped to 400 W" in out
    assert "holding 400 W" in out


def test_set_not_confirmed_times_out(capsys):
    def reply(request):
        if request["cmd"] == "set":
            return {"ok": True, "target_w": request["watts"], "clamped": False}
        return snapshot(requested=150, confirmed=0, pending=True)

    with StubService(reply) as svc:
        assert main(["set", "150", "--port", str(svc.port), "--timeout", "0.2"]) == 3
    assert "not confirmed" in capsys.readouterr().err


def test_set_service_error(capsys):
    def reply(request):
        if request["cmd"] == "set":
            return {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}
        return snapshot(link="reconnecting")

    with StubService(reply) as svc:
        assert main(["set", "150", "--port", str(svc.port)]) == 1
    assert "link_down" in capsys.readouterr().err


def test_unreachable_service(capsys):
    assert main(["read", "--port", str(free_port())]) == 2
    assert "cannot reach" in capsys.readouterr().err


def test_serve_bad_config(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"ftms": {}}), encoding="utf-8")
    assert main(["serve", "-c", str(path)], backend=FakeFtmsBackend()) == 2


def test_serve_port_in_use(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        backend = FakeFtmsBackend()
        assert main(["serve", "-c", str(write_cfg(tmp_path, busy.getsockname()[1]))], backend=backend) == 2
    assert backend.clients == []  # failed before touching BLE


def test_serve_setup_failure(tmp_path):
    backend = FakeFtmsBackend(has_ftms=False)
    assert main(["serve", "-c", str(write_cfg(tmp_path, free_port()))], backend=backend) == 3


def test_end_to_end_set_read_disconnect_shutdown():
    port = free_port()
    cfg = parse_ftms_config(
        {"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01]}, "ftms": {"port": port, "slew_w_per_s": 0}}
    )
    backend = FakeFtmsBackend()

    async def until(cond, timeout=3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not cond():
            if loop.time() > deadline:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(0.005)

    async def connect():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while True:
            try:
                return await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.01)

    async def ask(reader, writer, obj):
        writer.write(json.dumps(obj).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(_serve(cfg, backend, stop))
        reader, writer = await connect()
        assert await ask(reader, writer, {"cmd": "set", "watts": 150}) == {"ok": True, "target_w": 150, "clamped": False}

        for _ in range(300):
            if (await ask(reader, writer, {"cmd": "read"}))["target"]["confirmed_w"] == 150:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("150 W never confirmed")
        writer.close()  # client disconnect -> spin-down
        await until(lambda: backend.writes[-1] == b"\x05\x00\x00" and b"\x05\x96\x00" in backend.writes)
        stop.set()
        return await asyncio.wait_for(task, 5.0)

    assert asyncio.run(scenario()) == 0
    writes = backend.writes
    assert writes[:3] == [b"\x00", b"\x07", b"\x05\x00\x00"]
    after_150 = writes[writes.index(b"\x05\x96\x00") + 1 :]
    assert after_150 and all(w == b"\x05\x00\x00" for w in after_150)
    assert backend.clients[0].is_connected is False


def _cfg(port, **extra):
    return parse_ftms_config(
        {"device": {"name": "KICKR"}, "reconnect": {"backoff_s": [0.01], **extra}, "ftms": {"port": port}}
    )


async def _wait(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def _setup_done(backend, port):
    await _wait(lambda: len(backend.writes) >= 3)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while True:
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            return w
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.01)


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) terminates the process on Windows")
def test_serve_sigint_graceful_shutdown():
    port = free_port()
    backend = FakeFtmsBackend()

    async def scenario():
        task = asyncio.create_task(_serve(_cfg(port), backend))
        w = await _setup_done(backend, port)
        w.close()
        await asyncio.sleep(0.1)  # let the client-disconnect spin-down settle
        os.kill(os.getpid(), signal.SIGINT)
        return await asyncio.wait_for(task, 5.0)

    assert asyncio.run(scenario()) == 0
    assert backend.writes[-1] == b"\x05\x00\x00"
    assert backend.clients[0].is_connected is False
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_serve_reconnect_disabled_exits_4():
    port = free_port()
    backend = FakeFtmsBackend()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(_serve(_cfg(port, enabled=False), backend, stop))
        w = await _setup_done(backend, port)
        backend.clients[0].drop()
        try:
            return await asyncio.wait_for(task, 5.0)
        finally:
            w.close()

    assert asyncio.run(scenario()) == 4


def test_serve_unexpected_error_exits_1_and_spins_down(monkeypatch, caplog):
    from wahoo_lsl.ftms.controller import Controller

    async def boom(self, stop):
        raise RuntimeError("boom")

    monkeypatch.setattr(Controller, "run", boom)
    backend = FakeFtmsBackend()

    async def scenario():
        return await asyncio.wait_for(_serve(_cfg(free_port()), backend, asyncio.Event()), 5.0)

    with caplog.at_level(logging.INFO):
        assert asyncio.run(scenario()) == 1
    assert backend.writes[:3] == [b"\x00", b"\x07", b"\x05\x00\x00"]
    assert backend.writes[-1] == b"\x05\x00\x00" and len(backend.writes) > 3
    assert "spin-down (internal error)" in caplog.text


@pytest.mark.parametrize("argv", [["read"], ["set", "150"]])
def test_client_ctrl_c_returns_130(monkeypatch, argv):
    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(KickrClient, "read", interrupt)
    with StubService(lambda r: snapshot()) as svc:
        assert main([*argv, "--port", str(svc.port)]) == 130
