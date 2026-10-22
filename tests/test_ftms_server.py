import asyncio
import json

import pytest

from wahoo_lsl.ftms.controller import LinkDown
from wahoo_lsl.ftms.server import MAX_LINE_BYTES, ControlServer

SNAPSHOT = {
    "link": "ready",
    "link_epoch": 1,
    "target": {"requested_w": 0, "confirmed_w": 0, "pending": False, "t_lsl": 1.0},
    "power": {"watts": None, "t_lsl": None, "age_s": None},
    "last_error": None,
    "last_error_t_lsl": None,
}


class StubController:
    def __init__(self) -> None:
        self.requests: list[int] = []
        self.spin_downs: list[str] = []
        self.slews: list[float] = []
        self.calls: list[str] = []
        self.link_down = False

    def request_target(self, watts: int):
        if self.link_down:
            raise LinkDown("BLE link is reconnecting")
        self.requests.append(watts)
        accepted = min(watts, 1000)
        return accepted, accepted != watts

    def snapshot(self):
        return SNAPSHOT

    def set_slew_rate(self, w_per_s):
        self.slews.append(w_per_s)
        return float(w_per_s)

    def reset_slew(self) -> None:
        self.calls.append("reset_slew")

    async def spin_down(self, reason: str) -> bool:
        self.calls.append("spin_down")
        self.spin_downs.append(reason)
        return True


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


async def serving(ctl):
    server = ControlServer(ctl, "127.0.0.1", 0)
    await server.bind()
    await server.start_serving()
    return server


async def ask(reader, writer, request):
    raw = request if isinstance(request, bytes) else json.dumps(request).encode()
    writer.write(raw + b"\n")
    await writer.drain()
    return json.loads(await reader.readline())


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 5.0))


def test_set_and_read_round_trip():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await ask(reader, writer, {"cmd": "set", "watts": 150}) == {"ok": True, "target_w": 150, "clamped": False}
        assert await ask(reader, writer, {"cmd": "set", "watts": 1200}) == {"ok": True, "target_w": 1000, "clamped": True}
        assert await ask(reader, writer, {"cmd": "read"}) == {"ok": True, **SNAPSHOT}
        writer.close()
        await server.close()

    run(scenario)
    assert ctl.requests == [150, 1200]


@pytest.mark.parametrize(
    "request_bytes, code",
    [
        (b"not json", "bad_request"),
        (b"[1, 2]", "bad_request"),
        (b'{"cmd": "dance"}', "bad_request"),
        (b'{"cmd": "set"}', "invalid_watts"),
        (b'{"cmd": "set", "watts": -1}', "invalid_watts"),
        (b'{"cmd": "set", "watts": 1.5}', "invalid_watts"),
        (b'{"cmd": "set", "watts": true}', "invalid_watts"),
        (b'{"cmd": "set", "watts": "100"}', "invalid_watts"),
    ],
)
def test_errors_keep_connection_usable(request_bytes, code):
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, request_bytes)
        assert reply["ok"] is False and reply["error"] == code and reply["detail"]
        assert (await ask(reader, writer, {"cmd": "read"}))["ok"] is True
        writer.close()
        await server.close()

    run(scenario)
    assert ctl.requests == []


def test_link_down_reply():
    ctl = StubController()
    ctl.link_down = True

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, {"cmd": "set", "watts": 100})
        assert reply == {"ok": False, "error": "link_down", "detail": "BLE link is reconnecting"}
        writer.close()
        await server.close()

    run(scenario)


def test_blank_lines_ignored():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"\n  \n")
        assert (await ask(reader, writer, {"cmd": "read"}))["ok"] is True
        writer.close()
        await server.close()

    run(scenario)


def test_too_long_line_rejected_and_closed():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert reply["error"] == "bad_request"
        assert await reader.read() == b""
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        writer.close()
        await server.close()

    run(scenario)


def test_second_client_busy_first_unaffected():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        assert (await ask(r1, w1, {"cmd": "read"}))["ok"]
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
        reply = json.loads(await r2.readline())
        assert reply["error"] == "busy"
        assert await r2.read() == b""
        assert (await ask(r1, w1, {"cmd": "set", "watts": 100}))["ok"]
        assert ctl.spin_downs == []  # rejecting the second client must not spin down
        w2.close()
        w1.close()
        await server.close()

    run(scenario)


def test_client_eof_triggers_spin_down_and_next_client_accepted():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(r1, w1, {"cmd": "set", "watts": 100})
        w1.close()
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
        assert (await ask(r2, w2, {"cmd": "read"}))["ok"]
        w2.close()
        await server.close()

    run(scenario)


def test_client_reset_triggers_spin_down():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(r1, w1, {"cmd": "set", "watts": 100})
        w1.transport.abort()  # drop without a clean shutdown, like a killed process
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        await server.close()

    run(scenario)


def test_bind_on_used_port_raises_oserror():
    ctl = StubController()

    async def scenario():
        first = await serving(ctl)
        second = ControlServer(ctl, "127.0.0.1", first.port)
        with pytest.raises(OSError):
            await second.bind()
        await first.close()

    run(scenario)


def test_close_with_active_client_does_not_spin_down_from_handler():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(reader, writer, {"cmd": "read"})
        await server.close()
        assert await reader.read() == b""
        writer.close()

    run(scenario)
    assert ctl.spin_downs == []  # shutdown spins down via Controller.close, not the handler


def test_disconnect_spin_down_starts_before_wait_closed(monkeypatch):
    # A reconnecting client's set must not be overtaken by the old client's spin-down, so the
    # spin-down has to be issued synchronously after _active is cleared, not after wait_closed.
    ctl = StubController()
    gate = asyncio.Event()
    real_wait_closed = asyncio.StreamWriter.wait_closed
    blocked: list[bool] = []

    async def gated_wait_closed(self):
        if not blocked:
            blocked.append(True)
            await gate.wait()
        await real_wait_closed(self)

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", gated_wait_closed)

    async def scenario():
        server = await serving(ctl)
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(r1, w1, {"cmd": "set", "watts": 100})
        w1.close()
        await until(lambda: ctl.spin_downs == ["client disconnected"])
        assert not gate.is_set()  # spin-down began while the old connection was still closing
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(r2, w2, {"cmd": "set", "watts": 150})
        assert reply["ok"] and reply["target_w"] == 150
        gate.set()
        w2.close()
        await server.close()

    run(scenario)
    assert ctl.requests == [100, 150]


def test_slew_command_round_trip():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await ask(reader, writer, {"cmd": "slew", "w_per_s": 20}) == {"ok": True, "slew_w_per_s": 20.0}
        assert await ask(reader, writer, {"cmd": "slew", "w_per_s": 2.5}) == {"ok": True, "slew_w_per_s": 2.5}
        writer.close()
        await server.close()

    run(scenario)
    assert ctl.slews == [20, 2.5]


@pytest.mark.parametrize("value", [-1, 1001, True, "10", None, [1]])
def test_slew_command_rejects_bad_values(value):
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, {"cmd": "slew", "w_per_s": value})
        assert reply["ok"] is False and reply["error"] == "invalid_slew"
        assert (await ask(reader, writer, {"cmd": "read"}))["ok"] is True  # connection still usable
        writer.close()
        await server.close()

    run(scenario)
    assert ctl.slews == []


def test_slew_command_rejects_non_finite():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, b'{"cmd": "slew", "w_per_s": NaN}')
        assert reply["error"] == "invalid_slew"
        writer.close()
        await server.close()

    run(scenario)


def test_disconnect_resets_slew_before_spin_down():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        await ask(reader, writer, {"cmd": "slew", "w_per_s": 20})
        writer.close()
        await until(lambda: ctl.spin_downs)
        await server.close()

    run(scenario)
    assert ctl.calls == ["reset_slew", "spin_down"]


def test_unknown_command_lists_slew():
    ctl = StubController()

    async def scenario():
        server = await serving(ctl)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        reply = await ask(reader, writer, {"cmd": "nope"})
        assert "'slew'" in reply["detail"]
        writer.close()
        await server.close()

    run(scenario)
