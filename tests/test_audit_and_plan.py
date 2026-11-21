import json
from pathlib import Path

import audit_dataset

ROOT = Path(__file__).resolve().parent.parent


def _row(**kw):
    base = {"record_id": "a1", "company_name": "ABC Ltd", "nse_symbol": "ABC", "isin": "INE123A01016",
            "delisting_type": "COMPULSORY", "order_date": "2017-01-01", "effective_delisting_date": "",
            "calendar_year": "2017", "source_url": "https://www.nseindia.com/x", "local_artifact_path": "README.md",
            "confidence_status": "VERIFIED_PRIMARY"}
    return {**base, **kw}


def test_clean_row_passes():
    r = audit_dataset.audit([_row()])
    assert r["error_count"] == 0 and r["warning_count"] == 0


def test_bad_rows_are_flagged():
    rows = [
        _row(record_id="dup"), _row(record_id="dup", company_name="Other Ltd", isin=""),
        _row(record_id="b", isin="INE123", company_name="Bad Isin Ltd"),
        _row(record_id="c", source_url="https://www.nseindia.com/static/complaints/defaulter-expelled-members",
             company_name="Broker Ltd", isin=""),
        _row(record_id="d", local_artifact_path="data/raw/missing.pdf", company_name="Gone Ltd", isin=""),
        _row(record_id="e", company_name="NaT", isin="", order_date=""),
    ]
    r = audit_dataset.audit(rows)
    for check in ("duplicate_record_id", "malformed_isin", "non_listing_source", "missing_artifact"):
        assert check in r["errors"], check
    for check in ("no_date", "suspicious_company_name"):
        assert check in r["warnings"], check


def test_query_plan_is_valid():
    plan = json.loads((ROOT / "config" / "query_plan.json").read_text())
    assert {"fetch_domains", "phases", "disk"} <= set(plan)
    seen = set()
    for ph in plan["phases"]:
        assert ph["name"] and ph["queries"]
        for q in ph["queries"]:
            text, tag = (q["q"], q["year_tag"]) if isinstance(q, dict) else (q, ph["year_tag"])
            assert tag == "master" or (tag.isdigit() and 2006 <= int(tag) <= 2026), (ph["name"], tag)
            assert text not in seen, f"duplicate query: {text}"
            seen.add(text)
