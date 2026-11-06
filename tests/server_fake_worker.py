"""Stand-in for `wahoo-worker server`: python server_fake_worker.py ORDER_FILE [SWITCH ...]

Obeys the stdin protocol (`ftms on`, `ftms off`, `stop`; EOF = stop) and prints the labelled lines
the supervisor parses. Each action appends a word to ORDER_FILE: ftms-on, ftms-off, ftms, cps.
Switches: --no-ftms, --no-ready (FTMS never logs its ready line), --ftms-dies CODE DELAY_S
(FTMS exits unprompted), --exit CODE DELAY_S (worker exits), --ignore-stop (stop and EOF do nothing),
--ignore-ftms-off (`ftms off` is ignored),
--late-ftms-off DELAY_S (`ftms off` takes that long to exit; an `ftms on` meanwhile restarts FTMS after it).
"""
import sys
import threading
import time

order_file, args = sys.argv[1], sys.argv[2:]
no_ftms, no_ready, ignore_stop = "--no-ftms" in args, "--no-ready" in args, "--ignore-stop" in args
ignore_off = "--ignore-ftms-off" in args


def value(flag, n):
    return args[args.index(flag) + 1:][:n] if flag in args else None


late_off = value("--late-ftms-off", 1)
pending_off, restart = False, False
ftms_dies, exit_after = value("--ftms-dies", 2), value("--exit", 2)
lock = threading.Lock()
ftms_on, ftms_since, done = False, 0.0, threading.Event()


def say(text):
    print(text, flush=True)


def note(word):
    with open(order_file, "a") as f:
        f.write(word + "\n")


def ftms_exit(code):
    global ftms_on
    ftms_on = False
    note("ftms")
    say(f"[worker] 12:00:00 INFO    wahoo_lsl.apps.server_worker: ftms exited with code {code}")


def ftms_start():
    global ftms_on, ftms_since
    ftms_on, ftms_since = True, time.monotonic()
    say("[ftms] 12:00:00 INFO    wahoo_lsl.ftms.server: starting")
    if not no_ready:
        say("[ftms] 12:00:00 INFO    wahoo_lsl.ftms.server: Accepting client commands on 127.0.0.1:1")


def finish():
    with lock:
        if ftms_on:
            ftms_exit(0)
        note("cps")
        say("[worker] 12:00:00 INFO    wahoo_lsl.apps.server_worker: cps exited with code 0")
    done.set()


def late_exit():
    global pending_off, restart
    time.sleep(float(late_off[0]))
    with lock:
        if ftms_on and pending_off:
            pending_off = False
            ftms_exit(0)
            if restart:
                restart = False
                note("ftms-on")
                ftms_start()


def read_stdin():
    global pending_off, restart
    for line in sys.stdin:
        cmd = line.strip()
        with lock:
            if cmd == "ftms on" and pending_off:
                restart = True
            elif cmd == "ftms on" and not ftms_on:
                note("ftms-on")
                ftms_start()
            elif cmd == "ftms off" and ftms_on and late_off:
                note("ftms-off")
                pending_off = True
                threading.Thread(target=late_exit, daemon=True).start()
            elif cmd == "ftms off" and ftms_on and not ignore_off:
                note("ftms-off")
                ftms_exit(0)
            elif cmd == "stop" and not ignore_stop:
                break
    if not ignore_stop:  # `stop` or EOF
        finish()


say("[cps] 12:00:00 INFO    wahoo_lsl.cli: cps started")
if not no_ftms:
    ftms_start()
threading.Thread(target=read_stdin, daemon=True).start()
t0 = time.monotonic()
while not done.is_set():
    time.sleep(0.02)
    with lock:
        if ftms_dies and ftms_on and time.monotonic() - ftms_since >= float(ftms_dies[1]):
            ftms_exit(int(ftms_dies[0]))
    if exit_after and time.monotonic() - t0 >= float(exit_after[1]):
        sys.exit(int(exit_after[0]))
