"""`./rag loop`: the whole pipeline, continuously.

    processor  `main.py --watch` runs the whole time: ingests every READY batch, then quant-sync / quant-zfactor
    collector  one cycle after another (`rag_collector.loop --once`): an agent session (or deterministic
               collection), universe-sync, export; the pause after each cycle is the collector's own decision
    backpressure  more than --max-pending batches waiting for the processor -> no new collection until it catches up

Every decision comes from step results and exit codes, never from model text. A processor watcher that exits
on its own is restarted (at most --max-restarts times). Stop: Ctrl-C, SIGTERM or `touch logs/STOP` (the running
collector cycle is asked to stop and finishes its current step; the watcher stops after its current batch).

    ./rag loop                                       # agent mode
    ./rag loop --mode deterministic --max-queries 10
    ./rag loop --once                                # one collector cycle, then ingest what it exported, exit
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from unified import registry

ROOT = registry.ROOT
LOG = ROOT / "logs" / "loop.jsonl"
STOP = ROOT / "logs" / "STOP"


def processor_stop() -> Path:
    """The processor's STOP_FILE (--watch stops after its batch), resolved the way processor/config/settings.py does."""
    raw = os.environ.get("STOP_FILE") or (str(Path(os.environ["DATA_DIR"]) / "STOP") if os.environ.get("DATA_DIR")
                                          else "data/STOP")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else ROOT / "processor" / p


AGENT_CMD = ROOT / "agent" / "run_agent.sh"
WATCH_HEALTHY_S = 3600  # a watcher that stayed up this long resets the restart count


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Loop:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.stopping = False
        self.watch: subprocess.Popen | None = None
        self.child: subprocess.Popen | None = None
        self.watch_restarts = 0
        self.watch_started = 0.0

    # ---- control ------------------------------------------------------------------------------------------------
    def on_signal(self, signum, _frame) -> None:
        self.stopping = True
        if self.child and self.child.poll() is None:
            self.child.send_signal(signal.SIGTERM)

    def stop_requested(self) -> bool:
        return self.stopping or STOP.exists()

    def say(self, msg: str) -> None:
        print(f"■ {msg}", flush=True)

    def record(self, **entry) -> None:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now(), **entry}, default=str) + "\n")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stop_requested():
            self.check_watch()
            time.sleep(min(2.0, max(0.0, end - time.monotonic())))

    # ---- processor watcher ---------------------------------------------------------------------------------------
    def processor_cmd(self, once: bool) -> list[str]:
        cmd = [str(registry.python_for("processor")), "main.py", "--watch", "--poll", str(self.a.poll)]
        return cmd + (["--once"] if once else [])

    def start_watch(self) -> None:
        processor_stop().unlink(missing_ok=True)
        # own session: a terminal Ctrl-C reaches the loop only; stop_watch() lets the running batch finish
        self.watch = subprocess.Popen(self.processor_cmd(False), cwd=ROOT / "processor", env=registry.env_for(),
                                      stdin=subprocess.DEVNULL, start_new_session=True)
        self.watch_started = time.monotonic()
        self.say(f"processor watcher started (pid {self.watch.pid})")
        self.record(event="watch_started", pid=self.watch.pid)

    def check_watch(self) -> None:
        if self.a.once or not self.watch or self.watch.poll() is None or self.stopping:
            return
        code = self.watch.returncode
        self.record(event="watch_exited", code=code)
        if time.monotonic() - self.watch_started >= WATCH_HEALTHY_S:  # it ran fine for a while: not a crash loop
            self.watch_restarts = 0
        if self.watch_restarts >= self.a.max_restarts:
            self.say(f"processor watcher exited (code {code}) {self.watch_restarts} times: stopping")
            self.stopping = True
            return
        self.watch_restarts += 1
        self.say(f"processor watcher exited (code {code}); restart {self.watch_restarts}/{self.a.max_restarts}")
        time.sleep(5)
        self.start_watch()

    def stop_watch(self) -> None:
        if not self.watch or self.watch.poll() is not None:
            return
        stop_file = processor_stop()
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.touch()
        try:
            self.watch.wait(timeout=self.a.stop_timeout)
        except subprocess.TimeoutExpired:
            self.say("processor watcher still busy: SIGTERM (the running batch is not recorded, so it is redone)")
            self.watch.terminate()
            try:
                self.watch.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.watch.kill()
        stop_file.unlink(missing_ok=True)

    def pending(self) -> int | None:
        """Batches waiting for the processor (processor.status: outbox.pending_count), None if unknown."""
        _, res = registry.run("processor.status", [], stderr=subprocess.DEVNULL, timeout=120)
        for section in ("outbox", "counts"):
            n = (res.get(section) or {}).get("pending_count")
            if isinstance(n, int):
                return n
        return None

    # ---- collector cycles ----------------------------------------------------------------------------------------
    def collector_cmd(self, export_batches: int = 20) -> list[str]:
        cmd = [str(registry.python_for("collector")), "-m", "rag_collector.loop", "--once", "--mode", self.a.mode,
               "--export-batch", str(self.a.export_batch), "--export-max-batches", str(max(1, export_batches))]
        if self.a.mode == "deterministic":
            cmd += ["--max-queries", str(self.a.max_queries)]
        else:
            cmd += ["--agent-cmd", str(AGENT_CMD)]
        return cmd

    def collector_cycle(self, export_batches: int = 20) -> dict:
        self.child = subprocess.Popen(self.collector_cmd(export_batches), cwd=ROOT / "collector",
                                      env=registry.env_for(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      text=True)
        found: list[dict] = []

        def relay(stream) -> None:  # the collector's progress, passed through; its last JSON line decides
            for line in stream:
                sys.stdout.write(line)
                sys.stdout.flush()
                if line.startswith('{"loop_decision"'):
                    try:
                        found.append(json.loads(line))
                    except ValueError:
                        pass

        reader = threading.Thread(target=relay, args=(self.child.stdout,), daemon=True)
        reader.start()
        while reader.is_alive():  # a cycle can take an hour: keep the processor watcher alive meanwhile
            reader.join(5)
            if STOP.exists() and not self.stopping:  # `touch logs/STOP`: same as SIGTERM, the cycle ends its step
                self.say(f"{STOP} found: asking the collector cycle to stop")
                self.on_signal(signal.SIGTERM, None)
            self.check_watch()
        code = self.child.wait()
        self.child = None
        decision = found[-1] if found else None
        return decision or {"loop_decision": "failed", "pause_min": self.a.failure_pause_min,
                            "why": f"collector loop gave no decision (exit {code})"}

    # ---- main ----------------------------------------------------------------------------------------------------
    def main(self) -> int:
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGTERM, self.on_signal)
        STOP.unlink(missing_ok=True)
        for repo in ("collector", "processor"):
            if not registry.python_for(repo).exists():
                self.say(f"{registry.repo(repo)['python']} is missing; set it up with: {registry.repo(repo)['setup']}")
                return 2
        self.say(f"rag loop: mode={self.a.mode}, max pending batches {self.a.max_pending}; stop: Ctrl-C or touch {STOP}")
        if not self.a.once:
            self.start_watch()
        failures, code = 0, 0
        try:
            while not self.stop_requested():
                self.check_watch()
                waiting = self.pending() if not self.a.once else 0
                if waiting is None:
                    self.say("could not read the processor's pending batches (processor.status); collecting anyway")
                    self.record(event="pending_unknown")
                elif waiting > self.a.max_pending:
                    self.say(f"backpressure: {waiting} batches waiting for the processor; collection paused")
                    self.record(event="backpressure", pending=waiting)
                    self.sleep(self.a.poll)
                    continue
                d = self.collector_cycle(self.a.max_pending - (waiting or 0))
                self.record(event="collector_cycle", **d)
                action, why = d.get("loop_decision"), d.get("why", "")
                if self.a.once:
                    if self.stop_requested():
                        code = 0 if action != "failed" else 1
                        break
                    self.say("--once: ingesting what was exported")
                    ingest = subprocess.run(self.processor_cmd(True), cwd=ROOT / "processor", env=registry.env_for(),
                                            stdin=subprocess.DEVNULL)
                    self.record(event="processor_once", code=ingest.returncode)
                    code = 1 if action == "failed" or ingest.returncode else 0
                    break
                if action == "stop" or self.stop_requested():
                    self.say(f"collector says stop: {why}")
                    code = 0 if why == "stop requested" or self.stop_requested() else 1
                    break
                failures = failures + 1 if action == "failed" else 0
                if failures >= self.a.max_failures:
                    self.say(f"stopping: {failures} failed collector cycles in a row ({why})")
                    code = 1
                    break
                pause = float(d.get("pause_min") or 0)
                if pause > 0:
                    self.say(f"pausing {pause:g} min ({why}); the processor keeps ingesting")
                self.sleep(pause * 60)
        finally:
            if self.child and self.child.poll() is None:  # an unexpected error mid-cycle: don't leave it running
                self.child.terminate()
                try:
                    self.child.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait()
            self.stop_watch()
            self.record(event="stopped", code=code)
            self.say("stopped")
        return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="./rag loop", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("--mode", choices=("agent", "deterministic"), default="agent")
    ap.add_argument("--max-queries", type=int, default=10, help="deterministic mode: queries per cycle")
    ap.add_argument("--export-batch", type=int, default=500, help="documents per outbox batch")
    ap.add_argument("--max-pending", type=int, default=3, help="pause collection above this many waiting batches")
    ap.add_argument("--poll", type=float, default=60, help="seconds between outbox checks")
    ap.add_argument("--max-failures", type=int, default=3)
    ap.add_argument("--failure-pause-min", type=float, default=5)
    ap.add_argument("--max-restarts", type=int, default=3, help="processor watcher restarts before stopping")
    ap.add_argument("--stop-timeout", type=float, default=600, help="seconds to let the watcher finish its batch")
    ap.add_argument("--once", action="store_true", help="one collector cycle, then ingest it, then exit")
    return Loop(ap.parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
