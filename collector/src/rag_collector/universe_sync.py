"""universe-sync step: rebuild data/nse_unified.sqlite3 from the collector's current outputs.

The database is rebuilt from scratch into a temporary file and swapped in with os.replace(), so it always
reflects exactly the current CSVs (a record dropped by the merge disappears from the DB too) and a reader
never sees a half-built database. Nothing happens (noop) while the input files are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from rag_collector import paths
from rag_collector.nse_bridge import sync_archive_to_store
from rag_collector.stepkit import now_iso, result
from rag_collector.universe import UniverseStore

INPUTS = ("data/reference/nse_equity_listed.csv", "data/reference/nse_sme_listed.csv",
          "data/processed/nse_delisted_master.csv", "data/processed/out_of_scope_delistings.csv",
          "data/processed/proposed_delisting_leads.csv", "data/processed/bse_only_delistings.csv")


def state_path() -> Path:
    return paths.metadata() / "universe_sync.json"


def fingerprint(root: Path, include_bse_only: bool) -> str:
    h = hashlib.sha256(f"bse_only={include_bse_only}\n".encode())
    for rel in INPUTS:
        p = root / rel
        h.update(rel.encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest() if p.exists() else b"missing")
    return h.hexdigest()


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(force: bool = False, include_bse_only: bool = False) -> dict:
    root, db = paths.root(), paths.universe_db()
    master = root / "data" / "processed" / "nse_delisted_master.csv"
    if not master.exists():
        return result("universe-sync", "blocked", "no master dataset yet (data/processed/nse_delisted_master.csv)",
                      next_suggested=["process"])
    fp = fingerprint(root, include_bse_only)
    prev = read_state()
    if not force and db.exists() and prev.get("fingerprint") == fp:
        return result("universe-sync", "noop", "inputs unchanged since the last sync", counts=prev.get("counts", {}),
                      database=str(db.relative_to(root)), fingerprint=fp)

    tmp = db.with_name(f".{db.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        with UniverseStore(tmp) as store:
            counts = sync_archive_to_store(root, store, include_bse_only=include_bse_only)
        con = sqlite3.connect(tmp)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        if check != "ok":
            return result("universe-sync", "failed", f"integrity_check on the rebuilt DB: {check}")
        os.replace(tmp, db)
    finally:
        tmp.unlink(missing_ok=True)

    state = {"fingerprint": fp, "synced_at": now_iso(), "counts": counts, "include_bse_only": include_bse_only}
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_tmp = state_path().with_suffix(".json.tmp")
    state_tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(state_tmp, state_path())
    return result("universe-sync", "ok", "universe rebuilt", counts=counts, database=str(db.relative_to(root)),
                  fingerprint=fp, next_suggested=["export"])
