"""Offline regression tests for hardening fixes ported into collector/scripts and rag_collector."""
from __future__ import annotations

import csv
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import audit_dataset
import common
import docket
import ocr
import parse_nse_records
import polite_fetcher
import run_limits
import verify_records
from rag_collector.nse_bridge import ingest_suspended_and_surveillance_csv
from rag_collector.universe import UniverseStore


# ---------------------------------------------------------------------------
# 1. common.py: file_lock, atomic_write_text, append_jsonl concurrency
# ---------------------------------------------------------------------------


def test_common_file_lock_serializes_threads(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / ".state.lock"
    counter_path = tmp_path / "counter.txt"
    counter_path.write_text("0", encoding="utf-8")

    n_threads = 4
    n_iters = 15
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(n_iters):
                with common.file_lock(lock_path):
                    val = int(counter_path.read_text(encoding="utf-8"))
                    counter_path.write_text(str(val + 1), encoding="utf-8")
        except BaseException as exc:  # pragma: no cover - surfaced by assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert int(counter_path.read_text(encoding="utf-8")) == n_threads * n_iters


def test_common_atomic_write_text_no_leftover_tmp(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "state.json"
    common.atomic_write_text(target, '{"v": 1}\n')
    common.atomic_write_text(target, '{"v": 2}\n')

    assert target.read_text(encoding="utf-8") == '{"v": 2}\n'
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        common.atomic_write_text(target, '{"v": 3}\n')

    assert target.read_text(encoding="utf-8") == '{"v": 2}\n'
    assert [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_common_append_jsonl_concurrency_safety(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "events.jsonl"
    n_threads = 6
    n_items = 25
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            barrier.wait()
            for i in range(n_items):
                common.append_jsonl(jsonl_path, {"tid": tid, "i": i, "pad": "x" * 200})
        except BaseException as exc:  # pragma: no cover - surfaced by assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * n_items
    parsed = [json.loads(line) for line in lines]
    seen = {(r["tid"], r["i"]) for r in parsed}
    assert seen == {(tid, i) for tid in range(n_threads) for i in range(n_items)}


# ---------------------------------------------------------------------------
# 2. run_limits.py: StopFile.stop() first-writer-wins via os.link
# ---------------------------------------------------------------------------


def test_stop_file_first_writer_wins(tmp_path: Path) -> None:
    stop_path = tmp_path / "runs" / ".test.stop"
    sf = run_limits.StopFile(stop_path)
    assert sf.reason() is None

    sf.stop("max_files_reached")
    sf.stop("max_companies_reached")

    assert sf.reason() == "max_files_reached"
    assert [p for p in stop_path.parent.iterdir() if p.name.endswith(".tmp")] == []

    # Concurrent first-writer race: winner is a single intact reason and never overwritten.
    race_path = tmp_path / "runs" / ".race.stop"
    race_sf = run_limits.StopFile(race_path)
    reasons = [f"reason_{i}" for i in range(6)]
    barrier = threading.Barrier(len(reasons))
    threads = [
        threading.Thread(target=lambda r=r: (barrier.wait(), race_sf.stop(r)))
        for r in reasons
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winner = race_sf.reason()
    assert winner in reasons
    race_sf.stop("late_override")
    assert race_sf.reason() == winner
    assert [p for p in race_path.parent.iterdir() if p.name.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# 3. docket.py: stale lease_token rejection & atomic max_docs_per_company cap
# ---------------------------------------------------------------------------


def test_docket_stale_lease_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    db_path = tmp_path / "docket.sqlite"
    url = "https://www.nseindia.com/orders/acme_delisting.pdf"

    with docket.Docket(db_path) as dk1, docket.Docket(db_path) as dk2:
        dk1.file([{"url": url, "kind": "search", "year_tag": "2020"}], ["nseindia.com"])
        assert dk1.lease(url) is True

        # Expire dk1's lease so dk2 legitimately takes over with a fresh lease_token.
        dk1.db.execute("UPDATE items SET lease_until=? WHERE url=?", (time.time() - 10, url))
        assert dk2.lease(url) is True

        stale_status = dk1.done_fetch(
            url,
            {"http_status": 200, "local_path": "data/raw/acme.pdf", "quarantined": False},
        )
        assert stale_status == "stale_lease"

        row = dk2.db.execute("SELECT status, local_path FROM items WHERE url=?", (url,)).fetchone()
        assert row["status"] == "fetching"
        assert row["local_path"] is None

        ok_status = dk2.done_fetch(
            url,
            {"http_status": 200, "local_path": "data/raw/acme.pdf", "quarantined": False},
        )
        assert ok_status == "fetched"
        row = dk2.db.execute("SELECT status, local_path FROM items WHERE url=?", (url,)).fetchone()
        assert row["status"] == "fetched"
        assert row["local_path"] == "data/raw/acme.pdf"


def test_docket_atomic_max_docs_per_company_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    db_path = tmp_path / "docket.sqlite"
    urls = [f"https://www.nseindia.com/doc{i}.pdf" for i in (1, 2, 3)]

    with docket.Docket(db_path) as dk:
        dk.file(
            [{"url": u, "kind": "search", "year_tag": "2020", "company": "ACME"} for u in urls],
            ["nseindia.com"],
        )
        assert dk.lease(urls[0], max_docs_per_company=2) is True
        assert dk.lease(urls[1], max_docs_per_company=2) is True
        assert dk.lease(urls[2], max_docs_per_company=2) is False

        row = dk.db.execute("SELECT status, last_error FROM items WHERE url=?", (urls[2],)).fetchone()
        assert row["status"] == "parked"
        assert "max_docs_per_company (2)" in (row["last_error"] or "")


# ---------------------------------------------------------------------------
# 4. polite_fetcher.py: SSRF / private IP / redirect-to-private-IP rejection
# ---------------------------------------------------------------------------


def test_polite_fetcher_rejects_ssrf_and_private_ips(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, type=0):
        mapping = {
            "www.nseindia.com": "93.184.216.34",
            "rebind.example": "127.0.0.1",
            "metadata.example": "169.254.169.254",
        }
        ip = mapping.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert polite_fetcher.validate_fetch_url("https://www.nseindia.com/delisting.pdf") is None

    for bad_url in (
        "file:///etc/passwd",
        "ftp://www.nseindia.com/delisting.pdf",
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://[::1]/local",
        "http://localhost:8082/search",
        "http://service.internal/admin",
        "https://rebind.example/doc.pdf",
        "https://metadata.example/doc.pdf",
    ):
        reason = polite_fetcher.validate_fetch_url(bad_url)
        assert reason is not None, f"expected rejection for {bad_url}"


def test_polite_fetcher_rejects_redirect_to_private_ip(tmp_path: Path, monkeypatch) -> None:
    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(polite_fetcher, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(polite_fetcher, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(polite_fetcher, "ROOT", tmp_path)
    monkeypatch.setattr(polite_fetcher, "log", lambda *a, **k: None)
    monkeypatch.setattr(
        polite_fetcher,
        "fetch_policy",
        lambda: {"open_web": True, "known": ["nseindia.com"], "deny": [], "limits": polite_fetcher.DEFAULT_LIMITS},
    )

    seen_urls: list[str] = []

    class FakeSession:
        def get(self, url, **kwargs):
            seen_urls.append(url)
            return SimpleNamespace(
                status_code=302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                close=lambda: None,
            )

    entry = polite_fetcher.fetch_one(FakeSession(), "https://www.nseindia.com/redirect.pdf", "2020")
    assert seen_urls == ["https://www.nseindia.com/redirect.pdf"]
    assert entry["quarantined"] is True
    assert entry["permanent"] is True
    assert entry["http_status"] is None
    assert "private/local" in (entry.get("error") or "")


# ---------------------------------------------------------------------------
# 5. parse_nse_records.py: equity ISIN priority over non-equity ISIN
# ---------------------------------------------------------------------------


def test_parse_order_prefers_equity_isin_over_debt_isin() -> None:
    equity_isin = "INE002A01018"
    debt_isin = "INE002A07015"
    assert common.isin_valid(equity_isin) and common.isin_equity(equity_isin)
    assert common.isin_valid(debt_isin) and not common.isin_equity(debt_isin)

    order_text = (
        "In the matter of Acme Industries Limited (ACMEIND)\n"
        "Delisting Committee Order\n"
        f"Non-Convertible Debenture ISIN: {debt_isin}\n"
        f"Equity Shares ISIN: {equity_isin}\n"
        "The equity shares of the company shall be compulsorily delisted from the Exchange "
        "w.e.f. July 15, 2020.\n"
        "Date: July 01, 2020\n"
    )
    rows = parse_nse_records.rows_from_order(order_text)
    assert len(rows) == 1
    assert rows[0]["isin"] == equity_isin
    assert rows[0]["delisting_type"] == "COMPULSORY"


# ---------------------------------------------------------------------------
# 6. ocr.py: st_mtime_ns cache invalidation when PDF is newer than cache
# ---------------------------------------------------------------------------


def test_ocr_cache_invalidated_when_pdf_newer_by_mtime_ns(tmp_path: Path, monkeypatch) -> None:
    ocr_dir = tmp_path / "ocr"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    pdf = raw_dir / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    monkeypatch.setattr(ocr, "OCR_DIR", ocr_dir)
    monkeypatch.setattr(ocr, "available", lambda: False)

    cached = ocr.cache_path(pdf)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("CACHED OCR TEXT", encoding="utf-8")

    t0 = 1_700_000_000_000_000_000
    os.utime(pdf, ns=(t0, t0))
    os.utime(cached, ns=(t0 + 1_000_000_000, t0 + 1_000_000_000))

    # Cache is newer than PDF -> cache hit without invoking tesseract.
    assert ocr.ocr_pdf_detail(pdf) == ("CACHED OCR TEXT", [])

    # PDF becomes newer than cache -> cache invalidated; falls through to available() == False.
    os.utime(pdf, ns=(t0 + 2_000_000_000, t0 + 2_000_000_000))
    assert ocr.ocr_pdf_detail(pdf) == ("", [])


# ---------------------------------------------------------------------------
# 7. verify_records.py: empty nse_delisted_master.csv produces valid header
# ---------------------------------------------------------------------------


def test_verify_records_empty_master_writes_valid_header(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    processed = data_dir / "processed"
    parsed = processed / "parsed"
    reference = data_dir / "reference"
    processed.mkdir(parents=True)
    parsed.mkdir(parents=True)
    reference.mkdir(parents=True)

    (processed / "nse_delisted_master.csv").write_text(
        ",".join(common.SCHEMA_FIELDS) + "\n", encoding="utf-8"
    )
    (reference / "nse_equity_listed.csv").write_text(
        "SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE\n"
        "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n",
        encoding="utf-8",
    )

    emitted: list[dict] = []
    monkeypatch.setattr(verify_records, "DATA", data_dir)
    monkeypatch.setattr(verify_records, "PROCESSED", processed)
    monkeypatch.setattr(verify_records, "PARSED", parsed)
    monkeypatch.setattr(verify_records, "REFERENCE", reference)
    monkeypatch.setattr(verify_records, "log", lambda *a, **k: None)
    monkeypatch.setattr(verify_records, "emit", emitted.append)

    rc = verify_records.main()
    assert rc == 0
    assert emitted and emitted[-1]["records"] == 0

    report_path = processed / "verification_report.csv"
    assert report_path.exists()
    with report_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [verify_records.REPORT_FIELDS]


# ---------------------------------------------------------------------------
# 8. audit_dataset.py: non-integer calendar_year emits invalid_calendar_year
# ---------------------------------------------------------------------------


def test_audit_dataset_invalid_calendar_year_emits_error(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "data" / "raw" / "sample.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(audit_dataset, "ROOT", tmp_path)

    base_row = {
        "record_id": "rec-1",
        "company_name": "Acme Industries Limited",
        "nse_symbol": "ACMEIND",
        "isin": "INE002A01018",
        "delisting_type": "COMPULSORY",
        "order_date": "2020-07-01",
        "effective_delisting_date": "2020-07-15",
        "calendar_year": "not-a-year",
        "source_url": "https://www.nseindia.com/orders/acme.pdf",
        "local_artifact_path": "data/raw/sample.pdf",
        "confidence_status": "VERIFIED_PRIMARY",
    }
    second_row = dict(base_row, record_id="rec-2", calendar_year="2020.5")

    result = audit_dataset.audit([base_row, second_row])
    assert "invalid_calendar_year" in result["errors"]
    assert result["errors"]["invalid_calendar_year"]["count"] == 2
    assert result["error_count"] >= 2


# ---------------------------------------------------------------------------
# 9. UniverseStore & nse_bridge: WAL, unique ISIN, status preservation,
#    SUSPENDED + ingest_suspended_and_surveillance_csv, prune_delisting_events
# ---------------------------------------------------------------------------


def test_universe_wal_and_unique_isin_index(tmp_path: Path) -> None:
    db_path = tmp_path / "universe.sqlite3"
    with UniverseStore(db_path) as store:
        mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        idx = store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='nse_entities_isin_unique'"
        ).fetchone()
        assert idx is not None

        store.upsert_nse_entity(
            {
                "isin": "INE002A01018",
                "nse_symbol": "ALPHA",
                "company_name": "Alpha Limited",
                "listing_status": "ACTIVE",
            }
        )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                """
                INSERT INTO nse_entities (isin, nse_symbol, company_name, normalized_name, listing_status)
                VALUES ('INE002A01018', 'BETA', 'Beta Limited', 'BETA', 'ACTIVE')
                """
            )
        # Multiple NULL ISINs remain allowed by the partial unique index.
        store.upsert_nse_entity({"company_name": "Gamma Limited", "listing_status": "DELISTED"})
        store.upsert_nse_entity({"company_name": "Delta Limited", "listing_status": "DELISTED"})
        assert store.count_nse_entities() == 3


def test_universe_preserves_proposed_delisting_on_active_upsert(tmp_path: Path) -> None:
    with UniverseStore(tmp_path / "universe.sqlite3") as store:
        eid1, inserted = store.upsert_nse_entity(
            {
                "isin": "INE002A01018",
                "nse_symbol": "PROPCO",
                "company_name": "Proposed Co Limited",
                "listing_status": "PROPOSED_DELISTING",
            }
        )
        assert inserted is True

        eid2, inserted2 = store.upsert_nse_entity(
            {
                "isin": "INE002A01018",
                "nse_symbol": "PROPCO",
                "company_name": "Proposed Co Limited",
                "listing_date": "2012-03-15",
                "listing_status": "ACTIVE",
            }
        )
        assert eid2 == eid1
        assert inserted2 is False

        entities = store.list_nse_entities()
        assert len(entities) == 1
        assert entities[0]["listing_status"] == "PROPOSED_DELISTING"
        assert entities[0]["listing_date"] == "2012-03-15"


def test_universe_suspended_status_and_ingest_csv(tmp_path: Path) -> None:
    susp_csv = tmp_path / "nse_suspended_equities.csv"
    susp_csv.write_text(
        "SYMBOL,NAME OF COMPANY,ISIN NUMBER,SUSPENSION DATE,REASON,SURVEILLANCE STAGE\n"
        "SUSPCO,Suspended Corp Limited,INE002A01018,15-May-2023,Non-compliance with listing norms,SUSPENDED\n",
        encoding="utf-8",
    )
    surv_csv = tmp_path / "nse_surveillance_asm_gsm.csv"
    surv_csv.write_text(
        "SYMBOL,NAME OF COMPANY,ISIN NUMBER,EFFECTIVE DATE,STAGE,REMARKS\n"
        "SURVCO,Surveillance Corp Limited,INE002A01026,20-Jun-2023,GSM Stage IV,Shortlisted under GSM\n",
        encoding="utf-8",
    )

    with UniverseStore(tmp_path / "universe.sqlite3") as store:
        # Active entity transitions to SUSPENDED on suspended CSV ingestion.
        store.upsert_nse_entity(
            {
                "isin": "INE002A01018",
                "nse_symbol": "SUSPCO",
                "company_name": "Suspended Corp Limited",
                "listing_date": "2010-01-01",
                "listing_status": "ACTIVE",
            }
        )

        susp_res = ingest_suspended_and_surveillance_csv(susp_csv, store)
        assert susp_res == {"suspended_rows": 1, "surveillance_rows": 0}

        # Older ACTIVE listing date does not clobber SUSPENDED.
        store.upsert_nse_entity(
            {
                "isin": "INE002A01018",
                "nse_symbol": "SUSPCO",
                "company_name": "Suspended Corp Limited",
                "listing_date": "2010-01-01",
                "listing_status": "ACTIVE",
            }
        )
        susp_ent = store.list_nse_entities(status="SUSPENDED")
        assert len(susp_ent) == 1
        assert susp_ent[0]["nse_symbol"] == "SUSPCO"
        assert susp_ent[0]["suspension_date"] == "2023-05-15"
        assert susp_ent[0]["suspension_reason"] == "Non-compliance with listing norms"

        surv_res = ingest_suspended_and_surveillance_csv(surv_csv, store)
        assert surv_res == {"suspended_rows": 0, "surveillance_rows": 1}

        active_ent = store.list_nse_entities(status="ACTIVE")
        assert len(active_ent) == 1
        assert active_ent[0]["nse_symbol"] == "SURVCO"
        assert active_ent[0]["surveillance_stage"] == "GSM Stage IV"

        filings = store.list_qualitative_filings()
        categories = {f["announcement_category"] for f in filings}
        assert {"SUSPENSION", "SURVEILLANCE"} <= categories


def test_universe_prune_delisting_events(tmp_path: Path) -> None:
    with UniverseStore(tmp_path / "universe.sqlite3") as store:
        eid, _ = store.upsert_nse_entity(
            {"company_name": "Prune Test Limited", "listing_status": "DELISTED"}
        )
        store.upsert_delisting_event(
            eid, {"record_id": "keep-1", "scope": "NSE_2006_2026", "company_name": "Prune Test Limited"}
        )
        store.upsert_delisting_event(
            eid, {"record_id": "stale-2", "scope": "NSE_2006_2026", "company_name": "Prune Test Limited"}
        )
        store.upsert_delisting_event(
            eid, {"record_id": "pre-1", "scope": "PRE_2006", "company_name": "Prune Test Limited"}
        )

        removed = store.prune_delisting_events("NSE_2006_2026", {"keep-1"})
        assert removed == 1
        assert [e["record_id"] for e in store.list_delisting_events(scope="NSE_2006_2026")] == ["keep-1"]
        assert [e["record_id"] for e in store.list_delisting_events(scope="PRE_2006")] == ["pre-1"]
