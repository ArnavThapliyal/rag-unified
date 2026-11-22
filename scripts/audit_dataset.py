"""Data-quality audit of data/processed/nse_delisted_master.csv.

Errors (exit 1): duplicate record_id, malformed ISIN, invalid enum values, rows without a source,
rows from non-listing pages (brokers/members), missing backing artifact.
Warnings: no date at all, year outside 2006–2026, suspicious company names, possible duplicates.

Writes data/metadata/dataset_audit.json (also included in collection_metadata.json).

  python scripts/audit_dataset.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict

from common import METADATA, PROCESSED, ROOT, emit, isin_equity, isin_valid, now_iso

TYPES = {"COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT", "OTHER"}
CONFIDENCE = {"VERIFIED_PRIMARY", "CORROBORATED_SECONDARY", "NEEDS_REVIEW"}
NON_LISTING_RE = re.compile(r"member|broker|defaulter|expelled|arbitrat|complaint|debarred", re.I)
BAD_NAME_RE = re.compile(r"^(nan|nat|none|total|sr\.? ?no\.?|company name|name of the company)$|^\W*$|^\d+$", re.I)
MAX_EXAMPLES = 10


def audit(rows: list[dict]) -> dict:
    errors: dict[str, list] = defaultdict(list)
    warnings: dict[str, list] = defaultdict(list)

    def flag(bucket, check, r):
        bucket[check].append({"record_id": r.get("record_id"), "company_name": r.get("company_name"),
                              "source_url": r.get("source_url")})

    ids = Counter(r.get("record_id") for r in rows)
    by_company = defaultdict(list)
    for r in rows:
        if ids[r.get("record_id")] > 1:
            flag(errors, "duplicate_record_id", r)
        if r.get("isin") and not isin_valid(r["isin"]):
            flag(errors, "malformed_isin", r)
        elif r.get("isin") and not isin_equity(r["isin"]):
            flag(errors, "debt_security_isin", r)
        if r.get("delisting_type") not in TYPES:
            flag(errors, "invalid_delisting_type", r)
        if r.get("confidence_status") not in CONFIDENCE:
            flag(errors, "invalid_confidence_status", r)
        if not r.get("source_url"):
            flag(errors, "missing_source_url", r)
        elif NON_LISTING_RE.search(r["source_url"]):
            flag(errors, "non_listing_source", r)
        lap = r.get("local_artifact_path")
        extra = [x for x in (r.get("all_artifact_paths") or "").split(" | ") if x]
        if not lap or not all((ROOT / x).exists() for x in [lap, *extra]):
            flag(errors, "missing_artifact", r)
        if not r.get("order_date") and not r.get("effective_delisting_date"):
            flag(warnings, "no_date", r)
        y = r.get("calendar_year")
        if y and not 2006 <= int(y) <= 2026:
            flag(warnings, "year_out_of_range", r)
        name = (r.get("company_name") or "").strip()
        if BAD_NAME_RE.search(name) or len(name) < 3:
            flag(warnings, "suspicious_company_name", r)
        key = r.get("isin") or re.sub(r"\W+", "", name.upper())
        by_company[key].append(r)
    for key, group in by_company.items():
        if len(group) > 1 and len({g.get("delisting_type") for g in group}) == 1:
            for r in group:
                flag(warnings, "possible_duplicate_event", r)

    def summarize(d):
        return {k: {"count": len(v), "examples": v[:MAX_EXAMPLES]} for k, v in sorted(d.items())}

    return {"generated_at": now_iso(), "records": len(rows),
            "error_count": sum(len(v) for v in errors.values()),
            "warning_count": sum(len(v) for v in warnings.values()),
            "errors": summarize(errors), "warnings": summarize(warnings)}


def load_rows() -> list[dict]:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run() -> dict:
    result = audit(load_rows())
    METADATA.mkdir(parents=True, exist_ok=True)
    (METADATA / "dataset_audit.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def main() -> int:
    r = run()
    emit({"records": r["records"], "errors": {k: v["count"] for k, v in r["errors"].items()},
          "warnings": {k: v["count"] for k, v in r["warnings"].items()},
          "report": "data/metadata/dataset_audit.json"})
    return 1 if r["error_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
