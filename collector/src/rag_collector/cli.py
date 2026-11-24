"""rag-collector queries on the universe DB (the steps themselves are in rag_collector.steps).

    rag-collector universe-as-of 2007-06-01 --limit 10      # survivorship-bias-free universe on a date
    rag-collector delistings --year 2007                    # delisting events from the universe DB
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from rag_collector import paths
from rag_collector.universe import UniverseStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag-collector", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", type=Path, help="default: data/nse_unified.sqlite3 (built by universe-sync)")
    sub = ap.add_subparsers(dest="command", required=True)
    u = sub.add_parser("universe-as-of", help="the NSE universe listed on a historical date")
    u.add_argument("date", metavar="YYYY-MM-DD")
    u.add_argument("--status")
    u.add_argument("--limit", type=int, default=10)
    d = sub.add_parser("delistings", help="delisting events in the universe DB")
    d.add_argument("--year", type=int)
    d.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)

    db = a.database or paths.universe_db()
    if not db.exists():
        print(json.dumps({"error": f"{db} not found: run `python -m rag_collector.steps universe-sync` first"}))
        return 2
    with UniverseStore(db) as store:
        if a.command == "universe-as-of":
            ents = store.list_nse_entities(status=a.status, as_of_date=a.date)
            out = {"as_of_date": a.date, "count": len(ents),
                   "by_status": dict(Counter(e["listing_status"] for e in ents)),
                   "sample": [{k: e.get(k) for k in ("nse_symbol", "isin", "company_name", "listing_status",
                                                     "listing_date", "subsequent_delisting_date")}
                              for e in ents[: max(0, a.limit)]]}
        else:
            evs = store.list_delisting_events()
            if a.year:
                evs = [e for e in evs if e.get("calendar_year") == a.year]
            out = {"count": len(evs), "events": evs[: max(0, a.limit)]}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
