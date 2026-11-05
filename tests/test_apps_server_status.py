from pathlib import Path

import pytest

from wahoo_lsl.apps.server_status import ServerStatus

SRC = Path(__file__).resolve().parents[1] / "src" / "wahoo_lsl"
PREFIX = "2026-10-28 10:00:00,000 INFO    wahoo_lsl.x: "

# (format string as logged, file that logs it, child label, example message, field, expected value)
SOURCES = [
    ("FTMS control ready at 0 W (link_epoch %d)", "ftms/controller.py", "ftms",
     "FTMS control ready at 0 W (link_epoch 1)", "ftms", "ready"),
    ("FTMS connection to %s lost", "ftms/link.py", "ftms", "FTMS connection to KICKR lost", "ftms", "reconnecting"),
    ("FTMS reconnect attempt %d (after %.1fs)", "ftms/controller.py", "ftms",
     "FTMS reconnect attempt 2 (after 1.0s)", "ftms", "reconnecting"),
    ("Client connected: %s", "ftms/server.py", "ftms", "Client connected: ('127.0.0.1', 5000)",
     "client", "('127.0.0.1', 5000)"),
    ("Streaming from %s (%s %s)", "ble.py", "cps", "Streaming from KICKR (mac AA:BB)", "cps", "streaming"),
    ("Reconnected to %s", "ble.py", "cps", "Reconnected to KICKR", "cps", "streaming"),
    ("Connection to %s lost", "ble.py", "cps", "Connection to KICKR lost", "cps", "reconnecting"),
    ("Reconnect attempt %d (after %.1fs)", "ble.py", "cps", "Reconnect attempt 3 (after 5.0s)", "cps", "reconnecting"),
    ("LSL outlet %s: %d channels (%s)", "sinks/lsl_sink.py", "cps",
     "LSL outlet KICKR-CPS-f24bfa: 3 channels (power_w, wheel_revs, wheel_event_t)", "stream", "KICKR-CPS-f24bfa"),
]


@pytest.mark.parametrize("fmt, source, label, message, field, value", SOURCES)
def test_pattern_is_still_logged_and_recognised(fmt, source, label, message, field, value):
    assert fmt in (SRC / source).read_text(encoding="utf-8"), f"log message changed in {source}: update server_status"
    st = ServerStatus()
    assert st.update(f"[{label}] {PREFIX}{message}")
    assert getattr(st, field) == value


def test_client_disconnected_clears_client():
    assert "Client disconnected: %s" in (SRC / "ftms/server.py").read_text(encoding="utf-8")
    st = ServerStatus(client="('127.0.0.1', 5000)")
    assert st.update(f"[ftms] {PREFIX}Client disconnected: ('127.0.0.1', 5000)")
    assert st.client == ""


def test_lines_from_the_other_child_are_ignored():
    st = ServerStatus()
    assert not st.update(f"[cps] {PREFIX}FTMS control ready at 0 W (link_epoch 1)")
    assert not st.update(f"[ftms] {PREFIX}Streaming from KICKR (mac AA:BB)")
    assert (st.ftms, st.cps) == ("starting", "starting")


def test_unrelated_and_malformed_lines_change_nothing():
    st = ServerStatus()
    for line in ("", "no label", f"[ftms] {PREFIX}Accepting client commands on 127.0.0.1:47610", "[server] hello"):
        assert not st.update(line)
    assert st == ServerStatus()


def test_repeated_line_reports_no_change():
    st = ServerStatus()
    line = f"[ftms] {PREFIX}FTMS control ready at 0 W (link_epoch 1)"
    assert st.update(line)
    assert not st.update(line)


def test_starting_and_stopped_keep_the_stream_name():
    st = ServerStatus(ftms="ready", cps="streaming", client="peer", stream="KICKR-CPS-f24bfa")
    st.stopped()
    assert (st.ftms, st.cps, st.client, st.stream) == ("stopped", "stopped", "", "KICKR-CPS-f24bfa")
    st.starting()
    assert (st.ftms, st.cps, st.client) == ("starting", "starting", "")
    st.starting(ftms_enabled=False)
    assert (st.ftms, st.cps) == ("off", "starting")
