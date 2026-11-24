"""rag-collector steps: every stage of the collector, runnable on its own, one JSON result each.

    python -m rag_collector.steps list                       # the steps and what they do
    python -m rag_collector.steps status                     # dashboard: records, docket, outbox, universe
    python -m rag_collector.steps discover --max-queries 5   # search (SearXNG / APIs) -> docket
    python -m rag_collector.steps fetch --max-files 40       # download docket items
    python -m rag_collector.steps process                    # parse + OCR + merge -> data/processed/
    python -m rag_collector.steps drain                      # fetch + process, no searching
    python -m rag_collector.steps collect --max-queries 5    # discover + fetch + process in one run
    python -m rag_collector.steps universe-sync              # rebuild data/nse_unified.sqlite3
    python -m rag_collector.steps export                     # new documents -> outbox batch for rag-processor
    python -m rag_collector.steps audit                      # read-only data-quality audit

The result contract (status values, exit codes, locks) is in rag_collector/stepkit.py. discover/fetch/process/
drain/collect run scripts/collect.py and translate its run summary; they share one lock ("collect"), because
collect.py's stages share the docket and the run-state files.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from rag_collector import export, paths, universe_sync
from rag_collector.stepkit import emit, result, run_step

COLLECT_STAGES = {"discover": "discover", "fetch": "fetch", "process": "process", "drain": "drain", "collect": "all"}
STEP_HELP = {
    "status": "read-only dashboard: records, docket, outbox, universe (no lock)",
    "discover": "search for documents and file them in the docket (needs SearXNG or a search API)",
    "fetch": "download docket items at each domain's pace",
    "process": "parse/OCR downloaded files and merge them into data/processed/",
    "drain": "fetch + process without searching",
    "collect": "discover + fetch + process (one collect.py run)",
    "universe-sync": "rebuild the point-in-time universe DB from the processed CSVs (noop when unchanged)",
    "export": "write new relevant documents + universe snapshot as an outbox batch for rag-processor",
    "audit": "read-only data-quality audit of the master dataset",
}
# collect.py stop reasons -> step status
BLOCKED_STOPS = {"low_disk_space", "network_unavailable", "searxng_down", "search_unavailable"}
RETRY_STOPS = {"search_engines_suspended", "engines_blocked"}


def _run_json(cmd: list[str], timeout: float | None = None) -> tuple[int, dict | None, str]:
    """Run a pipeline script without a shell; parse the last JSON object it printed."""
    r = subprocess.run(cmd, cwd=paths.root(), capture_output=True, text=True, timeout=timeout)
    parsed = None
    for line in reversed(r.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                continue
    if parsed is None and r.stdout.strip().startswith("{"):
        try:
            parsed = json.loads(r.stdout)
        except ValueError:
            pass
    return r.returncode, parsed, (r.stderr or "")[-800:]


def _script(name: str) -> str:
    return str(paths.root() / "scripts" / name)


def collect_step(step: str, extra: list[str]) -> dict:
    stage = COLLECT_STAGES[step]
    code, out, err = _run_json([paths.python(), _script("collect.py"), "--stage", stage, *extra])
    if out is None:
        return result(step, "failed", f"collect.py printed no summary (exit {code}): {err.strip()[-300:]}")
    if out.get("dry_run"):
        return result(step, "noop", "dry run", plan=out)
    if "error" in out and "status" not in out:
        return result(step, "blocked", str(out["error"]), detail=out)
    status, stop = out.get("status"), out.get("stop_reason") or ""
    counts = {k: out.get(k) or 0 for k in ("queries_done", "queries_failed", "urls_found", "filed_new", "fetched_ok",
                                           "fetch_failed", "fetch_dead", "harvested_ok", "parsed_rows",
                                           "new_records", "harvest_backlog")}
    counts["records_after"] = out.get("records_after")
    did = any(counts[k] for k in ("queries_done", "filed_new", "fetched_ok", "parsed_rows", "new_records"))
    nxt = ["universe-sync", "export"] if counts["new_records"] or counts["parsed_rows"] else []
    if stop in BLOCKED_STOPS:
        st = "blocked"
    elif stop in RETRY_STOPS:
        st = "retry_later"
    elif status == "completed":
        st = "ok" if did else "noop"
        if counts["fetch_failed"] or counts["queries_failed"]:
            st = "partial" if did else st
    else:
        st = "failed"
    return result(step, st, stop or str(status), counts=counts, next_suggested=nxt, run_id=out.get("run_id"),
                  summary_file=out.get("summary_file"), collect_status=status)


def status_step() -> dict:
    root = paths.root()
    rep: dict = {}
    code, out, err = _run_json([paths.python(), _script("status_report.py"), "--json"], timeout=120)
    rep["dataset"] = out if out is not None else {"error": err.strip()[-300:] or f"exit {code}"}
    code, out, err = _run_json([paths.python(), _script("collect.py"), "--dry-run"], timeout=180)
    if out:
        rep["collector"] = {k: out.get(k) for k in ("would_run", "limits", "harvest_backlog", "docket",
                                                     "blocked_domains", "search_cooldown_min_left",
                                                     "search_engines_usable")}
    uni = universe_sync.read_state()
    exists = paths.universe_db().exists()
    current = universe_sync.fingerprint(root, uni.get("include_bse_only", False))
    rep["universe"] = {"database": str(paths.universe_db().relative_to(root)), "exists": exists,
                       "synced_at": uni.get("synced_at"), "counts": uni.get("counts"),
                       "stale": exists and uni.get("fingerprint") != current}
    ob = paths.outbox()
    st = export.load_state(ob)
    rep["outbox"] = {"path": str(ob), "batches": len(export.list_batches(ob)),
                     "documents_exported": len(st.get("exported_sha256", {})),
                     "last_batch": (st.get("batches") or [None])[-1]}
    dry = export.run(dry_run=True, outbox=ob) if paths.docket_db().exists() else None
    if dry:
        rep["outbox"]["would_export"] = dry["counts"].get("would_export", 0)
    nxt = []
    if rep["universe"]["stale"] or not rep["universe"]["exists"]:
        nxt.append("universe-sync")
    if rep["outbox"].get("would_export"):
        nxt.append("export")
    return result("status", "ok", "", next_suggested=nxt, **rep)


def audit_step() -> dict:
    with tempfile.TemporaryDirectory() as td:
        code, out, err = _run_json([paths.python(), _script("audit_dataset.py"), "--out", str(Path(td) / "a.json")],
                                   timeout=600)
    if out is None:
        return result("audit", "failed", f"audit printed nothing (exit {code}): {err.strip()[-300:]}")
    errors = out.get("errors") or {}
    return result("audit", "ok" if code == 0 and not errors else "partial",
                  "audit errors found" if errors else "", counts={"records": out.get("records", 0),
                                                                  "errors": sum(errors.values()) if isinstance(errors, dict) else 0},
                  audit=out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rag-collector-step", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = ap.add_subparsers(dest="step", required=True)
    sub.add_parser("list", help="list the steps")
    sub.add_parser("status", help=STEP_HELP["status"])
    for name in COLLECT_STAGES:
        p = sub.add_parser(name, help=STEP_HELP[name], allow_abbrev=False)
        if name in ("discover", "collect"):
            p.add_argument("--max-queries", type=int)
            p.add_argument("--phase")
        if name in ("fetch", "drain", "collect"):
            p.add_argument("--max-files")
            p.add_argument("--max-docs-per-company")
        if name in ("process", "drain", "collect"):
            p.add_argument("--max-companies")
        p.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("universe-sync", help=STEP_HELP["universe-sync"], allow_abbrev=False)
    u.add_argument("--force", action="store_true")
    u.add_argument("--include-bse-only", action="store_true")
    e = sub.add_parser("export", help=STEP_HELP["export"], allow_abbrev=False)
    e.add_argument("--max-files", type=int, default=500)
    e.add_argument("--include-irrelevant", action="store_true")
    e.add_argument("--dry-run", action="store_true")
    sub.add_parser("audit", help=STEP_HELP["audit"])
    return ap


def collect_args(a: argparse.Namespace) -> list[str]:
    extra = []
    for opt in ("max_queries", "phase", "max_files", "max_docs_per_company", "max_companies"):
        v = getattr(a, opt, None)
        if v is not None:
            extra += [f"--{opt.replace('_', '-')}", str(v)]
    if getattr(a, "dry_run", False):
        extra.append("--dry-run")
    return extra


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    locks = paths.locks()
    if a.step == "list":
        return emit(result("list", "ok", steps=[{"name": k, "help": v} for k, v in STEP_HELP.items()]))
    if a.step == "status":
        return emit(run_step("status", status_step, locks / "readonly-status"))
    if a.step in COLLECT_STAGES:
        res = run_step("collect", collect_step, locks, a.step, collect_args(a))
        res["step"] = a.step
        return emit(res)
    if a.step == "universe-sync":
        return emit(run_step("universe-sync", universe_sync.run, locks, a.force, a.include_bse_only))
    if a.step == "export":
        return emit(run_step("export", export.run, locks, a.max_files, a.include_irrelevant, None, a.dry_run))
    if a.step == "audit":
        return emit(run_step("audit", audit_step, locks / "readonly-audit"))
    return emit(result(a.step, "failed", "unknown step"))


if __name__ == "__main__":
    sys.exit(main())
