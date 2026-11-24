"""
rag-processor steps: every stage runnable on its own, one JSON result each (contract: pipeline/stepkit.py).

    python -m pipeline.steps list
    python -m pipeline.steps status                        # LanceDB, outbox, ledger, universe, quant
    python -m pipeline.steps ingest --next                 # the oldest pending collector batch
    python -m pipeline.steps ingest --all                  # every pending batch, oldest first
    python -m pipeline.steps ingest --batch <outbox>/<id>  # one batch (also a done one: re-ingest is a noop per file)
    python -m pipeline.steps ingest-dir /path/to/purchased # a folder of purchased files (what main.py ROOT does)
    python -m pipeline.steps quant-sync                    # universe + typed market rows -> data/quant.sqlite3
    python -m pipeline.steps quant-zfactor                 # Z-factors -> quant DB + `quant_factors` table
    python -m pipeline.steps quant-bracket --schedule      # read-only: bracket bounds per date / constituents
    python -m pipeline.steps health                        # can the pipeline start here? (libmagic, LanceDB schema)

Only the result JSON goes to stdout: while a step runs, file descriptor 1 points at stderr (child processes
inherit that), so pipeline logs and library output never mix with the result.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

from config.settings import (BATCH_LEDGER, COLLECTOR_OUTBOX, LANCEDB_PATH, LANCEDB_TABLE, QUANT_DB, QUANT_TABLE,
                             STEP_LOCK_DIR, UNIVERSE_DB)
from pipeline.stepkit import emit, result, run_step

STEP_HELP = {
    "status": "LanceDB row counts, collector outbox (ready / pending), batch ledger, universe snapshot, quant DB",
    "ingest": "verify and ingest collector outbox batches into LanceDB (+ install the universe snapshot)",
    "ingest-dir": "ingest a folder of purchased files (crawled recursively, any layout)",
    "quant-sync": "universe snapshot + typed price/fundamentals rows from LanceDB -> quant DB",
    "quant-zfactor": "point-in-time Z-factors and size brackets -> quant DB + quant_factors table",
    "quant-bracket": "read-only: target bracket schedule over time, or survivorship-free constituents on a date",
    "health": "check that the pipeline can start (libmagic, LanceDB schema, writable data dir)",
}


def _run_pipeline(root: Path, workers: int | None, run_id: str) -> Dict[str, Any]:
    from pipeline.indexer import Indexer
    from pipeline.pipeline import PipelineRunner

    return PipelineRunner(indexer=Indexer(workers=workers, run_id=run_id)).run(root)


def _setup(run_id: str) -> None:
    from config.logsetup import setup_logging
    from config.settings import ensure_dirs

    ensure_dirs()
    setup_logging(run_id)


def ingest_step(outbox: Path, batch: Path | None, every: bool, workers: int | None) -> Dict[str, Any]:
    from pipeline import batches as b

    run_id = uuid.uuid4().hex[:12]
    if batch is not None:
        todo: List[Path] = [batch]
    else:
        pending = b.pending_batches(outbox)
        todo = pending if every else pending[:1]
    if not todo:
        ready = b.ready_batches(outbox)
        return result("ingest", "noop", "no pending batches" if ready or Path(outbox).is_dir()
                      else f"outbox {outbox} does not exist yet", counts={"ready": len(ready), "pending": 0},
                      outbox=str(outbox))
    _setup(run_id)
    entries = [b.ingest_batch(d, lambda root: _run_pipeline(root, workers, run_id)) for d in todo]
    statuses = [e["status"] for e in entries]
    counts = {"batches": len(entries), "ok": statuses.count("ok"), "partial": statuses.count("partial"),
              "failed": statuses.count("failed"),
              "files_indexed": sum(e.get("files_indexed") or 0 for e in entries),
              "files_failed": sum(e.get("files_failed") or 0 for e in entries),
              "chunks_written": sum(e.get("chunks_written") or 0 for e in entries),
              "pending_after": len(b.pending_batches(outbox)) if batch is None else None}
    status = "failed" if counts["failed"] == len(entries) else "partial" if counts["failed"] or counts["partial"] else "ok"
    nxt = (["quant-sync"] if counts["files_indexed"] or any(e.get("universe") == "installed" for e in entries) else [])
    if counts.get("pending_after"):
        nxt.insert(0, "ingest")
    return result("ingest", status, "; ".join(f"{e['batch_id']}: {e['reason']}" for e in entries if e.get("reason")),
                  counts=counts, batches=[{k: e.get(k) for k in ("batch_id", "status", "reason", "files_indexed",
                                                                 "files_failed", "chunks_written", "universe")}
                                          for e in entries], run_id=run_id, next_suggested=nxt)


def ingest_dir_step(root: Path, workers: int | None) -> Dict[str, Any]:
    if not root.exists():
        return result("ingest-dir", "blocked", f"{root} does not exist")
    run_id = uuid.uuid4().hex[:12]
    _setup(run_id)
    s = _run_pipeline(root, workers, run_id)
    failed = s.get("files_failed", 0)
    status = "partial" if failed and s.get("files_indexed") else "failed" if failed else (
        "ok" if s.get("files_indexed") else "noop")
    return result("ingest-dir", status, f"{failed} file(s) failed" if failed else "",
                  counts={k: s.get(k) for k in ("files_found", "files_indexed", "files_failed", "files_skipped",
                                                "chunks_written")},
                  failed_files=s.get("failed_files", [])[:50], run_id=run_id,
                  next_suggested=["quant-sync"] if s.get("files_indexed") else [])


def _lance_counts() -> Dict[str, Any]:
    try:
        import lancedb

        from pipeline.quant.sync import table_names

        db = lancedb.connect(str(LANCEDB_PATH))
        names = table_names(db)
        return {t: db.open_table(t).count_rows() for t in (LANCEDB_TABLE, QUANT_TABLE) if t in names}
    except Exception as exc:  # status must never fail on a missing or locked DB
        return {"error": f"{type(exc).__name__}: {exc}"}


def status_step(outbox: Path) -> Dict[str, Any]:
    from pipeline import batches as b
    from pipeline.quant.sync import read_state

    states = b.batch_states()
    ready = b.ready_batches(outbox)
    pending = b.pending_batches(outbox)
    by_status: Dict[str, int] = {}
    for s in states.values():
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    rep = {"lancedb": {"path": str(LANCEDB_PATH), "rows": _lance_counts()},
           "outbox": {"path": str(outbox), "exists": Path(outbox).is_dir(), "ready": len(ready),
                      "pending": [d.name for d in pending][:20], "pending_count": len(pending)},
           "ledger": {"path": str(BATCH_LEDGER), "batches": len(states), "by_status": by_status},
           "universe": {"path": str(UNIVERSE_DB), "exists": UNIVERSE_DB.exists(), **b.read_universe_meta()},
           "quant": {"path": str(QUANT_DB), "exists": QUANT_DB.exists(), **read_state()}}
    nxt = ["ingest"] if pending else []
    if QUANT_DB.exists() is False and (rep["universe"]["exists"] or rep["lancedb"]["rows"].get(LANCEDB_TABLE)):
        nxt.append("quant-sync")
    return result("status", "ok", "", next_suggested=nxt, **rep)


def health_step() -> Dict[str, Any]:
    checks: Dict[str, str] = {}
    try:
        import magic  # noqa: F401

        checks["libmagic"] = "ok"
    except Exception as exc:
        checks["libmagic"] = f"missing: {exc} (macOS: brew install libmagic)"
    try:
        from pipeline.vector_store import VectorStore

        VectorStore().open_or_create()
        checks["lancedb_schema"] = "ok"
    except Exception as exc:
        checks["lancedb_schema"] = f"{type(exc).__name__}: {exc}"
    try:
        from config.settings import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / f".health.{os.getpid()}"
        probe.write_text("x")
        probe.unlink()
        checks["data_dir_writable"] = "ok"
    except OSError as exc:
        checks["data_dir_writable"] = str(exc)
    bad = {k: v for k, v in checks.items() if v != "ok"}
    return result("health", "blocked" if bad else "ok", "; ".join(f"{k}: {v}" for k, v in bad.items()), checks=checks)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m pipeline.steps", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = ap.add_subparsers(dest="step", required=True)
    sub.add_parser("list")
    st = sub.add_parser("status", help=STEP_HELP["status"])
    st.add_argument("--outbox", type=Path, default=COLLECTOR_OUTBOX)
    ing = sub.add_parser("ingest", help=STEP_HELP["ingest"], allow_abbrev=False)
    ing.add_argument("--outbox", type=Path, default=COLLECTOR_OUTBOX)
    g = ing.add_mutually_exclusive_group()
    g.add_argument("--batch", type=Path)
    g.add_argument("--next", action="store_true", help="the oldest pending batch (default)")
    g.add_argument("--all", action="store_true")
    ing.add_argument("--workers", type=int)
    d = sub.add_parser("ingest-dir", help=STEP_HELP["ingest-dir"], allow_abbrev=False)
    d.add_argument("root", type=Path)
    d.add_argument("--workers", type=int)
    sub.add_parser("quant-sync", help=STEP_HELP["quant-sync"]).add_argument("--full", action="store_true",
                                                                            help="re-read every market row")
    z = sub.add_parser("quant-zfactor", help=STEP_HELP["quant-zfactor"], allow_abbrev=False)
    z.add_argument("--value-field", default=None)
    z.add_argument("--target-min-cr", type=float, default=3500.0)
    z.add_argument("--target-max-cr", type=float, default=12500.0)
    z.add_argument("--no-lance", action="store_true", help="don't write the quant_factors table")
    br = sub.add_parser("quant-bracket", help=STEP_HELP["quant-bracket"], allow_abbrev=False)
    br.add_argument("--schedule", action="store_true")
    br.add_argument("--date")
    br.add_argument("--start-date")
    br.add_argument("--end-date")
    br.add_argument("--limit", type=int, default=20)
    sub.add_parser("health", help=STEP_HELP["health"])
    return ap


def dispatch(a: argparse.Namespace) -> Dict[str, Any]:
    locks = STEP_LOCK_DIR
    if a.step == "list":
        return result("list", "ok", steps=[{"name": k, "help": v} for k, v in STEP_HELP.items()])
    if a.step == "status":
        return run_step("status", status_step, locks / "readonly-status", a.outbox)
    if a.step == "ingest":
        # one LanceDB writer at a time: ingest and ingest-dir share the "ingest" lock
        return run_step("ingest", ingest_step, locks, a.outbox, a.batch, a.all, a.workers)
    if a.step == "ingest-dir":
        res = run_step("ingest", ingest_dir_step, locks, a.root, a.workers)
        res["step"] = "ingest-dir"
        return res
    if a.step == "quant-sync":
        from pipeline.quant.sync import quant_sync

        return run_step("quant", lambda: quant_sync(full=a.full), locks)
    if a.step == "quant-zfactor":
        from config.settings import QUANT_VALUE_FIELD
        from pipeline.quant.sync import quant_zfactor

        res = run_step("quant", lambda: quant_zfactor(value_field=a.value_field or QUANT_VALUE_FIELD,
                                                      write_lance=not a.no_lance,
                                                      target_bracket_cr=(a.target_min_cr, a.target_max_cr)), locks)
        res["step"] = "quant-zfactor"
        return res
    if a.step == "quant-bracket":
        from pipeline.quant.sync import quant_bracket

        return run_step("quant-bracket", lambda: quant_bracket(schedule=a.schedule, date=a.date,
                                                               start_date=a.start_date, end_date=a.end_date,
                                                               limit=a.limit), locks / "readonly-bracket")
    if a.step == "health":
        return run_step("health", health_step, locks / "readonly-health")
    return result(a.step, "failed", "unknown step")


def main(argv: List[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)  # logs, child processes and libraries write to stderr while the step runs
    try:
        res = dispatch(a)
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
    if a.step == "quant-sync":
        res["step"] = "quant-sync"
    return emit(res)


if __name__ == "__main__":
    sys.exit(main())
