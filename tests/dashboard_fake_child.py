"""Stand-in child for launcher tests: python dashboard_fake_child.py LABEL ORDER_FILE [MODE ...]

Modes: none (idle until interrupted), "listen PORT", "exit CODE DELAY_S", "ignore" (ignores
interrupts; only terminate() stops it). On interrupt: append LABEL to ORDER_FILE, exit 0.
"""
import signal
import socket
import sys
import time

label, order_file, mode = sys.argv[1], sys.argv[2], sys.argv[3:]
stopping = False


def on_interrupt(signum, frame):
    global stopping
    stopping = True


handler = signal.SIG_IGN if mode[:1] == ["ignore"] else on_interrupt
signal.signal(signal.SIGINT, handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, handler)

print(f"{label} started", flush=True)
server = None
if mode[:1] == ["listen"]:
    server = socket.socket()
    server.bind(("127.0.0.1", int(mode[1])))
    server.listen(8)
if mode[:1] == ["exit"]:
    time.sleep(float(mode[2]))
    sys.exit(int(mode[1]))
while not stopping:
    time.sleep(0.02)
if server is not None:
    server.close()
with open(order_file, "a") as f:
    f.write(label + "\n")
print(f"{label} stopping", flush=True)
