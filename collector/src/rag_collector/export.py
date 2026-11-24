"""export step: hand newly collected documents to rag-processor as one immutable outbox batch.

Batch layout (outbox = data/export/ or $RAG_COLLECTOR_OUTBOX):

    <outbox>/<YYYYMMDD-HHMMSS>/
        files/<raw subdir>/<name>            the document (APFS copy-on-write clone, or a full copy; read-only)
        files/<raw subdir>/<name>.meta.json  sidecar in the keys rag-processor's metadata.py reads
        universe.sqlite3                     consistent snapshot of data/nse_unified.sqlite3 (sqlite backup API)
        manifest.json                        contract_version, producer, files + sha256, universe snapshot info
        READY                                written last: the batch is complete

Rules that keep batches immutable and consumers safe:
- Files are cloned (cp -c, i.e. clonefile(2)) or copied, never hard-linked, so a later re-fetch of the same
  raw file can't change a batch that was already exported. Copies are chmod 0444 and verified by sha256.
- The batch is built in a hidden .<id>.tmp directory and renamed into place; READY is written after the
  rename. rag-processor only reads directories that contain READY.
- The universe DB is copied with sqlite3.Connection.backup(), never as a raw file (WAL/mid-write safe), and its
  PRAGMA integrity_check result is recorded in the manifest.
- A document is exported once per content (sha256); a changed re-fetch is a new document. Only documents the
  process stage parsed as relevant are exported (relevance not "irrelevant:*"), unless include_irrelevant.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from rag_collector import __version__, paths
from rag_collector.stepkit import CONTRACT_VERSION, now_iso, result
from rag_collector import universe_sync

RECORD_FILES = {"nse_delisted_master.csv": "NSE_2006_2026", "out_of_scope_delistings.csv": "PRE_2006",
                "bse_only_delistings.csv": "BSE_ONLY"}
VENDORS = (("nseindia.com", "NSE"), ("bseindia.com", "BSE"), ("sebi.gov.in", "SEBI"), ("ibbi.gov.in", "IBBI"),
           ("mca.gov.in", "MCA"), ("web.archive.org", "Wayback"))
TABULAR_EXT = {".xlsx", ".xls", ".csv"}


# ---------------------------------------------------------------- helpers

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clone_file(src: Path, dst: Path) -> str:
    """Copy-on-write clone on APFS (cp -c); a plain full copy elsewhere. Never a hard link. Returns the method."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        r = subprocess.run(["/bin/cp", "-c", str(src), str(dst)], capture_output=True)
        if r.returncode == 0:
            return "clone"
        dst.unlink(missing_ok=True)
    shutil.copyfile(src, dst)
    return "copy"


def snapshot_sqlite(src: Path, dst: Path) -> dict:
    """Consistent copy of a SQLite database with the backup API (safe while it is being written)."""
    dst.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        check = d.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {t: d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("nse_entities", "delisting_events", "qualitative_filings")}
    finally:
        d.close()
        s.close()
    return {"integrity_check": check, **counts}


def vendor_of(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for dom, name in VENDORS:
        if host == dom or host.endswith("." + dom):
            return name
    return host or None


def doc_type_of(url: str, local_path: str, year_tag: str | None) -> str:
    u = url.lower()
    if re.search(r"delisting[_\-%20 ]*order|/orders?/", u):
        return "delisting_order"
    if "circular" in u:
        return "circular"
    if Path(local_path).suffix.lower() in TABULAR_EXT or year_tag == "master":
        return "delisting_list"
    return "delisting_notice"


def records_by_artifact(processed: Path) -> dict[str, list[dict]]:
    """local artifact path -> the reconciled records it backs (master, pre-2006 and BSE-only outputs)."""
    out: dict[str, list[dict]] = {}
    for name, scope in RECORD_FILES.items():
        p = processed / name
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                arts = {a.strip() for a in (row.get("all_artifact_paths") or "").split("|") if a.strip()}
                if row.get("local_artifact_path"):
                    arts.add(row["local_artifact_path"].strip())
                for a in arts:
                    out.setdefault(a, []).append({**row, "_scope": scope})
    return out


PLACEHOLDERS = {"", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "-", "--"}


def known(value) -> bool:
    """False for the merge's placeholders ("UNKNOWN", "N/A", ...): those never become metadata."""
    return value is not None and str(value).strip().upper() not in PLACEHOLDERS


def sidecar_for(item: dict, recs: list[dict], sha: str, batch_id: str) -> dict:
    """Flat metadata dict: top-level keys rag-processor maps to columns, collector_* keys land in metadata_json."""
    meta: dict = {"source_url": item["url"], "doc_type": doc_type_of(item["url"], item["local_path"], item["year_tag"]),
                  "vendor": vendor_of(item["url"])}
    if len(recs) == 1:  # a document about one company: its identifiers and date are the record's
        r = recs[0]
        meta.update({k: v for k, v in (("symbol", r.get("nse_symbol")), ("nse_symbol", r.get("nse_symbol")),
                                       ("isin", r.get("isin"))) if known(v)})
        date = next((d for d in (r.get("order_date"), r.get("effective_delisting_date")) if known(d)), None)
        if date:
            meta["published_at"] = date
    meta.update({
        "collector_record_ids": sorted({r["record_id"] for r in recs if r.get("record_id")}),
        "collector_scopes": sorted({r["_scope"] for r in recs}),
        "collector_company_names": sorted({r["company_name"] for r in recs if r.get("company_name")})[:25],
        "collector_year_tag": item["year_tag"], "collector_kind": item["kind"],
        "collector_relevance": item["relevance"], "collector_title": item["title"],
        "collector_fetched_at": (datetime.fromtimestamp(item["fetched_at"], timezone.utc).isoformat()
                                 if item["fetched_at"] else None),
        "collector_sha256": sha, "collector_batch": batch_id,
    })
    return {k: v for k, v in meta.items() if v not in (None, "", [])}


def candidates(include_irrelevant: bool) -> list[dict]:
    db = paths.docket_db()
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT url, kind, year_tag, local_path, relevance, title, fetched_at FROM items "
            "WHERE status='parsed' AND local_path IS NOT NULL ORDER BY fetched_at, url").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        rel = r["relevance"] or ""
        if not include_irrelevant and rel.startswith("irrelevant"):
            continue
        out.append(dict(r))
    return out


def state_path(outbox: Path) -> Path:
    return outbox / "export_state.json"


def load_state(outbox: Path) -> dict:
    try:
        return json.loads(state_path(outbox).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exported_sha256": {}, "universe_fingerprint": None, "batches": []}


def save_state(outbox: Path, state: dict) -> None:
    tmp = state_path(outbox).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path(outbox))


def git_commit(root: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def new_batch_id(outbox: Path) -> str:
    base = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bid, n = base, 1
    while (outbox / bid).exists() or (outbox / f".{bid}.tmp").exists():
        n += 1
        bid = f"{base}-{n}"
    return bid


def list_batches(outbox: Path) -> list[dict]:
    out = []
    for d in sorted(outbox.iterdir()) if outbox.is_dir() else []:
        if d.is_dir() and not d.name.startswith("."):
            out.append({"batch_id": d.name, "ready": (d / "READY").exists()})
    return out


# ---------------------------------------------------------------- the step

def run(max_files: int = 500, include_irrelevant: bool = False, outbox: Path | None = None,
        dry_run: bool = False) -> dict:
    root = paths.root()
    outbox = outbox or paths.outbox()
    state = load_state(outbox)
    exported = state.setdefault("exported_sha256", {})

    if not dry_run:  # only a real export (which holds the export lock) touches the outbox
        outbox.mkdir(parents=True, exist_ok=True)
        # stale temp batches from a crashed export are removed (they never had READY)
        for d in outbox.glob(".*.tmp"):
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file():
                        f.chmod(0o644)
                shutil.rmtree(d, ignore_errors=True)

    counts = {"candidates": 0, "already_exported": 0, "missing_raw_file": 0, "changed_during_copy": 0,
              "exported": 0, "cloned": 0, "copied": 0, "bytes": 0}
    pending = []
    for it in candidates(include_irrelevant):
        counts["candidates"] += 1
        src = root / it["local_path"]
        if not src.is_file():
            counts["missing_raw_file"] += 1
            continue
        sha = sha256_file(src)
        if sha in exported:
            counts["already_exported"] += 1
            continue
        pending.append((it, src, sha))
    more_waiting = max(0, len(pending) - max_files)
    pending = pending[:max_files]

    uni_state = universe_sync.read_state()
    uni_fp = uni_state.get("fingerprint") if paths.universe_db().exists() else None
    universe_changed = bool(uni_fp) and uni_fp != state.get("universe_fingerprint")
    if not pending and not universe_changed:
        return result("export", "noop", "no new documents and the universe is unchanged", counts=counts,
                      outbox=str(outbox), next_suggested=[] if uni_fp else ["universe-sync"])
    if dry_run:
        return result("export", "noop", "dry run", counts={**counts, "would_export": len(pending)},
                      universe_changed=universe_changed, more_waiting=more_waiting, outbox=str(outbox))

    batch_id = new_batch_id(outbox)
    tmp = outbox / f".{batch_id}.tmp"
    (tmp / "files").mkdir(parents=True)
    arts = records_by_artifact(paths.processed())
    files = []
    for it, src, sha in pending:
        rel = Path(it["local_path"])
        sub = rel.relative_to("data/raw") if rel.parts[:2] == ("data", "raw") else Path(rel.name)
        dst = tmp / "files" / sub
        method = clone_file(src, dst)
        got = sha256_file(dst)
        if got != sha:  # the raw file was rewritten while we copied: leave it for the next export
            dst.unlink()
            counts["changed_during_copy"] += 1
            continue
        dst.chmod(0o444)
        recs = arts.get(it["local_path"], [])
        meta = sidecar_for(it, recs, sha, batch_id)
        side = dst.with_name(dst.name + ".meta.json")
        side.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        side.chmod(0o444)
        size = dst.stat().st_size
        counts["cloned" if method == "clone" else "copied"] += 1
        counts["bytes"] += size
        files.append({"path": f"files/{sub.as_posix()}", "sha256": sha, "bytes": size, "source_url": it["url"],
                      "doc_type": meta["doc_type"], "record_ids": meta.get("collector_record_ids", [])})

    universe = None
    if uni_fp:
        snap = tmp / "universe.sqlite3"
        info = snapshot_sqlite(paths.universe_db(), snap)
        if info["integrity_check"] != "ok":
            shutil.rmtree(tmp, ignore_errors=True)
            return result("export", "failed", f"universe snapshot integrity_check: {info['integrity_check']}")
        snap.chmod(0o444)
        universe = {"path": "universe.sqlite3", "sha256": sha256_file(snap), "fingerprint": uni_fp,
                    "synced_at": uni_state.get("synced_at"), **info}

    if not files and universe is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return result("export", "noop", "every pending document changed during the copy", counts=counts)
    counts["exported"] = len(files)
    manifest = {"contract_version": CONTRACT_VERSION, "batch_id": batch_id, "created_at": now_iso(),
                "producer": {"name": "rag-collector", "version": __version__, "git_commit": git_commit(root)},
                "ingest_root": "files", "files": files, "universe": universe, "counts": counts}
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    final = outbox / batch_id
    os.replace(tmp, final)
    ready_tmp = final / ".READY.tmp"
    ready_tmp.write_text(json.dumps({"batch_id": batch_id, "manifest_sha256": sha256_file(final / "manifest.json")}),
                         encoding="utf-8")
    os.replace(ready_tmp, final / "READY")

    for f in files:
        exported[f["sha256"]] = batch_id
    if universe:
        state["universe_fingerprint"] = uni_fp
    state.setdefault("batches", []).append({"batch_id": batch_id, "files": len(files), "universe": bool(universe),
                                            "created_at": manifest["created_at"]})
    save_state(outbox, state)
    return result("export", "ok", f"batch {batch_id}: {len(files)} documents" + (", universe snapshot" if universe else ""),
                  counts=counts, batch_id=batch_id, batch_dir=str(final), more_waiting=more_waiting,
                  next_suggested=["export"] if more_waiting else [])
