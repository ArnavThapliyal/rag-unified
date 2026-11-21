"""Concise progress dashboard by year and delisting category.

Examples:
  python scripts/status_report.py
  python scripts/status_report.py --year 2017 --needs-review
  python scripts/status_report.py --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict

from common import MANIFEST, PRIORITY_YEARS, PROCESSED, SEARCH_CACHE, read_jsonl

TYPES = ["COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT", "OTHER"]
SHORT = {"COMPULSORY": "COMP", "VOLUNTARY_EXIT_OFFER": "VOL_EX", "VOLUNTARY_NO_EXIT": "VOL_NO",
         "AMALGAMATION_MERGER": "MERGE", "IBC_NCLT": "IBC", "OTHER": "OTHER"}


def load_master() -> list[dict]:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int)
    ap.add_argument("--needs-review", action="store_true", help="list NEEDS_REVIEW rows (max 25)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = load_master()
    manifest = read_jsonl(MANIFEST)
    fetch = Counter("quarantined" if e.get("quarantined") else "ok" for e in manifest)
    searches = len(list(SEARCH_CACHE.glob("*.json")))

    grid: dict[str, Counter] = defaultdict(Counter)
    conf: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        y = r.get("calendar_year") or "unknown"
        grid[y][r["delisting_type"]] += 1
        conf[y][r["confidence_status"]] += 1
    years = sorted(set(grid) | {str(y) for y in PRIORITY_YEARS}, key=lambda y: (y == "unknown", y))
    if a.year:
        years = [str(a.year)]

    if a.json:
        print(json.dumps({"records": len(rows), "searches_cached": searches, "fetch": fetch,
                          "by_year": {y: dict(grid[y]) for y in years},
                          "confidence_by_year": {y: dict(conf[y]) for y in years}}, default=str))
        return 0

    print(f"Records: {len(rows)} | Searches cached: {searches} | "
          f"Fetched OK: {fetch['ok']} | Quarantined: {fetch['quarantined']}")
    hdr = f"{'YEAR':<9}" + "".join(f"{SHORT[t]:>8}" for t in TYPES) + f"{'TOTAL':>7}{'VERIF':>7}{'REVIEW':>7}"
    print(hdr)
    print("-" * len(hdr))
    for y in years:
        mark = "*" if y.isdigit() and int(y) in PRIORITY_YEARS else " "
        g = grid[y]
        print(f"{y + mark:<9}" + "".join(f"{g[t]:>8}" for t in TYPES)
              + f"{sum(g.values()):>7}{conf[y]['VERIFIED_PRIMARY']:>7}{conf[y]['NEEDS_REVIEW']:>7}")
    print("* = priority year")
    gaps = [f"{y}:{SHORT[t]}" for y in map(str, sorted(PRIORITY_YEARS)) for t in TYPES[:5] if not grid[y][t]]
    if gaps:
        print("Priority gaps (no rows yet): " + ", ".join(gaps))

    if a.needs_review:
        nr = [r for r in rows if r["confidence_status"] == "NEEDS_REVIEW"
              and (not a.year or r.get("calendar_year") == str(a.year))]
        print(f"\nNEEDS_REVIEW ({len(nr)}):")
        for r in nr[:25]:
            print(f"  {r['record_id']} | {r['company_name']} | {r['isin'] or '-'} | "
                  f"{r['effective_delisting_date'] or '-'} | {r['source_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
