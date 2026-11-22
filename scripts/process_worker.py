"""Process worker: parses (and OCRs) downloaded docket items and merges records, in batches.

Runs parse_nse_records.py then normalize_and_merge.py whenever `--batch` downloads are waiting (or
whatever is left once the upstream fetch worker has finished), and marks the docket items parsed
(rows, relevance) or parse_error. OCR of scanned PDFs happens here, so it never holds up searching
or downloading.

  python scripts/process_worker.py                    # process what is waiting, then exit
  python scripts/process_worker.py --follow 12345     # keep going while the fetch worker runs
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import common
import docket
from common import ROOT, emit, log

POLL_S = 10


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_script(name: str) -> dict:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / name)], capture_output=True, text=True, cwd=ROOT)
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"{name} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
    return json.loads(lines[-1])


def process_once(dk: docket.Docket) -> dict:
    parsed = run_script("parse_nse_records.py")
    merged = run_script("normalize_and_merge.py")
    sync = dk.sync_parsed(parsed.get("errors"))
    return {"parsed_files": parsed.get("parsed", 0), "parsed_rows": parsed.get("rows", 0),
            "parse_errors": len(parsed.get("errors", [])), "records_after": merged.get("records_out"), "docket": sync}


def run(batch: int, follow: int | None) -> dict:
    dk = docket.open_docket()
    out = {"batches": 0, "parsed_files": 0, "parsed_rows": 0, "parse_errors": 0, "records_after": None,
           "stop_reason": None}
    try:
        while True:
            waiting = dk.fetched_count()
            upstream = alive(follow)
            if waiting and (waiting >= batch or not upstream):
                r = process_once(dk)
                out["batches"] += 1
                for k in ("parsed_files", "parsed_rows", "parse_errors"):
                    out[k] += r[k]
                out["records_after"] = r["records_after"]
                print(f"    ↳ processed {r['parsed_files']} files, +{r['parsed_rows']} rows → records "
                      f"{r['records_after']}", flush=True)
                if r["docket"]["waiting"] >= waiting:  # nothing could be marked: don't spin
                    out["stop_reason"] = "unparseable_waiting"
                    break
                continue
            if not upstream:
                out["stop_reason"] = "idle"
                break
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        out["stop_reason"] = "keyboard_interrupt"
    except RuntimeError as e:
        out["stop_reason"] = f"error: {e}"[:300]
        log(f"process worker: {e}", level="ERROR", kind="run_error")
    log(f"process worker: {out['batches']} batches, {out['parsed_files']} files, +{out['parsed_rows']} rows; "
        f"stop: {out['stop_reason']}", kind="process_worker",
        **{k: out[k] for k in ("batches", "parsed_files", "parsed_rows", "parse_errors", "stop_reason")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", type=int, default=5, help="parse once this many downloads are waiting")
    ap.add_argument("--follow", type=int, metavar="PID")
    ap.add_argument("--summary-out", type=Path)
    a = ap.parse_args()
    common.graceful_sigterm()
    out = run(a.batch, a.follow)
    if a.summary_out:
        a.summary_out.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    emit(out)
    return 0 if not str(out["stop_reason"]).startswith("error") else 1


if __name__ == "__main__":
    sys.exit(main())
