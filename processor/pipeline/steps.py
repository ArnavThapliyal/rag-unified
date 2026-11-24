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
    python -m pipeline.steps health                        # can the pipeline start here? + RAM headroom for workers
    python -m pipeline.steps purge --file X.pdf [--yes]    # remove a document indexed by mistake, deny it for good

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
    "health": "check that the pipeline can start (libmagic, LanceDB schema, writable data dir) + RAM headroom",
    "purge": "remove documents indexed by mistake (--doc-id / --file) from LanceDB and add them to the denylist",
}


def _run_pipeline(root: Path, workers: int | None, run_id: str) -> Dict[str, Any]:
    from pipeline.indexer import Indexer
    from pipeline.pipeline import PipelineRunner

    return PipelineRunner(indexer=Indexer(workers=workers, run_id=run_id,
                                          skip_offer_documents=_skip_offer_documents(root))).run(root)


def _skip_offer_documents(root: Path) -> bool | None:
    """A collector batch says what its corpus is about (manifest scope_target, rag-collector's scope.target):
    offer documents are out of scope only for "delisted". No manifest (ingest-dir): the setting decides."""
    import json

    try:
        target = json.loads((Path(root).parent / "manifest.json").read_text(encoding="utf-8")).get("scope_target")
    except (OSError, ValueError, AttributeError):
        return None
    if target in ("listed", "both"):
        return False
    return None


def _hf_offline_if_cached() -> bool:
    """Run the ingest with the Hugging Face hub offline when every model it loads is already in the local cache:
    no network round-trips per worker start, and nothing silently updates a model mid-corpus. A fresh install
    (models missing) stays online so the first ingest can download them. HF_OFFLINE_INGEST=0 turns this off."""
    from config.settings import EMBEDDING_MODEL, _env_bool

    if not _env_bool("HF_OFFLINE_INGEST", True) or os.environ.get("HF_HUB_OFFLINE"):
        return False
    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub")
    needed = [EMBEDDING_MODEL, "docling-project/docling-models", "docling-project/docling-layout-heron"]
    if all(any((hub / f"models--{m.replace('/', '--')}" / "snapshots").glob("*")) for m in needed):
        os.environ["HF_HUB_OFFLINE"] = "1"  # inherited by the spawned workers and the embedder
        return True
    return False


def _setup(run_id: str) -> None:
    import logging

    from config.logsetup import setup_logging
    from config.settings import ensure_dirs

    ensure_dirs()
    setup_logging(run_id)
    if _hf_offline_if_cached():
        logging.getLogger(__name__).info("models cached: Hugging Face hub offline for this run (HF_HUB_OFFLINE=1)")


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
    m = memory_report()
    last = next((e for e in reversed(b.read_ledger()) if e.get("status") in ("ok", "partial")), {})
    rep["memory"] = {"free_pct": m["system"].get("free_pct"), "swap_used_mb": m["system"].get("swap_used_mb"),
                     "workers": m["workers"], "more_workers_fit": (m["headroom"] or {}).get("more_workers_fit"),
                     "llm_loaded": m["llm_loaded"], "notes": m["notes"]}
    rep["last_ingest"] = {"batch_id": last.get("batch_id"), "out_of_scope_files": last.get("out_of_scope_files", []),
                          "pages_unindexed": last.get("pages_unindexed", {})} if last else {}
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
    return result("health", "blocked" if bad else "ok", "; ".join(f"{k}: {v}" for k, v in bad.items()), checks=checks,
                  memory=memory_report())


# Footprints measured on the 2026-11-24 baseline (M4 Pro 48 GB), used until a run has measured its own.
_DEFAULT_PEAKS = {"embedder_peak_mb": 17000, "worker_peak_mb": 4500}


def _ingest_running() -> bool:
    import fcntl

    path = STEP_LOCK_DIR / "ingest.lock"
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _llm_loaded() -> List[str]:
    """Models LM Studio holds in memory right now (localhost only; [] when it is not running)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:1234/api/v0/models", timeout=1.5) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    return [m.get("id", "") for m in data.get("data", []) if m.get("state") == "loaded" and m.get("type") != "embeddings"]


def memory_report() -> Dict[str, Any]:
    """RAM now, the last ingest's measured peaks, and how many parse workers fit (for you and the agent)."""
    from config.settings import MAX_WORKERS, NUM_WORKERS
    from pipeline import batches as b
    from pipeline import memwatch

    mem = memwatch.system_memory()
    measured = next((e["memory"] for e in reversed(b.read_ledger()) if isinstance(e.get("memory"), dict)
                     and e["memory"].get("worker_peak_mb")), None)
    peaks = {k: int((measured or {}).get(k) or v) for k, v in _DEFAULT_PEAKS.items()}
    running = _ingest_running()
    room = memwatch.headroom(mem, peaks["worker_peak_mb"], peaks["embedder_peak_mb"], NUM_WORKERS, running)
    llm = _llm_loaded()
    notes = []
    if llm and not running:
        notes.append(f"LM Studio holds {', '.join(llm)} in memory: unload it (lms unload --all) before a big ingest")
    if room and room["suggested_max_workers"] > MAX_WORKERS:
        notes.append(f"{room['suggested_max_workers']} workers would fit: raise MAX_WORKERS / NUM_WORKERS to use them")
    if room and room["suggested_max_workers"] < NUM_WORKERS:
        notes.append(f"only {room['suggested_max_workers']} worker(s) fit now: lower NUM_WORKERS or free memory")
    return {"system": mem, "peaks": peaks, "peaks_source": "last ingest" if measured else "baseline default",
            "workers": NUM_WORKERS, "max_workers": MAX_WORKERS, "ingest_running": running, "llm_loaded": llm,
            "headroom": room, "notes": notes}


_SAFE_NAME = __import__("re").compile(r"[\w.\-]{1,200}")


def purge_step(doc_ids: List[str], files: List[str], reason: str, yes: bool) -> Dict[str, Any]:
    """Without --yes: show what would be removed. With it: delete the rows and append the doc_ids to the denylist
    (DENYLIST_FILE) so a later batch carrying the same bytes is skipped as skipped_denylisted."""
    from datetime import datetime, timezone

    from config.settings import DENYLIST_FILE
    from pipeline.vector_store import VectorStore, _check_doc_id

    store = VectorStore().open_or_create()
    targets: Dict[str, str] = {}
    try:
        for d in doc_ids:
            targets[_check_doc_id(d)] = "(by doc_id)"
    except ValueError as exc:
        return result("purge", "blocked", str(exc))
    for name in files:
        if not _SAFE_NAME.fullmatch(name):
            return result("purge", "blocked", f"not a plain file name: {name!r}")
        rows = store.table.search().where(f"source_filename = '{name}'").select(["doc_id"]).limit(100_000).to_list()
        for d in {r["doc_id"] for r in rows}:
            targets[d] = name
        if not rows:
            targets.setdefault(f"missing:{name}", name)
    missing = [n for k, n in targets.items() if k.startswith("missing:")]
    found = {d: n for d, n in targets.items() if not d.startswith("missing:")}
    rows = {d: len(store.get_by_doc_id(d)) for d in found}
    plan = [{"doc_id": d, "file": n, "rows": rows[d]} for d, n in found.items()]
    if not yes:
        return result("purge", "noop", "dry run: pass --yes to delete these rows and deny the doc_ids",
                      counts={"documents": len(plan), "rows": sum(rows.values())}, documents=plan, not_found=missing)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    DENYLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DENYLIST_FILE.open("a", encoding="utf-8") as fh:
        for d, n in found.items():
            store.delete_doc(d)
            fh.write(f"{d}  # {stamp} {n} {reason}".rstrip() + "\n")
    return result("purge", "ok" if plan else "noop", "" if plan else "nothing matched",
                  counts={"documents": len(plan), "rows_deleted": sum(rows.values())}, documents=plan,
                  not_found=missing, denylist=str(DENYLIST_FILE))


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
    pu = sub.add_parser("purge", help=STEP_HELP["purge"], allow_abbrev=False)
    pu.add_argument("--doc-id", action="append", default=[])
    pu.add_argument("--file", action="append", default=[], help="source file name as indexed, e.g. 7cd6e72df866a2c4.pdf")
    pu.add_argument("--reason", default="")
    pu.add_argument("--yes", action="store_true", help="really delete (default: dry run)")
    return ap


def dispatch(a: argparse.Namespace) -> Dict[str, Any]:
    locks = STEP_LOCK_DIR
    if a.step == "list":
        return result("list", "ok", steps=[{"name": k, "help": v} for k, v in STEP_HELP.items()])
    if a.step == "status":
        return run_step("status", status_step, None, a.outbox)
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
                                                               limit=a.limit), None)
    if a.step == "health":
        return run_step("health", health_step, None)
    if a.step == "purge":
        if not (a.doc_id or a.file):
            return result("purge", "blocked", "give --doc-id or --file")
        res = run_step("ingest", purge_step, locks, a.doc_id, a.file, a.reason, a.yes)  # one LanceDB writer
        res["step"] = "purge"
        return res
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
