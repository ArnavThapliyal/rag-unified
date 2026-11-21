"""Concise progress dashboard by year and delisting category.

Examples:
  python scripts/status_report.py
  python scripts/status_report.py --year 2017 --needs-review
  python scripts/status_report.py --json
  python scripts/status_report.py --watch 30      # live dashboard, refresh every 30 s (Ctrl-C to stop)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict

import build_metadata
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


def bar(p: float, width: int = 20) -> str:
    filled = round(width * min(max(p, 0), 100) / 100)
    return "█" * filled + "░" * (width - filled)


def print_indicators(meta: dict) -> None:
    ind = meta["indicators"]
    print("=" * 78)
    print(f" NSE DELISTING COLLECTION — {meta['generated_at']}")
    print("=" * 78)
    for label, val, extra in [
        ("Query plan", ind["query_plan_complete_pct"], f"{ind['queries_done']}/{ind['queries_total']} queries"),
        ("Priority grid", ind["priority_grid_coverage_pct"], "year × type cells with ≥1 record"),
        ("Fetch success", ind["fetch_success_rate_pct"], f"{ind['artifacts_downloaded']} files downloaded"),
        ("Verified primary", ind["verified_primary_pct"], f"{ind['needs_review']} rows NEEDS_REVIEW"),
        ("ISIN completeness", ind["isin_completeness_pct"], "rows with an ISIN"),
    ]:
        print(f" {label:<18} {bar(val)} {val:>5.1f}%  {extra}")
    print(f" Records: {ind['records_total']} (priority years: {ind['priority_year_records']}) | "
          f"files with rows: {ind['artifacts_with_rows']}, zero-row: {ind['artifacts_zero_rows']} | "
          f"blocked domains: {ind['blocked_domains']} | proposed leads: {ind['proposed_delisting_leads']}")
    lr = ind.get("last_run")
    if lr:
        print(f" Last run {lr['run_id']}: {lr['status']} ({lr['stop_reason']}), {lr['queries_done']} queries, "
              f"{lr['fetched_ok']} fetched, +{lr['new_records'] or 0} records, {lr['duration_s']} s")
    print(" Phases: " + "  ".join(f"{n} {p['complete_pct']:.0f}%" for n, p in meta["queries"]["phases"].items()))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int)
    ap.add_argument("--needs-review", action="store_true", help="list NEEDS_REVIEW rows (max 25)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="refresh the dashboard every N seconds")
    a = ap.parse_args()

    if a.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")
                report(a)
                time.sleep(a.watch)
        except KeyboardInterrupt:
            return 0
    return report(a)


def report(a) -> int:
    meta = build_metadata.build()
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
        print(json.dumps({"indicators": meta["indicators"], "records": len(rows), "searches_cached": searches, "fetch": fetch,
                          "by_year": {y: dict(grid[y]) for y in years},
                          "confidence_by_year": {y: dict(conf[y]) for y in years}}, default=str))
        return 0

    print_indicators(meta)
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
