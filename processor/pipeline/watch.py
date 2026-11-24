"""
Continuous processing: watch rag-collector's outbox and ingest every READY batch as it appears.

    main.py --watch                         # default outbox: COLLECTOR_OUTBOX (../rag-collector/data/export)
    main.py --watch /path/to/outbox --once  # process what is pending, then exit
    python -m pipeline.watch --poll 30

Each cycle: ingest the oldest pending batch (repeat while batches are pending), then quant-sync, and quant-zfactor
when new market rows arrived; then sleep --poll seconds. Decisions come from the step results only.
A batch with failed files is retried on later cycles (BATCH_MAX_ATTEMPTS); a batch that fails verification is
recorded and skipped after its attempts. Stop: Ctrl-C, SIGTERM or `touch data/STOP` (checked between batches and
while sleeping; a SIGTERM during an ingest stops it cleanly: worker processes are terminated, the batch is
not recorded and is picked up again next time). Each step result is appended to data/logs/watch.jsonl.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from config.settings import COLLECTOR_OUTBOX, LOG_DIR, STEP_LOCK_DIR, STOP_FILE, WATCH_POLL_SECONDS
from pipeline.stepkit import now_iso, run_step


class Watch:
    def __init__(self, outbox: Path, poll: float, once: bool, workers: Optional[int], zfactor: bool = True):
        self.outbox, self.poll, self.once, self.workers, self.zfactor = Path(outbox), poll, once, workers, zfactor
        self.stopping = False
        self.log_path = LOG_DIR / "watch.jsonl"

    def on_term(self, *_: Any) -> None:
        self.stopping = True
        raise KeyboardInterrupt  # unwinds the running ingest so the indexer terminates its child processes

    def stop_requested(self) -> bool:
        return self.stopping or STOP_FILE.exists()

    def record(self, res: Dict[str, Any]) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_iso(), **{k: res.get(k) for k in (
                "step", "status", "reason", "counts", "batches", "duration_s")}}, default=str) + "\n")
        print(f"■ {res.get('step')}: {res.get('status')} {res.get('reason') or ''}".rstrip(), file=sys.stderr,
              flush=True)

    def step(self, name: str, fn, *args) -> Dict[str, Any]:
        res = run_step(name, fn, STEP_LOCK_DIR, *args)
        self.record(res)
        if res.get("reason") == "interrupted":
            self.stopping = True
        return res

    def cycle(self) -> Dict[str, Any]:
        from pipeline.quant.sync import quant_sync, quant_zfactor
        from pipeline.steps import ingest_step

        ingested = 0
        while not self.stop_requested():
            res = self.step("ingest", ingest_step, self.outbox, None, False, self.workers)
            if res["status"] in ("noop", "failed", "retry_later"):
                break
            ingested += 1
            if not res["counts"].get("pending_after"):
                break
        synced = {}
        if ingested and not self.stop_requested():
            synced = self.step("quant", quant_sync)
            if self.zfactor and synced.get("counts", {}).get("daily_inserted") and not self.stop_requested():
                self.step("quant", quant_zfactor)
        return {"ingested_batches": ingested, "quant_sync": synced.get("status")}

    def sleep(self) -> None:
        end = time.monotonic() + self.poll
        while time.monotonic() < end and not self.stop_requested():
            time.sleep(min(2.0, max(0.0, end - time.monotonic())))

    def main(self) -> int:
        signal.signal(signal.SIGTERM, self.on_term)
        STOP_FILE.unlink(missing_ok=True)
        print(f"■ watching {self.outbox} (poll {self.poll:g}s); stop: Ctrl-C or touch {STOP_FILE}", file=sys.stderr,
              flush=True)
        try:
            while not self.stop_requested():
                self.cycle()
                if self.once:
                    break
                self.sleep()
        except KeyboardInterrupt:
            self.stopping = True
        print("■ stopped", file=sys.stderr, flush=True)
        return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.watch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outbox", nargs="?", type=Path, default=COLLECTOR_OUTBOX)
    ap.add_argument("--poll", type=float, default=WATCH_POLL_SECONDS, help="seconds between outbox checks")
    ap.add_argument("--once", action="store_true", help="process what is pending, then exit")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--no-zfactor", action="store_true", help="skip quant-zfactor after new market rows")
    a = ap.parse_args(argv)
    from config.logsetup import setup_logging
    from config.settings import ensure_dirs

    ensure_dirs()
    setup_logging()
    return Watch(a.outbox, a.poll, a.once, a.workers, zfactor=not a.no_zfactor).main()


if __name__ == "__main__":
    sys.exit(main())
