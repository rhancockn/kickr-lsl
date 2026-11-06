"""wahoo-worker server: FTMS and CPS in one loop over one shared BLE link, with fakes."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import threading
import time
import struct
from types import SimpleNamespace

import pytest
from fakes import KICKR, FakeBackend, FakeClient
from ftms_fakes import FakeFtmsBackend, FakeFtmsClient

from wahoo_lsl.apps import server_worker, worker
from wahoo_lsl.apps.server_worker import run_server
from wahoo_lsl.apps.shared_ble import SharedBackend
from wahoo_lsl.ble import ScanResult
from wahoo_lsl.config import parse_config
from wahoo_lsl.cps import CPS_SERVICE_UUID
from wahoo_lsl.ftms import protocol as p
from wahoo_lsl.ftms.config import parse_ftms_config

POWER = struct.pack("<Hh", 0, 200)
READY = "Accepting client commands on"


class ComboServices:
    def __init__(self, client: "ComboClient") -> None:
        self._c = client

    def get_service(self, uuid: str):
        self._c.backend.service_log.append(uuid)
        return self._c.ftms.services.get_service(uuid) or self._c.cps.services.get_service(uuid)

    def get_characteristic(self, uuid: str):
        return self._c.cps.services.get_characteristic(uuid)

    def __iter__(self):
        return iter([SimpleNamespace(uuid=CPS_SERVICE_UUID), SimpleNamespace(uuid=p.FTMS_SERVICE_UUID)])


class ComboClient:
    """One KICKR exposing both CPS and FTMS, built from the existing per-service fakes."""

    def __init__(self, backend: "ComboBackend", device, callback) -> None:
        self.backend = backend
        self._callback = callback
        self.is_connected = False
        self.cps = FakeClient(backend.cps, device, lambda _c: self.drop())
        self.ftms = FakeFtmsClient(backend.ftms, device, lambda _c: None)
        self.services = ComboServices(self)

    def _side(self, char):
        return self.ftms if char.uuid in self.backend.ftms.char_uuids else self.cps

    async def connect(self) -> None:
        await self.cps.connect()
        self.is_connected = True

    async def read_gatt_char(self, char):
        return await self._side(char).read_gatt_char(char)

    async def start_notify(self, char, callback):
        await self._side(char).start_notify(char, callback)

    async def stop_notify(self, char):
        await self._side(char).stop_notify(char)

    async def write_gatt_char(self, char, data, response=False):
        await self.ftms.write_gatt_char(char, data, response)

    async def disconnect(self) -> None:
        was = self.is_connected
        self.is_connected = False
        if was:
            self._callback(self)

    def drop(self) -> None:
        if self.is_connected:
            self.is_connected = False
            self._callback(self)


class ComboBackend:
    def __init__(self, cps_sessions=None) -> None:
        self.cps = FakeBackend(sessions=cps_sessions)
        self.ftms = FakeFtmsBackend()
        self.clients: list[ComboClient] = []
        self.service_log: list[str] = []
        self.device = KICKR
        self._results = [ScanResult(KICKR.name, KICKR.address, -50, True, KICKR, frozenset({p.FTMS_SERVICE_UUID}))]

    async def discover(self, timeout):
        return list(self._results)

    async def find_by_address(self, address, timeout):
        return KICKR if address.lower() == KICKR.address.lower() else None

    def make_client(self, device, callback) -> ComboClient:
        c = ComboClient(self, device, callback)
        self.clients.append(c)
        return c


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cfgs(tmp_path, *, cps_reconnect=False, ftms_reconnect=False):
    log_path = tmp_path / "cps.jsonl"
    cps = parse_config({
        "device": {"name": "KICKR", "scan_timeout_s": 1},
        "measurements": ["power"],
        "log": {"path": str(log_path), "flush_every_n": 1},
        "reconnect": {"enabled": cps_reconnect, "backoff_s": [0.01]},
        "lsl": {"enabled": False},
    })
    ftms = parse_ftms_config({
        "device": {"name": "KICKR", "scan_timeout_s": 1},
        "reconnect": {"enabled": ftms_reconnect, "backoff_s": [0.01]},
        "ftms": {"port": free_port()},
    })
    return cps, ftms, log_path


async def wait(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def streaming(b) -> bool:
    return bool(b.clients) and b.clients[0].cps.notify_callback is not None


def msgs(caplog):
    return [r.getMessage() for r in caplog.records]


def count(caplog, text):
    return sum(text in m for m in msgs(caplog))


def drive(tmp_path, caplog, scenario, *, ftms_enabled=True, backend=None, **cfg_kw):
    """Run run_server with a command queue; scenario(backend, commands, log_path) drives it."""
    caplog.set_level(logging.INFO, "wahoo_lsl")
    cps, ftms, log_path = cfgs(tmp_path, **cfg_kw)
    backend = backend or ComboBackend(cps_sessions=[([POWER], False)])

    async def go():
        commands: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(
            run_server(cps, ftms, SharedBackend(backend), commands, ftms_enabled=ftms_enabled)
        )
        try:
            await scenario(backend, commands, task)
        finally:
            if not task.done():
                commands.put_nowait(None)
                await asyncio.wait_for(task, 10)
        return await task

    return asyncio.run(go()), backend, log_path


def test_ftms_starts_before_cps_and_both_share_one_connect(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        assert b.service_log.index(p.FTMS_SERVICE_UUID) < b.service_log.index(CPS_SERVICE_UUID)
        assert count(caplog, READY) == 1
        commands.put_nowait("stop")
        assert await asyncio.wait_for(task, 10) == 0

    _, b, _ = drive(tmp_path, caplog, scenario)
    assert len(b.clients) == 1


def test_no_ftms_runs_cps_only(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        assert p.FTMS_SERVICE_UUID not in b.service_log
        assert count(caplog, READY) == 0
        commands.put_nowait("stop")

    code, *_ = drive(tmp_path, caplog, scenario, ftms_enabled=False)
    assert code == 0
    assert count(caplog, "ftms exited") == 0
    assert count(caplog, "cps exited with code 0") == 1


def test_ftms_off_spins_down_keeps_cps_then_on_again(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        combo = b.clients[0]
        commands.put_nowait("ftms off")
        await wait(lambda: count(caplog, "ftms exited with code 0") == 1)
        assert b.ftms.writes[-1] == b"\x05\x00\x00"  # 0 W spin-down ran before the detach
        assert combo.is_connected and not task.done()
        combo.cps.notify_callback(None, bytearray(POWER))  # CPS still streaming
        commands.put_nowait("ftms on")
        await wait(lambda: count(caplog, READY) == 2)
        assert len(b.clients) == 1
        commands.put_nowait("stop")

    _, b, log_path = drive(tmp_path, caplog, scenario)
    assert len(b.clients) == 1
    assert log_path.read_text(encoding="utf-8").count('"type":"sample"') == 2


@pytest.mark.parametrize("command", ["stop", None])  # None = stdin EOF
def test_stop_and_eof_stop_ftms_before_cps(tmp_path, caplog, command):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        commands.put_nowait(command)

    code, b, _ = drive(tmp_path, caplog, scenario)
    m = msgs(caplog)
    assert code == 0
    ftms_exit = next(i for i, x in enumerate(m) if "ftms exited with code 0" in x)
    cps_exit = next(i for i, x in enumerate(m) if "cps exited with code 0" in x)
    assert ftms_exit < cps_exit
    assert b.ftms.writes[-1] == b"\x05\x00\x00"


def test_stop_before_cps_started_still_exits(tmp_path, caplog):
    async def scenario(b, commands, task):
        commands.put_nowait("stop")

    code, *_ = drive(tmp_path, caplog, scenario)
    assert code == 0
    assert count(caplog, "ftms exited with code 0") == 1


def test_drop_with_both_attached_reconnects_both_on_one_connect(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        await wait(lambda: p.CONTROL_POINT_UUID in b.clients[0].ftms.callbacks)
        b.clients[0].drop()
        await wait(lambda: len(b.clients) == 2)
        c = b.clients[1]
        await wait(lambda: c.is_connected and p.CONTROL_POINT_UUID in c.ftms.callbacks and c.cps.notify_callback)
        await asyncio.sleep(0.1)
        assert len(b.clients) == 2
        assert count(caplog, "lost") >= 2
        commands.put_nowait("stop")

    code, *_ = drive(tmp_path, caplog, scenario, cps_reconnect=True, ftms_reconnect=True)
    assert code == 0


def test_ftms_gives_up_cps_continues(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        await wait(lambda: p.CONTROL_POINT_UUID in b.clients[0].ftms.callbacks)
        b.clients[0].drop()
        await wait(lambda: count(caplog, "ftms exited with code 4") == 1)
        await wait(lambda: len(b.clients) == 2 and b.clients[1].is_connected)
        assert not task.done()
        commands.put_nowait("stop")

    code, *_ = drive(tmp_path, caplog, scenario, cps_reconnect=True, ftms_reconnect=False)
    assert code == 0


def test_cps_ending_stops_ftms_and_returns_its_code(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        b.clients[0].drop()  # CPS reconnect disabled -> exit 4
        assert await asyncio.wait_for(task, 10) == 4

    drive(tmp_path, caplog, scenario)
    m = msgs(caplog)
    assert any("cps exited with code 4" in x for x in m)
    assert any("ftms exited with code" in x for x in m)


def test_unknown_command_warns(tmp_path, caplog):
    async def scenario(b, commands, task):
        commands.put_nowait("dance")
        await wait(lambda: count(caplog, "dance") == 1)
        commands.put_nowait("stop")

    drive(tmp_path, caplog, scenario, ftms_enabled=False)
    assert [r.levelno for r in caplog.records if "dance" in r.getMessage()] == [logging.WARNING]


def test_ftms_on_during_shutdown_is_not_dropped(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        await wait(lambda: count(caplog, READY) == 1)
        commands.put_nowait("ftms off")
        commands.put_nowait("ftms on")  # arrives while the first FTMS is still spinning down
        await wait(lambda: count(caplog, READY) == 2)
        commands.put_nowait("stop")

    drive(tmp_path, caplog, scenario)
    assert count(caplog, "ftms exited with code 0") == 2


def test_last_of_on_off_wins(tmp_path, caplog):
    async def scenario(b, commands, task):
        await wait(lambda: count(caplog, READY) == 1)
        for c in ("ftms off", "ftms on", "ftms off"):
            commands.put_nowait(c)
        await wait(lambda: count(caplog, "ftms exited with code 0") == 1)
        await asyncio.sleep(0.2)
        assert count(caplog, READY) == 1
        commands.put_nowait("stop")

    drive(tmp_path, caplog, scenario)


def test_cps_close_does_not_block_the_loop(tmp_path, caplog, monkeypatch):
    real_close = server_worker.Pipeline.close

    def slow_close(self, timeout):
        time.sleep(0.4)
        return real_close(self, timeout=timeout)

    monkeypatch.setattr(server_worker.Pipeline, "close", slow_close)
    gaps = []

    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        loop = asyncio.get_running_loop()

        async def ticker():
            last = loop.time()
            while True:
                await asyncio.sleep(0.01)
                gaps.append(loop.time() - last)
                last = loop.time()

        t = asyncio.create_task(ticker())
        b.clients[0].drop()  # CPS reconnect disabled: CPS ends, closes its pipeline
        await asyncio.wait_for(task, 10)
        t.cancel()

    drive(tmp_path, caplog, scenario)
    assert max(gaps) < 0.2


def test_ftms_stop_failure_still_stops_cps(tmp_path, caplog, monkeypatch):
    async def boom(self):
        raise RuntimeError("ftms stop failed")

    monkeypatch.setattr(server_worker._Ftms, "stop", boom)

    async def scenario(b, commands, task):
        await wait(lambda: streaming(b))
        commands.put_nowait("stop")

    code, *_ = drive(tmp_path, caplog, scenario)
    assert code == 0
    assert count(caplog, "cps exited with code 0") == 1
    assert any(r.levelno == logging.ERROR and "FTMS did not stop" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "name,label",
    [
        ("wahoo_lsl.ftms", "ftms"),
        ("wahoo_lsl.ftms.link", "ftms"),
        ("wahoo_lsl.apps.server_worker", "worker"),
        ("bleak.backends.winrt", "ble"),
        ("wahoo_lsl", "cps"),
        ("wahoo_lsl.ble", "cps"),
        ("asyncio", "cps"),
    ],
)
def test_label_mapping(name, label):
    assert server_worker.label_for(name) == label


class GatedStdin:
    """Sends `ftms on`, waits until FTMS accepts clients, then `stop`."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self._lines = iter(["ftms on\n", "stop\n"])

        class Hook(logging.Handler):
            def emit(_h, record):
                if READY in record.getMessage():
                    self.ready.set()

        self.hook = Hook()
        logging.getLogger("wahoo_lsl.ftms").addHandler(self.hook)

    def readline(self) -> str:
        line = next(self._lines, "")
        if line == "stop\n":
            assert self.ready.wait(5)
        return line


def test_main_stdout_format_replaces_root_handlers_and_reports(tmp_path, capsys):
    cps, ftms, _ = cfgs(tmp_path)
    cps_path = tmp_path / "c.json"
    ftms_path = tmp_path / "f.json"
    cps_path.write_text(json.dumps({
        "device": {"name": "KICKR", "scan_timeout_s": 1}, "measurements": ["power"],
        "log": {"path": str(tmp_path / "l.jsonl")}, "reconnect": {"enabled": False},
        "lsl": {"enabled": False},
    }), encoding="utf-8")
    ftms_path.write_text(json.dumps({
        "device": {"name": "KICKR", "scan_timeout_s": 1}, "ftms": {"port": ftms.ftms.port},
    }), encoding="utf-8")
    stdin = GatedStdin()
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    junk = logging.NullHandler()
    root.addHandler(junk)
    try:
        code = server_worker.main(
            ["-c", str(cps_path), "-f", str(ftms_path), "--no-ftms"],
            backend=ComboBackend(cps_sessions=[([POWER], False)]),
            stdin=stdin,
        )
        assert junk not in root.handlers
    finally:
        logging.getLogger("wahoo_lsl.ftms").removeHandler(stdin.hook)
        root.handlers[:] = saved
        root.setLevel(level)
    out = capsys.readouterr().out
    assert code == 0
    assert "[worker] " in out and "ftms exited with code 0" in out and "cps exited with code 0" in out
    assert any(line.startswith("[ftms] ") and READY in line for line in out.splitlines())
    assert any(line.startswith("[cps] ") for line in out.splitlines())


def test_main_config_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert server_worker.main(["-c", str(bad), "-f", str(bad)], stdin=io.StringIO("")) == 2
    assert capsys.readouterr().out.startswith("[worker] config error: ")


def test_worker_dispatches_server(monkeypatch):
    calls = []
    monkeypatch.setattr(server_worker, "main", lambda argv: calls.append(argv) or 0)
    assert worker.main(["server", "-c", "a"]) == 0
    assert calls == [["-c", "a"]]
