"""Record-level reconciliation between an old master (production) and a regenerated staging output.

  python remediation/tools/reconcile_master.py OLD_MASTER.csv STAGING_PROCESSED_DIR OUT_DIR [OLD_BSE_ONLY.csv]

Reads the staging outputs nse_delisted_master.csv, out_of_scope_delistings.csv, bse_only_delistings.csv and
extraction_review.csv. Writes OUT_DIR/record_reconciliation.csv (one row per old record, plus one per added
record) and OUT_DIR/record_reconciliation_summary.json.

Each old record is traced to the staging record(s) of the same company (shared ISIN, normalized name or NSE
symbol) and the same event (effective/order dates within EVENT_WINDOW_DAYS, or either side undated). Outcomes
(an old record can have several, e.g. "merged;reclassified"):
  unchanged            same event, same name/symbol/ISIN/type/dates
  merged               two or more old records became this one staging record
  split                the old record's source files now back two or more staging records of the company
  date_corrected       effective or order date differs
  date_added / date_removed
  reclassified         delisting_type differs
  ids_changed          ISIN or NSE symbol differs (filled, corrected or removed)
  renamed              company name differs
  moved_out_of_scope   now in out_of_scope_delistings.csv (year outside 2006-2026)
  moved_bse_only       was in the master, now in bse_only_delistings.csv (only BSE sources, no NSE symbol)
  moved_master         was in the old bse_only file, now in the master
  removed_false_entity no staging record; the old name is a field label / OCR garbage, or its source line is in
                       extraction_review.csv
  removed_unmatched    no staging record and no explanation found: needs a human
Staging records no old record maps to are `added`.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from normalize_and_merge import norm_name  # noqa: E402
from parse_nse_records import FIELD_LABEL_RE  # noqa: E402

EVENT_WINDOW_DAYS = 400
FILES = {"master": "nse_delisted_master.csv", "out_of_scope": "out_of_scope_delistings.csv",
         "bse_only": "bse_only_delistings.csv"}


def read(p: Path) -> list[dict]:
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []


def d(s):
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def keys(r: dict) -> set:
    out = set()
    if r.get("isin"):
        out.add(("I", r["isin"]))
    n = norm_name(r.get("company_name"))
    if n:
        out.add(("N", n))
    s = (r.get("nse_symbol") or "").upper()
    if s and s != "UNKNOWN":
        out.add(("S", s))
    return out


def anchor(r: dict):
    return d(r.get("effective_delisting_date")) or d(r.get("order_date"))


def same_event(a: dict, b: dict) -> bool:
    da, db = anchor(a), anchor(b)
    return da is None or db is None or abs((da - db).days) <= EVENT_WINDOW_DAYS


def artifacts(r: dict) -> set:
    return {x.strip() for x in (r.get("all_artifact_paths") or r.get("local_artifact_path") or "").split("|") if x.strip()}


def main() -> int:
    old_path, staging, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    old = [dict(r, _file="master") for r in read(old_path)]
    if len(sys.argv) > 4:
        old += [dict(r, _file="bse_only") for r in read(Path(sys.argv[4]))]
    new = []
    for where, f in FILES.items():
        for r in read(staging / f):
            new.append(dict(r, _file=where))
    review = read(staging / "extraction_review.csv")
    review_paths = {r["local_artifact_path"] for r in review}
    by_key = defaultdict(list)
    for i, r in enumerate(new):
        for k in keys(r):
            by_key[k].append(i)

    def candidates(o: dict) -> list[int]:
        idx = sorted({i for k in keys(o) for i in by_key.get(k, [])})
        same = [i for i in idx if same_event(o, new[i])]
        if anchor(o) and len([i for i in same if anchor(new[i])]) > 1:  # pick the nearest dated event
            dated = [i for i in same if anchor(new[i])]
            best = min(abs((anchor(o) - anchor(new[i])).days) for i in dated)
            same = [i for i in dated if abs((anchor(o) - anchor(new[i])).days) == best] + \
                   [i for i in same if not anchor(new[i])]
        return same

    matches = {o["record_id"]: candidates(o) for o in old}
    # prefer the master copy when the same event also appears in another file
    for rid, idx in matches.items():
        if len(idx) > 1 and any(new[i]["_file"] == "master" for i in idx):
            m = [i for i in idx if new[i]["_file"] == "master"]
            matches[rid] = m if len(m) == 1 else idx
    hits = Counter(i for idx in matches.values() for i in idx)

    rows, summary = [], Counter()
    for o in old:
        idx = matches[o["record_id"]]
        outcomes, changes, notes = [], [], []
        if not idx:
            label = bool(FIELD_LABEL_RE.search(o["company_name"] or "")) or not re.search(r"[A-Za-z]{3}", o["company_name"] or "")
            garbage = label or o.get("local_artifact_path") in review_paths or \
                re.search(r"\b[a-z]+\d\w*\b", o["company_name"] or "")
            outcomes.append("removed_false_entity" if garbage else "removed_unmatched")
            if o.get("local_artifact_path") in review_paths:
                notes.append("source line listed in extraction_review.csv")
            if label:
                notes.append("name contains a field label")
        else:
            # split: this old record's own source files back several staging records of the company
            co = [i for i in sorted({i for k in keys(o) for i in by_key.get(k, [])})
                  if artifacts(o) & artifacts(new[i])]
            if len(co) > 1 and len(idx) == 1:
                outcomes.append("split")
                notes.append("also backs: " + ", ".join(new[i]["record_id"] for i in co if i not in idx))
            if len(idx) > 1:
                outcomes.append("split")
            for i in idx:
                n = new[i]
                if n["_file"] != o["_file"]:
                    outcomes.append("moved_" + n["_file"])
                if hits[i] > 1:
                    outcomes.append("merged")
            n = new[idx[0]]
            for f, oc in (("effective_delisting_date", "date"), ("order_date", "date")):
                a, b = o.get(f) or "", n.get(f) or ""
                if a != b:
                    outcomes.append("date_corrected" if a and b else "date_added" if b else "date_removed")
                    changes.append(f"{f}: {a or '-'} -> {b or '-'}")
            if o["delisting_type"] != n["delisting_type"]:
                outcomes.append("reclassified")
                changes.append(f"delisting_type: {o['delisting_type']} -> {n['delisting_type']}")
            for f in ("isin", "nse_symbol"):
                a, b = (o.get(f) or "").upper(), (n.get(f) or "").upper()
                a, b = ("" if a == "UNKNOWN" else a), ("" if b == "UNKNOWN" else b)
                if a != b:
                    outcomes.append("ids_changed")
                    changes.append(f"{f}: {a or '-'} -> {b or '-'}")
            if (o["company_name"] or "").strip() != (n["company_name"] or "").strip():
                outcomes.append("renamed")
                changes.append(f"company_name: {o['company_name']} -> {n['company_name']}")
            if not outcomes:
                outcomes.append("unchanged")
        outcomes = list(dict.fromkeys(outcomes))
        for x in outcomes:
            summary[x] += 1
        n0 = new[idx[0]] if idx else {}
        rows.append({
            "old_file": o["_file"], "old_record_id": o["record_id"], "old_company_name": o["company_name"], "old_nse_symbol": o["nse_symbol"],
            "old_isin": o["isin"], "old_delisting_type": o["delisting_type"], "old_order_date": o["order_date"],
            "old_effective_delisting_date": o["effective_delisting_date"], "outcomes": ";".join(outcomes),
            "new_record_ids": " | ".join(new[i]["record_id"] for i in idx),
            "new_file": " | ".join(dict.fromkeys(new[i]["_file"] for i in idx)),
            "new_company_name": n0.get("company_name", ""), "new_delisting_type": n0.get("delisting_type", ""),
            "new_order_date": n0.get("order_date", ""), "new_effective_delisting_date": n0.get("effective_delisting_date", ""),
            "new_date_source": n0.get("date_source", ""), "new_review_flags": n0.get("review_flags", ""),
            "new_source_conflicts": n0.get("source_conflicts", ""),
            "changes": "; ".join(changes), "notes": "; ".join(notes)})
    added = [i for i in range(len(new)) if not hits[i]]
    for i in added:
        n = new[i]
        summary["added_" + n["_file"]] += 1
        rows.append({"old_record_id": "", "outcomes": "added", "new_record_ids": n["record_id"], "new_file": n["_file"],
                     "new_company_name": n["company_name"], "new_delisting_type": n["delisting_type"],
                     "new_order_date": n["order_date"], "new_effective_delisting_date": n["effective_delisting_date"],
                     "new_date_source": n.get("date_source", ""), "new_review_flags": n.get("review_flags", ""),
                     "new_source_conflicts": n.get("source_conflicts", ""),
                     "notes": "sources: " + (n.get("all_artifact_paths") or "")[:300]})
    fields = ["old_file", "old_record_id", "old_company_name", "old_nse_symbol", "old_isin", "old_delisting_type", "old_order_date",
              "old_effective_delisting_date", "outcomes", "new_record_ids", "new_file", "new_company_name",
              "new_delisting_type", "new_order_date", "new_effective_delisting_date", "new_date_source",
              "new_review_flags", "new_source_conflicts", "changes", "notes"]
    with (out / "record_reconciliation.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    res = {"old_records": dict(Counter(o["_file"] for o in old)), "staging_records": {k: sum(r["_file"] == k for r in new) for k in FILES},
           "extraction_review_candidates": len(review), "event_window_days": EVENT_WINDOW_DAYS,
           "outcome_counts": dict(sorted(summary.items())),
           "old_records_accounted_for": sum(1 for r in rows if r["old_record_id"]),
           "staging_records_merged_from_several_old": sum(1 for c in hits.values() if c > 1)}
    (out / "record_reconciliation_summary.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
