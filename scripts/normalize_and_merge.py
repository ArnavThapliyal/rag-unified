"""Normalize parsed candidate rows, deduplicate, and export the master dataset.

Reads  data/processed/parsed/*.jsonl
Writes data/processed/nse_delisted_master.csv / .jsonl
       data/processed/priority_years_audit.csv

Example:
  python scripts/normalize_and_merge.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from datetime import date

from common import (
    PARSED, PRIORITY_YEARS, PROCESSED, SCHEMA_FIELDS, domain_of, emit, log, read_jsonl,
)

CONF_RANK = {"VERIFIED_PRIMARY": 3, "CORROBORATED_SECONDARY": 2, "NEEDS_REVIEW": 1}
TYPES = {"COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT", "OTHER"}
NAME_STOP = re.compile(r"\b(LTD|LIMITED|PVT|PRIVATE|THE|CO|COMPANY|CORPN?|CORPORATION)\b")
MERGE_WINDOW_DAYS = 30

REGIMES = [  # (start date inclusive, label) — newest first
    (date(2024, 9, 25), "SEBI_2024_Amendment"),
    (date(2021, 6, 10), "SEBI_2021_Regs"),
    (date(2009, 6, 10), "SEBI_2009_Regs"),
    (date(1900, 1, 1), "SEBI_2003_Guidelines"),
]


def norm_name(name: str | None) -> str:
    s = (name or "").upper()
    s = re.sub(r"[.,&()'\"\-/]", " ", s)
    s = NAME_STOP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_d(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def financial_year(d: date | None) -> str | None:
    if not d:
        return None
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY{start}-{str(start + 1)[-2:]}"


def regime_for(d: date | None) -> str | None:
    if not d:
        return None
    return next(label for start, label in REGIMES if d >= start)


def normalize(r: dict) -> dict:
    eff, order = parse_d(r.get("effective_delisting_date")), parse_d(r.get("order_date"))
    anchor = eff or order
    year = anchor.year if anchor else None
    isin = (r.get("isin") or "").strip().upper() or None
    sym = (r.get("nse_symbol") or "").strip().upper() or "UNKNOWN"
    dtype = r.get("delisting_type") if r.get("delisting_type") in TYPES else "OTHER"
    name = re.sub(r"\s+", " ", (r.get("company_name") or "")).strip()
    name = re.sub(r"\s*\(Paid Up Value[^)]*\)?\s*$", "", name, flags=re.I) or None  # BSE list suffix
    return {
        "company_name": name,
        "nse_symbol": sym,
        "isin": isin,
        "delisting_type": dtype,
        "order_date": order.isoformat() if order else None,
        "effective_delisting_date": eff.isoformat() if eff else None,
        "calendar_year": year,
        "financial_year": financial_year(anchor),
        "is_priority_year": bool((year in PRIORITY_YEARS) or (order and order.year in PRIORITY_YEARS)),
        "regulatory_regime": regime_for(anchor),
        "exit_or_fair_value_inr": r.get("exit_or_fair_value_inr"),
        "source_url": r.get("source_url"),
        "local_artifact_path": r.get("local_artifact_path"),
        "confidence_status": r.get("confidence_status") if r.get("confidence_status") in CONF_RANK else "NEEDS_REVIEW",
        "_key": isin or norm_name(name),
    }


def same_event(a: dict, b: dict) -> bool:
    if a["_key"] != b["_key"] or a["delisting_type"] != b["delisting_type"]:
        # different types for same ISIN on the same date still count as one event
        if not (a["_key"] == b["_key"] and a["effective_delisting_date"]
                and a["effective_delisting_date"] == b["effective_delisting_date"]):
            return False
    da, db = parse_d(a["effective_delisting_date"]), parse_d(b["effective_delisting_date"])
    if da and db:
        return abs((da - db).days) <= MERGE_WINDOW_DAYS
    return True


def merge_group(group: list[dict]) -> dict:
    group = sorted(group, key=lambda r: (CONF_RANK[r["confidence_status"]],
                                         sum(v is not None and v != "UNKNOWN" for v in r.values())),
                   reverse=True)
    best = dict(group[0])
    for other in group[1:]:
        for k, v in other.items():
            if (best.get(k) in (None, "", "UNKNOWN")) and v not in (None, "", "UNKNOWN"):
                best[k] = v
    urls = list(dict.fromkeys(r["source_url"] for r in group if r.get("source_url")))
    domains = {domain_of(u) for u in urls}
    if best["confidence_status"] == "NEEDS_REVIEW" and len(domains) >= 2:
        best["confidence_status"] = "CORROBORATED_SECONDARY"
    best["source_count"] = len(urls)
    best["all_source_urls"] = " | ".join(urls)
    key_date = best["effective_delisting_date"] or best["order_date"] or ""
    best["record_id"] = hashlib.sha256(
        f"{best['_key']}|{key_date}|{best['delisting_type']}".encode()).hexdigest()[:16]
    return best


def main() -> int:
    raw = []
    for p in sorted(PARSED.glob("*.jsonl")):
        raw.extend(read_jsonl(p))
    proposed_raw = [r for r in raw if r.get("record_status") == "PROPOSED"]
    raw = [r for r in raw if r.get("record_status") != "PROPOSED"]
    rows = [normalize(r) for r in raw]
    rows = [r for r in rows if r["_key"] and r["company_name"]]  # a record needs a company name
    dropped = len(raw) - len(rows)

    groups: list[list[dict]] = []
    by_key: dict[str, list[list[dict]]] = {}
    # Dated rows first, so an undated row can attach to its company's one known event.
    rows.sort(key=lambda r: r["effective_delisting_date"] is None and r["order_date"] is None)
    for r in rows:
        existing = by_key.get(r["_key"], [])
        undated = r["effective_delisting_date"] is None and r["order_date"] is None
        if undated and len(existing) == 1:  # no date = no event identity: it is the same event
            existing[0].append(r)
            continue
        for g in existing:
            if same_event(g[0], r):
                g.append(r)
                break
        else:
            g = [r]
            groups.append(g)
            by_key.setdefault(r["_key"], []).append(g)

    merged = sorted((merge_group(g) for g in groups),
                    key=lambda r: (r["calendar_year"] or 0, r["company_name"] or ""))
    out_of_range = [r["record_id"] for r in merged if r["calendar_year"] and not 2006 <= r["calendar_year"] <= 2026]

    PROCESSED.mkdir(parents=True, exist_ok=True)
    with (PROCESSED / "nse_delisted_master.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)
    with (PROCESSED / "nse_delisted_master.jsonl").open("w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps({k: r.get(k) for k in SCHEMA_FIELDS}, ensure_ascii=False) + "\n")
    audit_fields = SCHEMA_FIELDS + ["source_count", "all_source_urls"]
    priority = [r for r in merged if r["is_priority_year"]]
    with (PROCESSED / "priority_years_audit.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=audit_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(priority)

    # Proposed-delisting leads: companies to verify later (not part of the master dataset).
    leads = {}
    delisted_keys = {r["_key"] for r in merged}
    for r in (normalize(x) for x in proposed_raw):
        if r["_key"] and r["_key"] not in leads:
            r["already_in_master"] = r["_key"] in delisted_keys
            leads[r["_key"]] = r
    lead_fields = ["company_name", "nse_symbol", "isin", "source_url", "local_artifact_path", "already_in_master"]
    with (PROCESSED / "proposed_delisting_leads.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=lead_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(leads.values(), key=lambda r: r["company_name"] or ""))

    summary = {
        "parsed_rows_in": len(raw), "proposed_leads": len(leads), "dropped_no_key": dropped, "records_out": len(merged),
        "duplicates_merged": len(rows) - len(merged), "priority_records": len(priority),
        "by_confidence": {c: sum(r["confidence_status"] == c for r in merged) for c in CONF_RANK},
        "out_of_range_years": out_of_range[:20],
    }
    log(f"merge in={len(raw)} out={len(merged)} priority={len(priority)}", kind="merge",
        **{k: v for k, v in summary.items() if k != "out_of_range_years"})
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
