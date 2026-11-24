"""How collect.py ends its workers (ultrareview: no SIGTERM on top of an interrupt save)."""
import signal
import subprocess
import sys
import time

import collect

# a worker that records which signals it got, then takes a while to "save" after SIGINT
WORKER = """
import signal, sys, time
got = []
def h(sig, _):
    got.append(sig)
    if sig == signal.SIGTERM:
        print('SIGTERM', flush=True); sys.exit(15)
signal.signal(signal.SIGTERM, h)
signal.signal(signal.SIGINT, h)
print('ready', flush=True)
while not got: time.sleep(0.05)
time.sleep(1.5)  # saving
print('saved', flush=True)
"""


def start():
    p = subprocess.Popen([sys.executable, "-c", WORKER], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    return p


def test_interrupted_workers_finish_their_save_without_sigterm():
    p = start()
    p.send_signal(signal.SIGINT)
    collect.stop_children({"w": p}, "interrupted", grace_s=0.2)
    assert p.returncode == 0 and "saved" in p.stdout.read()


def test_errored_runs_still_sigterm_lingering_workers():
    p = start()
    t0 = time.monotonic()
    collect.stop_children({"w": p}, "error", grace_s=0.2)
    assert p.returncode == 15 and time.monotonic() - t0 < 10
