"""Unified pipeline: collector outbox batches -> LanceDB, universe snapshot, quant stage, watch loop, step CLI.

Batches are built here exactly as rag-collector's export step writes them (files + .meta.json sidecars,
universe.sqlite3, manifest.json, READY last).
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import pipeline.batches as batches
from pipeline.embedder import Embedder
from pipeline.indexer import Indexer
from pipeline.pipeline import PipelineRunner
from pipeline.quant.store import QuantStore
from pipeline.quant.sync import quant_sync, quant_zfactor
from pipeline.stepkit import EXIT
from pipeline.vector_store import VectorStore
from stub_embedder import StubModel

REPO = Path(__file__).resolve().parent.parent

NOTICE = """<html><body><h1>Delisting of Gone Limited</h1>
<p>Notice is hereby given that the equity shares of Gone Limited (Symbol: GONE, ISIN INE111A01011) are delisted
from the Exchange with effect from June 1, 2007, pursuant to SEBI (Delisting of Securities) Guidelines, 2003.</p>
</body></html>"""


def prices_csv(day_caps):
    lines = ["nse_symbol,trade_date,close,market_cap_cr"]
    for day, caps in day_caps.items():
        for sym, cap in caps.items():
            lines.append(f"{sym},{day},{cap / 10:.2f},{cap}")
    return "\n".join(lines) + "\n"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_universe(path: Path) -> None:
    with QuantStore(path) as s:  # same nse_entities / delisting_events columns as the collector's UniverseStore
        eid, _ = s.upsert_nse_entity({"company_name": "Gone Limited", "nse_symbol": "GONE", "isin": "INE111A01011",
                                      "listing_status": "DELISTED", "latest_delisting_date": "2007-06-01"})
        s.upsert_delisting_event(eid, {"record_id": "r1", "company_name": "Gone Limited", "nse_symbol": "GONE",
                                       "effective_delisting_date": "2007-06-01", "scope": "NSE_2006_2026"})
        for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            s.upsert_nse_entity({"company_name": f"{sym} Limited", "nse_symbol": sym, "listing_status": "ACTIVE"})


def make_batch(outbox: Path, batch_id: str, files: dict, universe: bool = True) -> Path:
    """files: {relative path under files/: (bytes, sidecar dict)}"""
    d = outbox / batch_id
    entries = []
    for rel, (data, meta) in files.items():
        p = d / "files" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.with_name(p.name + ".meta.json").write_text(json.dumps(meta))
        entries.append({"path": f"files/{rel}", "sha256": sha(p), "bytes": len(data),
                        "source_url": meta.get("source_url"), "doc_type": meta.get("doc_type"),
                        "record_ids": meta.get("collector_record_ids", [])})
    uni = None
    if universe:
        make_universe(d / "universe.sqlite3")
        uni = {"path": "universe.sqlite3", "sha256": sha(d / "universe.sqlite3"), "fingerprint": "fp-" + batch_id,
               "integrity_check": "ok", "nse_entities": 6, "delisting_events": 1}
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"contract_version": 1, "batch_id": batch_id, "ingest_root": "files", "files": entries,
                "universe": uni, "producer": {"name": "rag-collector"}}
    (d / "manifest.json").write_text(json.dumps(manifest))
    (d / "READY").write_text(json.dumps({"batch_id": batch_id, "manifest_sha256": sha(d / "manifest.json")}))
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(batches, "BATCH_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(batches, "UNIVERSE_DB", tmp_path / "universe" / "universe.sqlite3")
    store = VectorStore(tmp_path / "db", "documents")

    def run_pipeline(root):
        ix = Indexer(store=store, scratch_dir=tmp_path / "scratch", workers=0,
                     embedder=Embedder(model=StubModel(), batch_size=16))
        return PipelineRunner(indexer=ix).run(root)

    return {"tmp": tmp_path, "outbox": tmp_path / "outbox", "store": store, "run": run_pipeline,
            "universe": tmp_path / "universe" / "universe.sqlite3", "quant": tmp_path / "quant.sqlite3"}


NOTICE_META = {"source_url": "https://nsearchives.nseindia.com/content/circulars/X.html", "doc_type": "delisting_notice",
               "vendor": "NSE", "symbol": "GONE", "nse_symbol": "GONE", "isin": "INE111A01011",
               "published_at": "2007-05-02", "collector_record_ids": ["r1"], "collector_sha256": "abc",
               "collector_batch": "20260101-000000"}


# ---------------------------------------------------------------- batches

def test_only_ready_batches_are_seen(env):
    make_batch(env["outbox"], "20260101-000000", {"a/n.html": (NOTICE.encode(), NOTICE_META)})
    (env["outbox"] / ".20260101-000001.tmp").mkdir()
    (env["outbox"] / "20260101-000002").mkdir()  # no READY yet: the collector is still writing it
    assert [d.name for d in batches.ready_batches(env["outbox"])] == ["20260101-000000"]


@pytest.mark.parametrize("tamper", ["file", "manifest", "contract", "escape", "universe"])
def test_tampered_batches_are_rejected_and_not_ingested(env, tamper):
    d = make_batch(env["outbox"], "20260101-000000", {"a/n.html": (NOTICE.encode(), NOTICE_META)})
    man = json.loads((d / "manifest.json").read_text())
    if tamper == "file":
        (d / "files/a/n.html").write_text("changed")
    elif tamper == "universe":
        (d / "universe.sqlite3").write_bytes(b"not a db")
    else:
        if tamper == "manifest":
            man["files"][0]["bytes"] = 1
        elif tamper == "contract":
            man["contract_version"] = 99
        elif tamper == "escape":
            man["files"][0]["path"] = "../../etc/passwd"
        (d / "manifest.json").write_text(json.dumps(man))
        if tamper != "manifest":  # keep READY consistent so the deeper check is what fails
            (d / "READY").write_text(json.dumps({"batch_id": d.name, "manifest_sha256": sha(d / "manifest.json")}))
    calls = []
    entry = batches.ingest_batch(d, lambda root: calls.append(root) or {})
    assert entry["status"] == "failed" and "rejected" in entry["reason"] and not calls


def test_ingest_batch_into_lancedb_with_collector_metadata(env):
    d = make_batch(env["outbox"], "20260101-000000", {
        "priority_2007/n.html": (NOTICE.encode(), NOTICE_META),
        "master_lists/p.csv": (prices_csv({"2025-06-02": {"AAA": 5000}}).encode(),
                               {"source_url": "https://example.com/p.csv", "doc_type": "delisting_list", "vendor": "BSE"}),
    })
    entry = batches.ingest_batch(d, env["run"])
    assert entry["status"] == "ok" and entry["files_indexed"] == 2 and entry["universe"] == "installed"
    rows = env["store"].open_or_create().table.to_arrow().to_pylist()
    notice = [r for r in rows if r["source_filename"] == "n.html"]
    assert notice and all(r["doc_type"] == "delisting_notice" and r["vendor"] == "NSE" for r in notice)
    assert notice[0]["nse_symbol"] == "GONE" and notice[0]["isin"] == "INE111A01011"
    assert notice[0]["source_url"].startswith("https://nsearchives.nseindia.com/")
    extra = json.loads(notice[0]["metadata_json"])
    assert extra["collector_record_ids"] == ["r1"]          # vectors trace back to the collector's records
    # sidecars and manifest are metadata, never documents
    assert {r["source_filename"] for r in rows} == {"n.html", "p.csv"}
    # done: not pending any more; ingesting it again writes nothing new
    assert batches.pending_batches(env["outbox"]) == []
    again = batches.ingest_batch(d, env["run"])
    assert again["files_indexed"] == 0 and again["chunks_written"] == 0


def test_partial_batches_are_retried_then_given_up(env, monkeypatch):
    monkeypatch.setattr(batches, "BATCH_MAX_ATTEMPTS", 2)
    d = make_batch(env["outbox"], "20260101-000000", {"a/n.html": (NOTICE.encode(), NOTICE_META)}, universe=False)
    fake = lambda root: {"files_found": 1, "files_indexed": 0, "files_failed": 1, "failed_files": ["n.html"]}  # noqa: E731
    assert batches.ingest_batch(d, fake)["status"] == "partial"
    assert [p.name for p in batches.pending_batches(env["outbox"])] == [d.name]   # retried
    batches.ingest_batch(d, fake)
    assert batches.pending_batches(env["outbox"]) == []                           # given up after 2 attempts
    assert batches.batch_states()[d.name]["status"] == batches.GAVE_UP


def test_newest_universe_snapshot_wins(env):
    old = make_batch(env["outbox"], "20260101-000000", {}, universe=True)
    new = make_batch(env["outbox"], "20260102-000000", {}, universe=True)
    assert batches.ingest_batch(new, env["run"])["universe"] == "installed"
    assert batches.ingest_batch(old, env["run"])["universe"] == "older"
    assert batches.read_universe_meta()["batch_id"] == "20260102-000000"
    con = sqlite3.connect(env["universe"])
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM nse_entities").fetchone()[0] == 6
    con.close()


# ---------------------------------------------------------------- quant

def test_quant_end_to_end_from_ingested_price_file(env):
    caps = {"2010-06-01": {"AAA": 120, "BBB": 600, "CCC": 4000, "DDD": 20000, "EEE": 90000, "GONE": 300},
            "2010-06-02": {"AAA": 125, "BBB": 610, "CCC": 4100, "DDD": 20500, "EEE": 91000}}
    d = make_batch(env["outbox"], "20260101-000000", {
        "prices/p.csv": (prices_csv(caps).encode(), {"source_url": "https://example.com/p.csv", "vendor": "test"})})
    assert batches.ingest_batch(d, env["run"])["status"] == "ok"
    kw = dict(universe_db=env["universe"], quant_db=env["quant"], lancedb_path=env["tmp"] / "db", table="documents")
    first = quant_sync(**kw)
    assert first["status"] == "ok", first
    assert first["counts"]["universe_entities"] == 6 and first["counts"]["daily_inserted"] == 11
    assert quant_sync(**kw)["status"] == "noop"                       # nothing new: boundary rows only re-upsert
    z = quant_zfactor(quant_db=env["quant"], lancedb_path=env["tmp"] / "db")
    assert z["status"] == "ok" and z["counts"]["rows_updated"] > 0, z
    import lancedb
    qf = lancedb.connect(str(env["tmp"] / "db")).open_table("quant_factors").to_arrow().to_pylist()
    assert qf and all(r["z_factor_mcap"] is not None for r in qf)
    # point in time: GONE (delisted 2007) is not in the 2010 cross-section
    with QuantStore(env["quant"]) as s:
        gone = [r for r in s.list_daily_market_rows(nse_symbol="GONE")]
    assert gone and gone[0]["z_factor_mcap"] is None


def test_quant_zfactor_without_prices_is_a_noop(env):
    d = make_batch(env["outbox"], "20260101-000000", {"a/n.html": (NOTICE.encode(), NOTICE_META)})
    batches.ingest_batch(d, env["run"])
    quant_sync(universe_db=env["universe"], quant_db=env["quant"], lancedb_path=env["tmp"] / "db", table="documents")
    z = quant_zfactor(quant_db=env["quant"], lancedb_path=env["tmp"] / "db")
    assert z["status"] == "noop" and z["reason"] == "no_price_rows"
    assert quant_zfactor(quant_db=env["tmp"] / "none.sqlite3")["status"] == "blocked"


# ---------------------------------------------------------------- watch + CLI

def test_watch_once_ingests_every_pending_batch_then_quant(env, monkeypatch):
    import pipeline.steps as steps
    import pipeline.watch as watch

    monkeypatch.setattr(steps, "_run_pipeline", lambda root, workers, run_id: env["run"](root))
    monkeypatch.setattr(steps, "_setup", lambda run_id: None)
    monkeypatch.setattr(watch, "STEP_LOCK_DIR", env["tmp"] / "locks")
    monkeypatch.setattr(watch, "STOP_FILE", env["tmp"] / "STOP")
    monkeypatch.setattr(watch, "LOG_DIR", env["tmp"] / "logs")
    import config.settings as settings
    monkeypatch.setattr(settings, "UNIVERSE_DB", env["universe"])
    monkeypatch.setattr(settings, "QUANT_DB", env["quant"])
    monkeypatch.setattr(settings, "LANCEDB_PATH", env["tmp"] / "db")
    for i in range(3):
        make_batch(env["outbox"], f"2026010{i + 1}-000000", {f"a/n{i}.html": (NOTICE.encode() + bytes([i]), NOTICE_META)})
    w = watch.Watch(env["outbox"], poll=0, once=True, workers=0)
    assert w.main() == 0
    assert batches.pending_batches(env["outbox"]) == []
    log = [json.loads(line) for line in (env["tmp"] / "logs" / "watch.jsonl").read_text().splitlines()]
    assert [e["step"] for e in log] == ["ingest", "ingest", "ingest", "quant-sync"]
    assert env["quant"].exists()


def test_watch_stop_file(env, monkeypatch):
    import pipeline.watch as watch

    monkeypatch.setattr(watch, "STOP_FILE", env["tmp"] / "STOP")
    w = watch.Watch(env["outbox"], poll=0, once=False, workers=0)
    (env["tmp"] / "STOP").touch()
    assert w.stop_requested()


def test_step_cli_prints_only_one_json_object(tmp_path):
    e = {**os.environ, "DATA_DIR": str(tmp_path), "COLLECTOR_OUTBOX": str(tmp_path / "outbox")}
    for args, code in ((["list"], 0), (["status"], 0), (["ingest", "--next"], 0)):
        r = subprocess.run([sys.executable, "-m", "pipeline.steps", *args], cwd=REPO, env=e, capture_output=True,
                           text=True, timeout=300)
        assert r.returncode == code, r.stderr[-2000:]
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, r.stdout
        out = json.loads(lines[0])
        assert out["contract_version"] == 1 and out["status"] in EXIT
    assert json.loads(lines[0])["status"] == "noop"   # empty outbox


def test_stepkit_is_identical_to_the_collectors():
    """The contract file must not drift between the repos (rag-unified reads both)."""
    for name in ("collector", "rag-collector", "nse-delisted-comp"):
        other = REPO.parent / name / "src" / "rag_collector" / "stepkit.py"
        if other.exists():
            assert other.read_bytes() == (REPO / "pipeline" / "stepkit.py").read_bytes()
            return
    pytest.skip("collector checkout not found next to this repo")
