"""wahoo-worker server: the FTMS service and the CPS logger in one process and one event loop.

Windows allows only one process per BLE device, so both services share one connection
(``SharedBackend``). The Wahoo Server app controls the worker over stdin and reads its stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from wahoo_lsl import cli
from wahoo_lsl.apps.shared_ble import SharedBackend
from wahoo_lsl.ble import BleBackend, BleSetupError, CpsClient, ReconnectExhausted
from wahoo_lsl.config import Config, ConfigError, load_config
from wahoo_lsl.derive import CpsDeriver
from wahoo_lsl.ftms.cli import _serve
from wahoo_lsl.ftms.config import FtmsConfig, load_ftms_config
from wahoo_lsl.pipeline import Pipeline, Sink, make_event
from wahoo_lsl.sinks import ConsoleSink, LogSink, LslSink, format_log_path
from wahoo_lsl.warn import WarnOnce

log = logging.getLogger(__name__)

FTMS_READY = "Accepting client commands on"
_LABELS = (("wahoo_lsl.ftms", "ftms"), ("wahoo_lsl.apps", "worker"), ("bleak", "ble"))


def label_for(logger_name: str) -> str:
    for prefix, label in _LABELS:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return label
    return "cps"


class _LabelFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.label = label_for(record.name)
        return True


class _ReadyHandler(logging.Handler):
    """Calls ``on_ready`` when the FTMS service reports that it accepts clients."""

    def __init__(self, on_ready: Any) -> None:
        super().__init__(logging.INFO)
        self._on_ready = on_ready

    def emit(self, record: logging.LogRecord) -> None:
        if FTMS_READY in record.getMessage():
            self._on_ready()


def setup_logging(debug: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)  # flushes after every record
    handler.addFilter(_LabelFilter())
    handler.setFormatter(logging.Formatter("[%(label)s] %(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("wahoo_lsl").setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger("bleak").setLevel(logging.DEBUG if debug else logging.WARNING)


async def run_cps(cfg: Config, backend: BleBackend, stop: asyncio.Event, debug: bool = False) -> int:
    """Async copy of ``cli.run_session`` that runs on the caller's loop until ``stop``."""
    log_path = format_log_path(cfg.log.path, datetime.now())
    try:  # pre-flight so a bad path can never mean a run without a log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8"):
            pass
    except OSError as e:
        cli.log.error("Cannot write log file %s: %s", log_path, e)
        return cli.EXIT_CONFIG

    warn = WarnOnce(cli.log)
    sinks: list[Sink] = [LogSink(log_path, cfg.log.flush_every_n)]
    if cfg.lsl.enabled:
        sinks.append(LslSink(cfg))
    if debug:
        sinks.append(ConsoleSink())
    pipeline = Pipeline(
        sinks, cfg.measurements, warn, queue_warn_depth=cfg.queue_warn_depth,
        deriver=CpsDeriver(cfg.derive.wheel_circumference_m),
    )
    pipeline.start()
    pipeline.put(make_event("clock_sync"))
    cli.log.info("Logging samples to %s", log_path)
    if cfg.lsl.enabled:
        rate = f"{cfg.lsl.nominal_srate:g} Hz" if cfg.lsl.nominal_srate else "irregular"
        cli.log.info("LSL outlet enabled (nominal rate %s); it appears once the device is connected", rate)

    client = CpsClient(cfg, pipeline.put, warn, backend=backend)
    code, reason = cli.EXIT_OK, "user"
    try:
        await client.run(stop)
    except BleSetupError as e:
        cli.log.error("%s", e)
        code, reason = cli.EXIT_SETUP, "setup_error"
    except ReconnectExhausted as e:
        cli.log.error("%s", e)
        code, reason = cli.EXIT_RECONNECT, "reconnect_exhausted"
    except Exception:
        cli.log.exception("Unexpected error")
        code, reason = cli.EXIT_ERROR, "error"
    finally:
        pipeline.put(make_event("stopped", reason=reason))
        if not pipeline.close(timeout=cli.CLOSE_TIMEOUT_S):
            cli.log.warning("Queue sizes at shutdown (items not yet processed): %s", pipeline.queue_sizes())
            if code == cli.EXIT_OK:
                code = cli.EXIT_ERROR
    cli.log.info("Stopped (%s)", reason)
    return code


class _Ftms:
    """The FTMS service task and its own stop event."""

    def __init__(self, cfg: FtmsConfig, backend: BleBackend) -> None:
        self._cfg, self._backend = cfg, backend
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if not self.running:
            self._stop = asyncio.Event()
            self.task = asyncio.ensure_future(self._run(self._stop))

    async def _run(self, stop: asyncio.Event) -> int:
        code = cli.EXIT_ERROR
        try:
            code = await _serve(self._cfg, self._backend, stop)
        except Exception:
            log.exception("FTMS service failed")
        finally:
            log.info("ftms exited with code %d", code)
        return code

    async def stop(self) -> None:
        """Stop and wait, so ``controller.close`` has spun the trainer down to 0 W."""
        if self.task is not None:
            self._stop.set()
            await asyncio.shield(self.task)


async def run_server(
    cps_cfg: Config,
    ftms_cfg: FtmsConfig,
    backend: BleBackend,
    commands: asyncio.Queue,
    *,
    ftms_enabled: bool = True,
    debug: bool = False,
) -> int:
    """Run both services until CPS ends or ``stop``/``None`` (EOF) arrives on ``commands``."""
    loop = asyncio.get_running_loop()
    ftms = _Ftms(ftms_cfg, backend)
    stop_cps = asyncio.Event()
    cps_done = asyncio.Event()
    stopping = False
    cps_task: asyncio.Task | None = None

    def start_cps() -> None:
        nonlocal cps_task
        if cps_task is None and not stopping:
            cps_task = asyncio.ensure_future(run_cps(cps_cfg, backend, stop_cps, debug))
            cps_task.add_done_callback(lambda _t: cps_done.set())

    ready = _ReadyHandler(lambda: loop.call_soon_threadsafe(start_cps))
    ftms_logger = logging.getLogger("wahoo_lsl.ftms")
    ftms_logger.addHandler(ready)
    try:
        if ftms_enabled:
            ftms.start()
            ftms.task.add_done_callback(lambda _t: start_cps())  # FTMS ended before it was ready
        else:
            start_cps()
        get = asyncio.ensure_future(commands.get())
        done_wait = asyncio.ensure_future(cps_done.wait())
        try:
            while True:
                await asyncio.wait({get, done_wait}, return_when=asyncio.FIRST_COMPLETED)
                if done_wait.done():
                    break
                command = get.result()
                get = asyncio.ensure_future(commands.get())
                if command in (None, "stop"):
                    break
                if command == "ftms on":
                    ftms.start()
                elif command == "ftms off":
                    if ftms.running:
                        ftms._stop.set()
                else:
                    log.warning("unknown command %r", command)
        finally:
            get.cancel()
            done_wait.cancel()
        stopping = True
        await ftms.stop()
        stop_cps.set()
        code = await cps_task if cps_task is not None else cli.EXIT_OK
        log.info("cps exited with code %d", code)
        return code
    finally:
        ftms_logger.removeHandler(ready)


def _read_commands(stdin: TextIO, loop: asyncio.AbstractEventLoop, commands: asyncio.Queue) -> None:
    try:
        for line in iter(stdin.readline, ""):
            loop.call_soon_threadsafe(commands.put_nowait, line.strip())
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        loop.call_soon_threadsafe(commands.put_nowait, None)  # EOF: the server is gone
    except RuntimeError:
        pass  # loop already closed


def _install_signals(loop: asyncio.AbstractEventLoop, commands: asyncio.Queue, main_task: asyncio.Task) -> None:
    requested = False

    def on_signal() -> None:
        nonlocal requested
        if requested:
            main_task.cancel()  # second signal: stop waiting for a graceful shutdown
        else:
            requested = True
            commands.put_nowait("stop")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        if sys.platform == "win32":  # add_signal_handler is not implemented on Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))
        else:
            loop.add_signal_handler(sig, on_signal)


async def _main_async(args: argparse.Namespace, cps_cfg: Config, ftms_cfg: FtmsConfig, backend: BleBackend | None,
                      stdin: TextIO) -> int:
    loop = asyncio.get_running_loop()
    commands: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_read_commands, args=(stdin, loop, commands), daemon=True).start()
    _install_signals(loop, commands, asyncio.current_task())
    return await run_server(
        cps_cfg, ftms_cfg, SharedBackend(backend), commands, ftms_enabled=not args.no_ftms, debug=args.debug
    )


def main(argv: Sequence[str] | None = None, *, backend: BleBackend | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wahoo-worker server")
    parser.add_argument("-c", "--config", required=True, type=Path, help="CPS logger config")
    parser.add_argument("-f", "--ftms-config", required=True, type=Path, help="FTMS service config")
    parser.add_argument("--no-ftms", action="store_true", help="do not start the FTMS service (`ftms on` starts it)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    try:
        cps_cfg = load_config(args.config)
        ftms_cfg = load_ftms_config(args.ftms_config)
    except ConfigError as e:
        print(f"[worker] config error: {e}", flush=True)
        return cli.EXIT_CONFIG
    setup_logging(args.debug)
    try:
        return asyncio.run(_main_async(args, cps_cfg, ftms_cfg, backend, stdin or sys.stdin))
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("forced exit; spin-down to 0 W may not have completed")
        return cli.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
