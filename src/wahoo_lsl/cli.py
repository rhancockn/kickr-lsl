"""Command-line entry point (spec §11)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .ble import (
    COMMS_ERRORS,
    BleBackend,
    BleSetupError,
    CpsClient,
    DeviceInfo,
    ReconnectExhausted,
    ScanResult,
    scan_cps,
)
from .config import Config, ConfigError, load_config
from .cps import DISTRIBUTED_SYSTEM_SUPPORT, FEATURE_BITS
from .pipeline import Pipeline, Sink, make_event
from .sinks import ConsoleSink, LogSink, format_log_path
from .warn import WarnOnce

log = logging.getLogger("wahoo_lsl")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SETUP = 3
EXIT_RECONNECT = 4
EXIT_INTERRUPTED = 130

CLOSE_TIMEOUT_S = 5.0  # per-thread join timeout when closing the pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wahoo-lsl", description="Stream and log Cycling Power Service data from a Wahoo KICKR."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="list nearby devices advertising the Cycling Power Service")
    scan.add_argument("--timeout", type=float, default=10.0, help="scan duration in seconds (default 10)")
    features = sub.add_parser("list-features", help="connect and print the device's cycling power features")
    run = sub.add_parser("run", help="stream and log CP Measurement notifications")
    for p in (features, run):
        p.add_argument("-c", "--config", required=True, type=Path, help="path to JSON config")
    for p in (scan, features, run):
        p.add_argument("--debug", action="store_true", help="verbose logging (incl. bleak); print each sample")
    run.add_argument("--diag-log", type=Path, help="also write diagnostic log lines to this file")
    return parser


def setup_logging(debug: bool, diag_log: Path | None = None) -> None:
    """Diagnostics go to stderr (and optionally a file); data goes to the JSONL log only."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if diag_log is not None:
        diag_log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(diag_log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("wahoo_lsl").setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger("bleak").setLevel(logging.DEBUG if debug else logging.WARNING)


def format_scan(results: Sequence[ScanResult]) -> str:
    if not results:
        return "No Cycling Power devices found."
    lines = [f"{'name':<24} {'address':<38} {'rssi':>5}  cps"]
    for r in results:
        rssi = r.rssi if r.rssi is not None else "?"
        lines.append(f"{(r.name or '?'):<24} {r.address:<38} {rssi:>5}  {'yes' if r.cps_advertised else 'no'}")
    return "\n".join(lines)


def format_features(info: DeviceInfo, measurements: Sequence[str]) -> str:
    f = info.features
    lines = [
        f"Device:  {info.name or '?'}   ({info.address_kind}: {info.address})",
        f"Sensor location: {info.sensor_location or 'not reported'}",
        f"Feature bits (0x{f.raw:08X}):",
    ]
    for name in FEATURE_BITS.values():
        lines.append(f"  [{'x' if f.flags[name] else ' '}] {name}")
    lines.append(f"  {DISTRIBUTED_SYSTEM_SUPPORT} = {f.flags[DISTRIBUTED_SYSTEM_SUPPORT]}")
    lines.append("Measurements supported: " + ", ".join(sorted(f.supported)))
    if measurements:
        width = max(len(m) for m in measurements)
        lines.append("Configured measurements:")
        for m in measurements:
            lines.append(f"  {m:<{width}}  {'available' if m in f.supported else 'UNAVAILABLE'}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, backend: BleBackend | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        setup_logging(args.debug, getattr(args, "diag_log", None))
    except OSError as e:
        print(f"wahoo-lsl: cannot write --diag-log {args.diag_log}: {e}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        if args.command == "scan":
            print(format_scan(asyncio.run(scan_cps(args.timeout, backend))))
            return EXIT_OK
        if args.command == "list-features":
            cfg = load_config(args.config, require_measurements=False)
            client = CpsClient(cfg, lambda _item: None, WarnOnce(log), backend=backend)
            info = asyncio.run(client.read_info())
            print(format_features(info, cfg.measurements))
            return EXIT_OK
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return EXIT_CONFIG
    except BleSetupError as e:
        log.error("%s", e)
        return EXIT_SETUP
    except COMMS_ERRORS as e:
        log.error("Bluetooth error: %s", e)
        return EXIT_SETUP
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return run_session(cfg, debug=args.debug, backend=backend)


def run_session(
    cfg: Config,
    *,
    debug: bool = False,
    backend: BleBackend | None = None,
    start: datetime | None = None,
) -> int:
    log_path = format_log_path(cfg.log.path, start or datetime.now())
    try:  # pre-flight in the main thread so a bad path can never mean a run without a log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8"):
            pass
    except OSError as e:
        log.error("Cannot write log file %s: %s", log_path, e)
        return EXIT_CONFIG

    warn = WarnOnce(log)
    sinks: list[Sink] = [LogSink(log_path, cfg.log.flush_every_n)]
    if debug:
        sinks.append(ConsoleSink())
    pipeline = Pipeline(sinks, cfg.measurements, warn, queue_warn_depth=cfg.queue_warn_depth)
    pipeline.start()
    pipeline.put(make_event("clock_sync"))
    log.info("Logging samples to %s", log_path)

    client = CpsClient(cfg, pipeline.put, warn, backend=backend)
    code, reason = EXIT_OK, "user"
    try:
        asyncio.run(_stream(client))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass  # Windows Ctrl-C, or a second POSIX Ctrl-C; client.run() has already disconnected in its finally block
    except BleSetupError as e:
        log.error("%s", e)
        code, reason = EXIT_SETUP, "setup_error"
    except ReconnectExhausted as e:
        log.error("%s", e)
        code, reason = EXIT_RECONNECT, "reconnect_exhausted"
    except Exception:
        log.exception("Unexpected error")
        code, reason = EXIT_ERROR, "error"
    finally:
        pipeline.put(make_event("stopped", reason=reason))
        if not pipeline.close(timeout=CLOSE_TIMEOUT_S):
            log.warning("Queue sizes at shutdown (items not yet processed): %s", pipeline.queue_sizes())
            if code == EXIT_OK:
                code = EXIT_ERROR
    log.info("Stopped (%s)", reason)
    return code


async def _stream(client: CpsClient) -> None:
    stop = asyncio.Event()
    if sys.platform != "win32":  # add_signal_handler is not implemented on Windows event loops
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()

        def on_signal() -> None:
            if stop.is_set():
                main_task.cancel()  # second Ctrl-C: stop waiting for a graceful shutdown
            else:
                stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
    await client.run(stop)
