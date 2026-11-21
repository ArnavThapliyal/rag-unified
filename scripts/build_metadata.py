"""Build the collection metadata: what has been searched, fetched, parsed, and recorded.

Writes:
  data/metadata/collection_metadata.json   totals, indicators, breakdowns, run + agent-session history
  data/metadata/artifacts.csv              one row per downloaded file (source, status, size, rows parsed)

Run automatically at the end of every collect.py run; safe to run any time:
  python scripts/build_metadata.py
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict

from common import (
    AGENT_SESSIONS, CONFIG, MANIFEST, METADATA, PARSED, PRIORITY_YEARS, PROCESSED, RAW, ROOT, RUNS,
    domain_of, emit, now_iso, read_jsonl,
)

TYPES = ["COMPULSORY", "VOLUNTARY_EXIT_OFFER", "VOLUNTARY_NO_EXIT", "AMALGAMATION_MERGER", "IBC_NCLT"]
COMPLETENESS_FIELDS = ["isin", "nse_symbol", "effective_delisting_date", "order_date", "exit_or_fair_value_inr"]


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def load_master() -> list[dict]:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def query_stats() -> dict:
    plan_p, state_p = CONFIG / "query_plan.json", METADATA / "collection_state.json"
    plan = json.loads(plan_p.read_text(encoding="utf-8")) if plan_p.exists() else {"phases": []}
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {"queries": {}}
    phases = {}
    for ph in plan["phases"]:
        c = Counter()
        for q in ph["queries"]:
            text = q["q"] if isinstance(q, dict) else q
            c[state["queries"].get(text, {}).get("status", "pending")] += 1
        total = len(ph["queries"])
        phases[ph["name"]] = {"total": total, "done": c["done"], "no_results": c["no_results"],
                              "failed": c["failed"], "pending": c["pending"],
                              "complete_pct": pct(total - c["pending"] - c["failed"], total)}
    planned = {(q["q"] if isinstance(q, dict) else q) for ph in plan["phases"] for q in ph["queries"]}
    adhoc = [q for q in state["queries"] if q not in planned]
    return {"phases": phases, "adhoc_queries": len(adhoc),
            "blocked_domains": state.get("blocked_domains", [])}


def fetch_stats(manifest: list[dict]) -> dict:
    by_domain: dict[str, Counter] = defaultdict(Counter)
    status, ctype = Counter(), Counter()
    total_bytes = 0
    for e in manifest:
        d = domain_of(e["url"])
        by_domain[d]["quarantined" if e.get("quarantined") else "ok"] += 1
        by_domain[d]["bytes"] += e.get("bytes") or 0
        status[str(e.get("http_status"))] += 1
        ctype[e.get("content_type") or "unknown"] += 1
        total_bytes += e.get("bytes") or 0
    ok = sum(c["ok"] for c in by_domain.values())
    return {"attempts": len(manifest), "ok": ok, "quarantined": len(manifest) - ok,
            "success_rate_pct": pct(ok, len(manifest)), "total_mb": round(total_bytes / 1e6, 2),
            "by_domain": {d: dict(c) for d, c in sorted(by_domain.items(), key=lambda x: -sum(x[1].values()))},
            "by_http_status": dict(status), "by_content_type": dict(ctype)}


def artifact_inventory(manifest: list[dict]) -> list[dict]:
    by_path = {e["local_path"]: e for e in manifest if e.get("local_path")}
    rows = []
    for p in sorted(x for x in RAW.rglob("*") if x.is_file() and not x.name.startswith(".")):
        rel = str(p.relative_to(ROOT))
        m = by_path.get(rel, {})
        parsed = PARSED / f"{p.parent.name}__{p.stem}.jsonl"
        n_rows = sum(1 for _ in parsed.open(encoding="utf-8")) if parsed.exists() else None
        rows.append({"local_path": rel, "bucket": p.parent.name, "source_url": m.get("url"),
                     "domain": domain_of(m["url"]) if m.get("url") else None,
                     "year_tag": m.get("year_tag"), "content_type": m.get("content_type"),
                     "bytes": p.stat().st_size, "sha256": m.get("sha256"),
                     "fetched_at": m.get("timestamp"), "parsed": parsed.exists(), "rows_extracted": n_rows})
    return rows


def record_stats(rows: list[dict]) -> dict:
    grid = defaultdict(Counter)
    for r in rows:
        grid[r.get("calendar_year") or "unknown"][r["delisting_type"]] += 1
    cells = [(y, t) for y in PRIORITY_YEARS for t in TYPES]
    filled = [f"{y}:{t}" for y, t in cells if grid[str(y)][t]]
    return {
        "total": len(rows),
        "by_year": {y: sum(c.values()) for y, c in sorted(grid.items())},
        "by_type": dict(Counter(r["delisting_type"] for r in rows)),
        "by_confidence": dict(Counter(r["confidence_status"] for r in rows)),
        "by_regime": dict(Counter(r.get("regulatory_regime") or "unknown" for r in rows)),
        "priority_year_records": sum(r.get("is_priority_year") == "True" for r in rows),
        "priority_grid": {str(y): {t: grid[str(y)][t] for t in TYPES} for y in sorted(PRIORITY_YEARS)},
        "priority_grid_cells_filled": len(filled), "priority_grid_cells_total": len(cells),
        "field_completeness_pct": {f: pct(sum(bool(r.get(f)) and r.get(f) != "UNKNOWN" for r in rows), len(rows))
                                   for f in COMPLETENESS_FIELDS},
        "years_covered": sorted(y for y in grid if y != "unknown"),
    }


def run_history(limit: int = 30) -> list[dict]:
    out = []
    for p in sorted(RUNS.glob("*.json"))[-limit:]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({k: r.get(k) for k in ("run_id", "started_at", "status", "stop_reason", "duration_s",
                                          "queries_done", "fetched_ok", "quarantined", "new_records")})
    return out


def agent_sessions(limit: int = 30) -> list[dict]:
    """Summarise Cline --json session logs written by scripts/run_agent.sh."""
    out = []
    for p in sorted(AGENT_SESSIONS.glob("*.jsonl"))[-limit:]:
        tools, first_ts, result = Counter(), None, {}
        for e in read_jsonl(p):
            first_ts = first_ts or e.get("ts")
            ev = e.get("event") or {}
            if ev.get("type") == "content_start" and ev.get("contentType") == "tool":
                tools[ev.get("toolName", "?")] += 1
            if e.get("type") == "run_result":
                result = e
        usage = result.get("aggregateUsage") or result.get("usage") or {}
        out.append({"file": str(p.relative_to(ROOT)), "started_at": first_ts,
                    "finish_reason": result.get("finishReason", "running_or_killed"),
                    "iterations": result.get("iterations"), "tool_calls": sum(tools.values()),
                    "tools": dict(tools), "input_tokens": usage.get("inputTokens"),
                    "output_tokens": usage.get("outputTokens")})
    return out


def leads_count() -> int:
    p = PROCESSED / "proposed_delisting_leads.csv"
    return max(0, sum(1 for _ in p.open(encoding="utf-8")) - 1) if p.exists() else 0


def build() -> dict:
    manifest = read_jsonl(MANIFEST)
    inventory = artifact_inventory(manifest)
    master = load_master()
    queries = query_stats()
    fetch = fetch_stats(manifest)
    records = record_stats(master)
    runs = run_history()
    sessions = agent_sessions()

    total_q = sum(p["total"] for p in queries["phases"].values())
    done_q = sum(p["total"] - p["pending"] - p["failed"] for p in queries["phases"].values())
    parsed = [a for a in inventory if a["parsed"]]
    indicators = {
        "records_total": records["total"],
        "priority_year_records": records["priority_year_records"],
        "verified_primary_pct": pct(records["by_confidence"].get("VERIFIED_PRIMARY", 0), records["total"]),
        "needs_review": records["by_confidence"].get("NEEDS_REVIEW", 0),
        "priority_grid_coverage_pct": pct(records["priority_grid_cells_filled"], records["priority_grid_cells_total"]),
        "query_plan_complete_pct": pct(done_q, total_q),
        "queries_done": done_q, "queries_total": total_q,
        "fetch_success_rate_pct": fetch["success_rate_pct"],
        "artifacts_downloaded": len(inventory),
        "artifacts_with_rows": sum(1 for a in parsed if a["rows_extracted"]),
        "artifacts_zero_rows": sum(1 for a in parsed if a["rows_extracted"] == 0),
        "blocked_domains": len(queries["blocked_domains"]),
        "isin_completeness_pct": records["field_completeness_pct"]["isin"],
        "last_run": runs[-1] if runs else None,
        "agent_sessions": len(sessions),
        "proposed_delisting_leads": leads_count(),
    }
    meta = {"generated_at": now_iso(), "indicators": indicators, "queries": queries, "fetch": fetch,
            "artifacts": {"total": len(inventory), "by_bucket": dict(Counter(a["bucket"] for a in inventory)),
                          "parsed": len(parsed), "rows_extracted": sum(a["rows_extracted"] or 0 for a in parsed)},
            "records": records, "runs": runs, "agent_sessions": sessions}

    METADATA.mkdir(parents=True, exist_ok=True)
    (METADATA / "collection_metadata.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    with (METADATA / "artifacts.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["local_path", "bucket", "source_url", "domain", "year_tag", "content_type", "bytes",
                  "sha256", "fetched_at", "parsed", "rows_extracted"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(inventory)
    return meta


def main() -> int:
    meta = build()
    emit({"written": ["data/metadata/collection_metadata.json", "data/metadata/artifacts.csv"],
          "indicators": meta["indicators"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
