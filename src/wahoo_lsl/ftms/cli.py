"""wahoo-ftms command line (spec §9.2).

`read` and `set` use only the stdlib client; the `serve` path imports BLE code lazily.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..clock import now_lsl
from . import DEFAULT_HOST, DEFAULT_PORT
from .client import KickrClient, KickrError, Status
from .slew import MAX_SLEW_W_PER_S

log = logging.getLogger("wahoo_lsl.ftms")

# serve
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SETUP = 3
EXIT_RECONNECT = 4
# read / set
EXIT_SERVICE_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_NOT_CONFIRMED = 3
EXIT_INTERRUPTED = 130

POLL_INTERVAL_S = 0.1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wahoo-ftms", description="Control a Wahoo KICKR's ERG target over FTMS.")
    sub = parser.add_subparsers(dest="command", required=True)
    serve_p = sub.add_parser("serve", help="connect to the trainer and accept local client commands")
    serve_p.add_argument("-c", "--config", required=True, type=Path, help="path to FTMS JSON config")
    serve_p.add_argument("--debug", action="store_true", help="log every BLE command, response and notification")
    serve_p.add_argument("--diag-log", type=Path, help="also write diagnostic log lines to this file")
    read_p = sub.add_parser("read", help="print the service's current target and power")
    set_p = sub.add_parser("set", help="read, set a target power, wait for confirmation, read back")
    set_p.add_argument("watts", type=int, help="target power in W")
    set_p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for confirmation (default 5)")
    set_p.add_argument(
        "--hold", type=float, default=0.0, help="keep the target for this many seconds before exiting (default 0)"
    )
    set_p.add_argument(
        "--slew", type=float, default=None,
        help="slew rate in W/s for increases in this run (decreases are always immediate; 0 = no ramp; "
        "default: the service's configured rate)",
    )
    for p in (read_p, set_p):
        p.add_argument("--host", default=DEFAULT_HOST, help=f"service host (default {DEFAULT_HOST})")
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"service port (default {DEFAULT_PORT})")
    return parser


def format_status(st: Status) -> str:
    t = st.target
    target = f"{t.requested_w} W"
    if t.confirmed_w != t.requested_w:
        confirmed = "n/a" if t.confirmed_w is None else f"{t.confirmed_w} W"
        target += f" (confirmed {confirmed}{', pending' if t.pending else ''})"
    if t.setpoint_w is not None and t.setpoint_w != t.requested_w:
        target += f" setpoint={t.setpoint_w} W"
    if st.slew is not None:
        target += f" slew={st.slew.w_per_s:g} W/s"
    if st.power.watts is None:
        power = "n/a"
    elif st.power.age_s is None:
        power = f"{st.power.watts} W"
    else:
        power = f"{st.power.watts} W (age {st.power.age_s:.1f} s)"
    return f"link={st.link} epoch={st.link_epoch} target={target} power={power} last_error={st.last_error or '-'}"


def main(argv: Sequence[str] | None = None, *, backend: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "set" and args.slew is not None and not 0 <= args.slew <= MAX_SLEW_W_PER_S:
        parser.error(f"--slew must be a number 0..{MAX_SLEW_W_PER_S:g}, got {args.slew!r}")
    if args.command == "serve":
        return serve(args.config, debug=args.debug, diag_log=args.diag_log, backend=backend)
    return run_client_command(args)


# ---- client commands -------------------------------------------------------------


def run_client_command(args: argparse.Namespace) -> int:
    try:
        with KickrClient(args.host, args.port) as client:
            if args.command == "read":
                print(format_status(client.read()))
                return EXIT_OK
            return _set(client, args.watts, args.timeout, args.hold, args.slew)
    except KickrError as e:
        print(f"wahoo-ftms: service error {e.code}: {e.detail}", file=sys.stderr)
        return EXIT_SERVICE_ERROR
    except OSError as e:
        print(f"wahoo-ftms: cannot reach the service at {args.host}:{args.port}: {e}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


def _set(client: KickrClient, watts: int, timeout: float, hold: float, slew: float | None = None) -> int:
    if slew is not None:
        client.set_slew_rate(slew)
    print("before: " + format_status(client.read()))
    accepted = client.set_target_power(watts)
    if accepted != watts:
        print(f"note: {watts} W was clamped to {accepted} W")
    deadline = None
    while True:
        status = client.read()
        if deadline is None:  # allow the whole ramp on top of --timeout
            ramp_end = status.target.ramp_end_t_lsl
            ramp_s = 0.0 if ramp_end is None else max(0.0, ramp_end - now_lsl())
            deadline = time.monotonic() + timeout + ramp_s
            if ramp_s > 0:
                print(f"ramping to {accepted} W over {ramp_s:.0f} s")
        if status.target.confirmed_w == accepted and not status.target.pending:
            break
        if time.monotonic() >= deadline:
            print("after:  " + format_status(status))
            print(f"wahoo-ftms: {accepted} W not confirmed within {timeout:g} s (+ ramp time)", file=sys.stderr)
            return EXIT_NOT_CONFIRMED
        time.sleep(POLL_INTERVAL_S)
    print("after:  " + format_status(status))
    if hold > 0:
        print(f"holding {accepted} W for {hold:g} s (Ctrl-C to release; the trainer drops to 0 W on exit)")
        try:
            time.sleep(hold)
        except KeyboardInterrupt:
            pass
    return EXIT_OK


# ---- service -------------------------------------------------------------------


def serve(config_path: Path, *, debug: bool = False, diag_log: Path | None = None, backend: Any = None) -> int:
    from ..cli import setup_logging
    from ..config import ConfigError
    from .config import load_ftms_config

    try:
        setup_logging(debug, diag_log)
    except OSError as e:
        print(f"wahoo-ftms: cannot write --diag-log {diag_log}: {e}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load_ftms_config(config_path)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return EXIT_CONFIG
    try:
        return asyncio.run(_serve(cfg, backend))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Windows Ctrl-C or a second POSIX signal. A second signal can cancel _serve inside its
        # finally, so the spin-down may not have completed.
        log.warning("forced exit; spin-down to 0 W may not have completed")
        return EXIT_OK


async def _serve(cfg: Any, backend: Any = None, stop: asyncio.Event | None = None) -> int:
    from ..ble import BleSetupError, ReconnectExhausted, _unless_stopped
    from .controller import Controller
    from .link import FtmsLink
    from .server import ControlServer

    link = FtmsLink(cfg, backend=backend)
    controller = Controller(cfg, link)
    bind_host = cfg.ftms.bind_host
    server = ControlServer(controller, bind_host, cfg.ftms.port, allowed=cfg.ftms.allowed_hosts)
    try:
        await server.bind()  # before BLE, so "port in use" fails immediately
    except OSError as e:
        log.error("Cannot listen on %s:%d: %s", bind_host, cfg.ftms.port, e)
        return EXIT_CONFIG
    await _warn_unresolvable(cfg.ftms.allowed_hosts)  # after bind, so "port in use" fails at once
    if stop is None:
        stop = asyncio.Event()
        _install_signal_handlers(stop)
    code, reason = EXIT_OK, "shutdown"
    try:
        await _unless_stopped(controller.start(), stop)
        if not stop.is_set():
            await server.start_serving()
            await controller.run(stop)
    except BleSetupError as e:
        log.error("%s", e)
        code = EXIT_SETUP
    except ReconnectExhausted as e:
        log.error("%s", e)
        code = EXIT_RECONNECT
    except Exception:
        log.exception("Unexpected error")
        code, reason = EXIT_ERROR, "internal error"
    finally:
        await server.close()
        await controller.close(reason)
    log.info("Stopped")
    return code


async def _warn_unresolvable(allowed: Sequence[str]) -> None:
    """Typo hint only (connections re-check names, cached for 2 s); bounded, never blocks start-up."""
    from . import allow

    names = [n for n in allowed if allow.normalize_ip(n) is None]
    tasks = {n: asyncio.ensure_future(allow.resolve_host(n)) for n in names}
    if tasks:
        await asyncio.wait(tasks.values(), timeout=allow.CHECK_TIMEOUT_S)
    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            log.warning("allowed host %r does not resolve (timed out)", name)
        elif task.cancelled() or task.exception() is not None or not task.result():
            log.warning("allowed host %r does not resolve", name)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    if sys.platform == "win32":  # add_signal_handler is not implemented on Windows event loops
        return
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set():
            main_task.cancel()  # second signal: stop waiting for a graceful shutdown
        else:
            log.info("Stopping: spinning down to 0 W ...")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)


if __name__ == "__main__":
    sys.exit(main())
