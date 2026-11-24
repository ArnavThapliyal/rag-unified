"""
Collector outbox batches -> the processor.

rag-collector writes immutable batches to its outbox (see rag_collector/export.py there):

    <outbox>/<batch_id>/files/...            documents + <name>.meta.json sidecars (read by pipeline/metadata.py)
    <outbox>/<batch_id>/universe.sqlite3     point-in-time universe snapshot (optional)
    <outbox>/<batch_id>/manifest.json        contract_version, files with sha256, universe info
    <outbox>/<batch_id>/READY                written last; {"batch_id", "manifest_sha256"}

The processor only reads batches that have READY, verifies the manifest hash and every file's sha256 before
ingesting (a batch that fails verification is not ingested), never writes into the outbox, and records every
attempt in its own ledger (BATCH_LEDGER, JSON lines). A batch whose files all indexed is done; one with failed
files is retried (already indexed documents are skipped by doc_id) up to BATCH_MAX_ATTEMPTS times, then given up.
The universe snapshot is copied with sqlite3's backup API to UNIVERSE_DB, newest batch wins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.settings import BATCH_LEDGER, BATCH_MAX_ATTEMPTS, UNIVERSE_DB

logger = logging.getLogger(__name__)

SUPPORTED_CONTRACTS = (1,)
DONE = ("ok", "noop")
GAVE_UP = "gave_up"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- ledger -------------------------------------------------------------------

def read_ledger(ledger: Optional[Path] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ledger = ledger or BATCH_LEDGER
    try:
        lines = Path(ledger).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("batch_id"):
            out.append(entry)
    return out


def append_ledger(entry: Dict[str, Any], ledger: Optional[Path] = None) -> None:
    ledger = Path(ledger or BATCH_LEDGER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def batch_states(ledger: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """batch_id -> {"status": last status, "attempts": n, "last": last entry}."""
    states: Dict[str, Dict[str, Any]] = {}
    for e in read_ledger(ledger):
        s = states.setdefault(e["batch_id"], {"attempts": 0})
        s["attempts"] += 1
        s["status"] = e.get("status")
        s["last"] = e
    for s in states.values():
        if s["status"] not in DONE and s["attempts"] >= BATCH_MAX_ATTEMPTS:
            s["status"] = GAVE_UP
    return states


# ---- outbox -------------------------------------------------------------------

def ready_batches(outbox: Path) -> List[Path]:
    """Complete batches (READY present), oldest first. Hidden (in-progress) directories are ignored."""
    outbox = Path(outbox)
    if not outbox.is_dir():
        return []
    return sorted(d for d in outbox.iterdir() if d.is_dir() and not d.name.startswith(".") and (d / "READY").is_file())


def pending_batches(outbox: Path, ledger: Optional[Path] = None) -> List[Path]:
    states = batch_states(ledger)
    return [d for d in ready_batches(outbox)
            if states.get(d.name, {}).get("status") not in (*DONE, GAVE_UP)]


class BatchError(ValueError):
    """The batch is malformed or does not match its manifest; it is not ingested."""


def verify_batch(batch_dir: Path) -> Dict[str, Any]:
    """Check READY, the manifest hash, the contract version and every file's sha256. Returns the manifest."""
    batch_dir = Path(batch_dir)
    try:
        ready = json.loads((batch_dir / "READY").read_text(encoding="utf-8"))
        manifest_path = batch_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchError(f"unreadable READY or manifest: {exc}") from exc
    if ready.get("manifest_sha256") != sha256_file(manifest_path):
        raise BatchError("manifest.json does not match the hash in READY")
    if manifest.get("contract_version") not in SUPPORTED_CONTRACTS:
        raise BatchError(f"unsupported contract_version {manifest.get('contract_version')!r}")
    if manifest.get("batch_id") != batch_dir.name:
        raise BatchError(f"manifest batch_id {manifest.get('batch_id')!r} is not the directory name")
    root = batch_dir.resolve()
    for f in manifest.get("files", []):
        p = (batch_dir / f["path"]).resolve()
        if root not in p.parents:
            raise BatchError(f"file path escapes the batch: {f['path']}")
        if not p.is_file():
            raise BatchError(f"missing file: {f['path']}")
        if sha256_file(p) != f.get("sha256"):
            raise BatchError(f"sha256 mismatch: {f['path']}")
    uni = manifest.get("universe")
    if uni:
        p = batch_dir / uni["path"]
        if not p.is_file() or sha256_file(p) != uni.get("sha256"):
            raise BatchError("universe snapshot missing or sha256 mismatch")
    return manifest


# ---- universe snapshot ----------------------------------------------------------

def universe_meta_path(universe_db: Optional[Path] = None) -> Path:
    return Path(universe_db or UNIVERSE_DB).with_suffix(".json")


def read_universe_meta(universe_db: Optional[Path] = None) -> Dict[str, Any]:
    try:
        return json.loads(universe_meta_path(universe_db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def install_universe(batch_dir: Path, manifest: Dict[str, Any], universe_db: Optional[Path] = None) -> Optional[str]:
    """Copy the batch's universe snapshot to universe_db (backup API, atomic replace) unless a newer batch's
    snapshot is already installed. Returns "installed", "older" or None (no snapshot in the batch)."""
    uni = manifest.get("universe")
    if not uni:
        return None
    universe_db = Path(universe_db or UNIVERSE_DB)
    current = read_universe_meta(universe_db)
    if current.get("batch_id") and current["batch_id"] > manifest["batch_id"]:
        return "older"
    universe_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = universe_db.with_name(f".{universe_db.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{(batch_dir / uni['path']).resolve()}?mode=ro&immutable=1", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        # the collector builds the snapshot in WAL mode; readers open the installed copy read-only, which a WAL
        # database without its -shm file refuses ("unable to open database file")
        dst.execute("PRAGMA journal_mode=DELETE")
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise BatchError(f"universe snapshot integrity_check: {check}")
    os.replace(tmp, universe_db)
    meta = {"batch_id": manifest["batch_id"], "installed_at": _now(),
            **{k: uni.get(k) for k in ("fingerprint", "synced_at", "nse_entities", "delisting_events")}}
    meta_tmp = universe_meta_path(universe_db).with_suffix(".json.tmp")
    meta_tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(meta_tmp, universe_meta_path(universe_db))
    return "installed"


# ---- ingest -------------------------------------------------------------------

def ingest_batch(batch_dir: Path, run_pipeline: Callable[[Path], Dict[str, Any]],
                 ledger: Optional[Path] = None, universe_db: Optional[Path] = None) -> Dict[str, Any]:
    """Verify, ingest and record one batch. run_pipeline(root) runs the normal PipelineRunner on root.
    Returns a ledger entry (also appended to the ledger)."""
    batch_dir = Path(batch_dir)
    entry: Dict[str, Any] = {"batch_id": batch_dir.name, "batch_dir": str(batch_dir), "started_at": _now()}
    try:
        manifest = verify_batch(batch_dir)
    except BatchError as exc:
        entry.update(status="failed", reason=f"batch rejected: {exc}", finished_at=_now())
        append_ledger(entry, ledger)
        return entry
    files_root = batch_dir / manifest.get("ingest_root", "files")
    summary: Dict[str, Any] = {}
    if manifest.get("files") and files_root.is_dir():
        summary = run_pipeline(files_root)
    entry["universe"] = install_universe(batch_dir, manifest, universe_db)
    failed = int(summary.get("files_failed", 0) or 0)
    entry.update(
        status="partial" if failed else "ok",
        reason=f"{failed} file(s) failed" if failed else "",
        files_in_manifest=len(manifest.get("files", [])),
        **{k: summary.get(k) for k in ("files_found", "files_indexed", "files_failed", "files_skipped",
                                        "chunks_written", "failed_files", "out_of_scope_files", "pages_unindexed",
                                        "triage", "memory", "run_id") if k in summary},
        finished_at=_now(),
    )
    if summary.get("finalize_error"):
        entry["finalize_error"] = summary["finalize_error"]
    append_ledger(entry, ledger)
    return entry
