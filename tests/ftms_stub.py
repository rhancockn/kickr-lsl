"""Threaded JSON-lines stand-in for the FTMS service (client and CLI tests)."""
from __future__ import annotations

import json
import socketserver
import threading


def snapshot(requested=0, confirmed=0, pending=False, watts=None, link="ready", epoch=1, last_error=None,
             setpoint=None, ramp_end=None, slew=None):
    reply = {
        "ok": True,
        "link": link,
        "link_epoch": epoch,
        "target": {"requested_w": requested, "confirmed_w": confirmed, "pending": pending, "t_lsl": 10.0},
        "power": {"watts": watts, "t_lsl": None if watts is None else 11.0, "age_s": None if watts is None else 0.25},
        "last_error": last_error,
        "last_error_t_lsl": None if last_error is None else 12.0,
    }
    if setpoint is not None or ramp_end is not None:
        reply["target"]["setpoint_w"] = requested if setpoint is None else setpoint
        reply["target"]["ramp_end_t_lsl"] = ramp_end
    if slew is not None:
        reply["slew"] = {"w_per_s": slew, "update_interval_s": 10.0}
    return reply


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for line in self.rfile:
            request = json.loads(line)
            self.server.requests.append(request)
            reply = self.server.reply(request)
            if reply is None:
                return  # close the connection without replying
            self.wfile.write((json.dumps(reply) + "\n").encode())
        # falls through on client EOF
        self.server.disconnects += 1


class StubService:
    """``reply(request) -> dict | None`` decides each answer (None closes the connection)."""

    def __init__(self, reply) -> None:
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.reply = reply
        self._server.requests = []
        self._server.disconnects = 0
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def requests(self) -> list:
        return self._server.requests

    @property
    def disconnects(self) -> int:
        return self._server.disconnects

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "StubService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
