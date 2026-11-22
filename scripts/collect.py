"""Autonomous collection run: discover -> fetch -> parse -> merge, driven by config/query_plan.json.

- Resumable: completed queries are tracked in data/metadata/collection_state.json and skipped.
- Logs every step to logs/events.jsonl + logs/agent_run.log, and writes a run summary to
  logs/runs/<run_id>.json (also on Ctrl-C or error).
- Search results from any website are fetched when the plan sets "open_web" (except "deny_domains").
- Downloaded HTML pages are scanned for linked documents (e.g. NSE's orders-of-delisting page links
  ~140 order PDFs); those links are fetched too ("harvesting").
- Each domain has a per-run request budget ("domain_limits"; NSE's is small). URLs over budget are
  kept in collection_state.json "deferred_urls" and fetched by later --harvest-only runs.
- Domains that fail repeatedly are blocked (persisted) so the run stays polite.
- Rebuilds data/metadata/ at the end.

Examples:
  python scripts/collect.py --dry-run                   # show pending queries per phase
  python scripts/collect.py --phase master              # one phase
  python scripts/collect.py --max-queries 10            # next 10 pending queries, any phase
  python scripts/collect.py                             # everything pending, in plan order
  python scripts/collect.py --retry-failed --phase priority_2017
  python scripts/collect.py --harvest-only --max-harvest 60   # fetch leftover linked documents only
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import traceback


import build_metadata
import polite_fetcher
import web_search
from common import (
    CONFIG, MANIFEST, METADATA, PROCESSED, ROOT, RUNS, agent_session, check_disk,
    emit, log, now_iso, polite_sleep, read_jsonl, run_id,
)


class LowDisk(Exception):
    pass

PLAN = CONFIG / "query_plan.json"
STATE = METADATA / "collection_state.json"
PY = sys.executable


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"queries": {}, "blocked_domains": []}


def save_state(state: dict) -> None:
    METADATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def plan_items(plan: dict, phase: str | None) -> list[dict]:
    items = []
    for ph in plan["phases"]:
        if phase and phase not in ("all", ph["name"]):
            continue
        for q in ph["queries"]:
            if isinstance(q, dict):
                items.append({"phase": ph["name"], "query": q["q"], "year_tag": q["year_tag"],
                              "pages": ph.get("pages", 1)})
            else:
                items.append({"phase": ph["name"], "query": q, "year_tag": ph["year_tag"],
                              "pages": ph.get("pages", 1)})
    return items


SEARCH_COOLDOWN_MIN = 30  # after search engines suspend SearXNG, no queries for this long


def search_cooldown_min_left(state: dict) -> int:
    """Minutes until searching is allowed: the global cooldown, or until the first benched engine is back."""
    until = state.get("engines_suspended_until", 0)
    until = max(until, web_search.Search().cooldown_until())
    return max(0, round((until - time.time()) / 60))


def master_count() -> int:
    p = PROCESSED / "nse_delisted_master.csv"
    if not p.exists():
        return 0
    with p.open(encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # records, not lines: quoted fields may hold newlines


def run_script(name: str) -> dict:
    """Run a pipeline script as a subprocess (same run id) and return its JSON summary."""
    r = subprocess.run([PY, str(ROOT / "scripts" / name)], capture_output=True, text=True, cwd=ROOT)
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"{name} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
    return json.loads(lines[-1])


def search_ready() -> dict:
    """Search provider -> None if usable (SearXNG reachable, API key present), else why not."""
    return {p.name: p.health() for p in web_search.Search().providers}


def mark_stale_runs(current: str) -> None:
    """Runs left at status "running" were killed without cleanup (e.g. by a tool timeout)."""
    for p in RUNS.glob("*.json"):
        if p.stem == current:
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("status") == "running":
            r.update(status="killed", stop_reason="process ended without cleanup")
            p.write_text(json.dumps(r, indent=1), encoding="utf-8")


def harvestable_pages(only: list[str] | None = None) -> list[dict]:
    """Manifest entries for downloaded HTML pages (optionally limited to `only` paths)."""
    policy = polite_fetcher.fetch_policy()
    pages = []
    for e in read_jsonl(MANIFEST):
        lp = e.get("local_path") or ""
        if e.get("quarantined") or not lp.endswith((".html", ".htm")):
            continue
        if not polite_fetcher.allowed(e["url"], policy) or (only is not None and lp not in only):
            continue
        pages.append(e)
    return list({p["local_path"]: p for p in pages}.values())


def harvest_backlog() -> int:
    """Linked documents on downloaded pages plus deferred URLs that have not been fetched yet."""
    done = polite_fetcher.already_fetched()
    policy = polite_fetcher.fetch_policy()
    links = {u for pg in harvestable_pages()
             for u in polite_fetcher.harvest_links(ROOT / pg["local_path"], pg["url"], policy)}
    links |= {d["url"] for d in load_state().get("deferred_urls", [])}
    return len(links - set(done))


def defer(state: dict, urls: list[str], tag: str | None) -> None:
    """Remember URLs a domain budget skipped, so a later run fetches them."""
    known = {d["url"] for d in state.setdefault("deferred_urls", [])}
    state["deferred_urls"] += [{"url": u, "year_tag": tag} for u in urls if u not in known]


def fetch_deferred(state: dict, limit: int, blocked: set[str], block_after: int, step: dict) -> dict:
    """Fetch deferred URLs (oldest first) within this run's domain budgets."""
    done = polite_fetcher.already_fetched()
    todo = [d for d in state.get("deferred_urls", []) if d["url"] not in done]
    total = {"fetched_ok": 0, "quarantined": 0}
    for tag in dict.fromkeys(d["year_tag"] for d in todo):
        budget = limit - total["fetched_ok"] - total["quarantined"]
        if budget <= 0:
            break
        f = polite_fetcher.fetch_urls([d["url"] for d in todo if d["year_tag"] == tag], tag, budget,
                                      blocked, block_after)
        total["fetched_ok"] += f["fetched_ok"]
        total["quarantined"] += f["quarantined"]
        step["newly_blocked_domains"] += f["newly_blocked_domains"]
    done = polite_fetcher.already_fetched()
    state["deferred_urls"] = [d for d in state.get("deferred_urls", []) if d["url"] not in done]
    step["harvested_ok"] += total["fetched_ok"]
    step["fetched_ok"] += total["fetched_ok"]
    step["quarantined"] += total["quarantined"]
    if todo:
        log(f"deferred: {len(todo)} waiting, {total['fetched_ok']} ok, {total['quarantined']} quarantined, "
            f"{len(state['deferred_urls'])} left", kind="deferred_fetch", **total)
    return total


def harvest(pages: list[dict], limit: int, blocked: set[str], block_after: int, step: dict) -> dict:
    """Fetch not-yet-fetched document links from `pages`, at most `limit` in total."""
    total = {"links": 0, "fetched_ok": 0, "quarantined": 0}
    state = step["_disk_state"]
    policy = polite_fetcher.fetch_policy()
    for pg in pages:
        if check_disk(state)["level"] == "critical":
            raise LowDisk()
        links = polite_fetcher.harvest_links(ROOT / pg["local_path"], pg["url"], policy)
        total["links"] += len(links)
        budget = limit - total["fetched_ok"] - total["quarantined"]
        if budget <= 0 or not links:
            continue
        f = polite_fetcher.fetch_urls(links, pg.get("year_tag"), budget, blocked, block_after,
                                      harvested_from=pg["url"])
        total["fetched_ok"] += f["fetched_ok"]
        total["quarantined"] += f["quarantined"]
        step["newly_blocked_domains"] += f["newly_blocked_domains"]
        defer(state, f["deferred"], pg.get("year_tag"))
    step["harvested_ok"] += total["fetched_ok"]
    step["fetched_ok"] += total["fetched_ok"]
    step["quarantined"] += total["quarantined"]
    if total["links"]:
        log(f"harvest: {total['links']} links, {total['fetched_ok']} ok, {total['quarantined']} quarantined",
            kind="harvest", **total)
    return total


def bar(done: int, total: int, width: int = 20) -> str:
    filled = int(width * done / total) if total else width
    return "█" * filled + "░" * (width - filled)


def process(step: dict) -> None:
    """Parse + merge, folding the results into the run summary."""
    parsed = run_script("parse_nse_records.py")
    merged = run_script("normalize_and_merge.py")
    step["parsed_files"] += parsed.get("parsed", 0)
    step["parsed_rows"] += parsed.get("rows", 0)
    step["parse_errors"] += len(parsed.get("errors", []))
    step["records_after"] = merged.get("records_out", step["records_after"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="all", help="phase name from config/query_plan.json, or 'all'")
    ap.add_argument("--max-queries", type=int, default=0, help="stop after N queries (0 = no limit)")
    ap.add_argument("--fetch-limit", type=int, help="override fetch_limit_per_query")
    ap.add_argument("--process-every", type=int, default=5, help="parse+merge every N queries")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-run queries that failed or returned no results (e.g. engines were throttled)")
    ap.add_argument("--harvest-only", action="store_true", help="only fetch pending linked documents")
    ap.add_argument("--max-harvest", type=int, help="override harvest_limit_per_page / per harvest-only run")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    state = load_state()
    items = plan_items(plan, a.phase)
    if not items:
        emit({"error": f"unknown or empty phase: {a.phase}",
              "phases": [p["name"] for p in plan["phases"]]})
        return 2

    def pending(it):
        st = state["queries"].get(it["query"], {}).get("status")
        # engines_blocked = searched while SearXNG's engines were suspended: never really searched
        return st in (None, "engines_blocked") or (a.retry_failed and st in ("failed", "no_results"))

    todo = [] if a.harvest_only else [it for it in items if pending(it)]
    if a.max_queries:
        todo = todo[: a.max_queries]

    if a.dry_run:
        by_phase: dict[str, dict] = {}
        for it in items:
            d = by_phase.setdefault(it["phase"], {"total": 0, "pending": 0})
            d["total"] += 1
            d["pending"] += pending(it)
        emit({"dry_run": True, "would_run": len(todo), "phases": by_phase,
              "harvest_backlog": harvest_backlog(), "blocked_domains": state["blocked_domains"],
              "search_cooldown_min_left": search_cooldown_min_left(state),
              "search_engines_usable": web_search.Search().usable(),
              "next": [t["query"] for t in todo[:5]]})
        return 0

    os.environ.pop("NSE_RUN_ID", None)  # every collect.py invocation is its own run
    rid = run_id()
    RUNS.mkdir(parents=True, exist_ok=True)
    mark_stale_runs(rid)
    summary_path = RUNS / f"{rid}.json"
    blocked = set(state["blocked_domains"])
    fetch_limit = a.fetch_limit or plan.get("fetch_limit_per_query", 8)
    policy = polite_fetcher.fetch_policy()
    block_after = plan.get("block_domain_after_failures", 3)
    harvest_limit = a.max_harvest or plan.get("harvest_limit_per_page", 20)
    step = {
        "run_id": rid, "agent_session": agent_session(), "started_at": now_iso(), "finished_at": None, "duration_s": None,
        "status": "running", "stop_reason": None, "args": vars(a),
        "queries_planned": len(todo), "queries_done": 0, "queries_failed": 0, "queries_no_results": 0,
        "phases": {}, "urls_found": 0, "fetched_ok": 0, "quarantined": 0, "skipped_cached": 0,
        "skipped_blocked": 0, "harvested_ok": 0, "harvest_backlog": None, "newly_blocked_domains": [],
        "parsed_files": 0, "parsed_rows": 0,
        "parse_errors": 0, "records_before": master_count(), "records_after": None, "new_records": None,
    }
    step["records_after"] = step["records_before"]
    step["_disk_state"] = state  # check_disk() remembers the last warning tier in collection_state.json
    t0 = time.time()

    def write_summary():
        step["duration_s"] = round(time.time() - t0, 1)
        if step["records_after"] is not None:
            step["new_records"] = step["records_after"] - step["records_before"]
        out = {k: v for k, v in step.items() if not k.startswith("_")}
        summary_path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    log(f"run start: {len(todo)} queries, phase={a.phase}", kind="run_start",
        queries_planned=len(todo), phase=a.phase)
    write_summary()

    if a.harvest_only:
        try:
            d = fetch_deferred(state, harvest_limit, blocked, block_after, step)
            h = harvest(harvestable_pages(), max(0, harvest_limit - d["fetched_ok"] - d["quarantined"]),
                        blocked, block_after, step)
            save_state(state)
            step["harvest_backlog"] = harvest_backlog()
            if h["fetched_ok"] or d["fetched_ok"]:
                process(step)
            step.update(status="completed", stop_reason="harvest_done")
        except LowDisk:
            process(step)
            step.update(status="stopped", stop_reason="low_disk_space")
        except KeyboardInterrupt:
            step.update(status="interrupted", stop_reason="keyboard_interrupt")
        state["blocked_domains"] = sorted(blocked)
        save_state(state)
        step["finished_at"] = now_iso()
        write_summary()
        meta = build_metadata.build()
        log(f"run end (harvest-only): {step['harvested_ok']} fetched, backlog {step['harvest_backlog']}",
            kind="run_end", status=step["status"], harvested_ok=step["harvested_ok"],
            harvest_backlog=step["harvest_backlog"], new_records=step["new_records"])
        emit({k: step[k] for k in ("run_id", "status", "harvested_ok", "quarantined", "harvest_backlog",
                                   "records_before", "records_after", "new_records", "duration_s")}
             | {"indicators": meta["indicators"]})
        return 0

    if not todo:
        step.update(status="completed", stop_reason="nothing_pending", finished_at=now_iso())
        write_summary()
        build_metadata.build()
        emit({k: step[k] for k in ("run_id", "status", "stop_reason", "queries_planned")})
        return 0

    wait_s = state.get("engines_suspended_until", 0) - time.time()
    if wait_s > 0:  # engines rate-limited us recently: don't touch SearXNG until the cooldown ends
        step.update(status="error", stop_reason="search_engines_suspended", finished_at=now_iso())
        write_summary()
        log(f"search cooldown: engines were suspended, {wait_s / 60:.0f} min left; no queries run",
            level="WARN", kind="search_cooldown")
        emit({"run_id": rid, "status": "error", "stop_reason": "search_engines_suspended",
              "retry_after_min": round(wait_s / 60), "hint": "harvest-only still works; otherwise wait"})
        return 1

    ready = search_ready()
    if not any(v is None for v in ready.values()):
        step.update(status="error", stop_reason="search_unavailable", finished_at=now_iso())
        write_summary()
        log(f"no search provider usable: {ready}", level="ERROR", kind="run_error")
        emit({"run_id": rid, "status": "error", "stop_reason": "search_unavailable", "providers": ready})
        return 1

    consecutive_search_errors = consecutive_engine_blocks = 0
    query_delay = web_search.search_settings()["query_delay_s"]
    since_process = 0
    search = web_search.Search()
    try:
        for i, it in enumerate(todo, 1):
            if check_disk(state)["level"] == "critical":
                raise LowDisk()
            q, tag, phase = it["query"], it["year_tag"], it["phase"]
            ph = step["phases"].setdefault(phase, {"queries": 0, "urls": 0, "fetched_ok": 0, "quarantined": 0})
            if i > 1:
                polite_sleep(*query_delay)  # long pause between queries: engines ban fast clients
            entry = {"phase": phase, "year_tag": tag, "ts": now_iso(), "run_id": rid}
            try:
                s = web_search.run(q, it["pages"], tag, search)
                consecutive_search_errors = 0
            except web_search.SearchExhausted as e:  # every provider used up: stop, query stays pending
                state["engines_suspended_until"] = e.until
                save_state(state)
                step["stop_reason"] = "search_engines_suspended"
                log(f"stopping: {e}; queries stay pending", level="ERROR", kind="search_engines_suspended",
                    retry_after_min=round((e.until - time.time()) / 60))
                break
            except Exception as e:  # network/SearXNG errors: record and continue
                consecutive_search_errors += 1
                entry.update(status="failed", error=str(e)[:200])
                state["queries"][q] = entry
                save_state(state)
                step["queries_failed"] += 1
                log(f"search failed ({consecutive_search_errors} in a row): {q}: {e}", level="ERROR",
                    kind="search_error", query=q)
                if consecutive_search_errors >= 3:
                    step["stop_reason"] = "search_errors"
                    break
                continue

            urls = []
            for cf in s["cache_files"]:
                u, _ = polite_fetcher.urls_from_cache_file(ROOT / cf, policy)
                urls += u
            f = polite_fetcher.fetch_urls(urls, tag, fetch_limit, blocked, block_after)
            defer(state, f["deferred"], tag)
            for d in f["newly_blocked_domains"]:
                step["newly_blocked_domains"].append(d)
            h = harvest(harvestable_pages(f["saved"]), harvest_limit, blocked, block_after, step) \
                if f["saved"] else {"fetched_ok": 0}
            state["blocked_domains"] = sorted(blocked)

            engines_blocked = not s["result_count"] and bool(s.get("unresponsive_engines"))
            if engines_blocked:
                consecutive_engine_blocks += 1
                log(f"0 results, unresponsive engines {s['unresponsive_engines']}: {q}", level="WARN",
                    kind="engines_unresponsive", query=q, engines=s["unresponsive_engines"])
            else:
                consecutive_engine_blocks = 0
            status = "done" if s["result_count"] else ("engines_blocked" if engines_blocked else "no_results")
            entry.update(status=status, results=s["result_count"],
                         unresponsive_engines=s.get("unresponsive_engines", []),
                         candidate_urls=len(set(urls)), fetched_ok=f["fetched_ok"],
                         quarantined=f["quarantined"], cache_files=s["cache_files"])
            state["queries"][q] = entry
            save_state(state)

            if consecutive_engine_blocks >= 2:
                # Search engines rate-limit/CAPTCHA SearXNG; more queries would only extend the ban.
                step["stop_reason"] = "search_engines_suspended"
                state["engines_suspended_until"] = time.time() + 60 * SEARCH_COOLDOWN_MIN
                save_state(state)
                log(f"stopping: search engines suspended for {consecutive_engine_blocks} queries in a row "
                    f"({', '.join(s['unresponsive_engines'])}); queries stay pending — retry later",
                    level="ERROR", kind="search_engines_suspended", engines=s["unresponsive_engines"])
                break
            step["queries_done"] += 1
            step["queries_no_results"] += not s["result_count"]
            step["urls_found"] += s["result_count"]
            for k in ("fetched_ok", "quarantined", "skipped_cached", "skipped_blocked"):
                step[k] += f[k]
            ph["queries"] += 1
            ph["urls"] += s["result_count"]
            ph["fetched_ok"] += f["fetched_ok"]
            ph["quarantined"] += f["quarantined"]

            linked = f"  +{h['fetched_ok']} linked docs" if h["fetched_ok"] else ""
            print(f"[{bar(i, len(todo))}] {i}/{len(todo)} {phase:<14} "
                  f"+{s['result_count']:>2} urls  fetched {f['fetched_ok']} ok / {f['quarantined']} quar"
                  f"{linked}  | {q[:55]}", flush=True)
            log(f"query done {i}/{len(todo)}: {q}", kind="query_done", query=q, phase=phase,
                progress=f"{i}/{len(todo)}", results=s["result_count"], fetched_ok=f["fetched_ok"],
                quarantined=f["quarantined"])
            write_summary()

            since_process += (f["fetched_ok"] + h["fetched_ok"]) > 0
            next_phase = todo[i]["phase"] if i < len(todo) else None
            if since_process >= a.process_every or (next_phase != phase and since_process):
                process(step)
                since_process = 0
                print(f"    ↳ processed: records {step['records_after']} "
                      f"(+{step['records_after'] - step['records_before']} this run)", flush=True)
                write_summary()

        if since_process:
            process(step)
        step["status"] = "completed"
        step["stop_reason"] = step["stop_reason"] or "plan_exhausted"
    except LowDisk:
        if since_process:
            process(step)
        step.update(status="stopped", stop_reason="low_disk_space")
    except KeyboardInterrupt:
        step.update(status="interrupted", stop_reason="keyboard_interrupt")
    except Exception as e:
        step.update(status="error", stop_reason=f"{type(e).__name__}: {e}"[:300])
        log(traceback.format_exc()[-800:], level="ERROR", kind="run_error")
    finally:
        step["finished_at"] = now_iso()
        save_state(state)
        step["harvest_backlog"] = harvest_backlog()
        write_summary()
        try:
            meta = build_metadata.build()
        except Exception as e:
            meta = {"error": str(e)}
        log(f"run end: status={step['status']} queries={step['queries_done']} fetched={step['fetched_ok']} "
            f"new_records={step['new_records']}", kind="run_end",
            **{k: step[k] for k in ("status", "stop_reason", "queries_done", "fetched_ok", "quarantined",
                                    "new_records", "duration_s")})

    emit({k: step[k] for k in ("run_id", "status", "stop_reason", "queries_done", "queries_failed",
                               "urls_found", "fetched_ok", "harvested_ok", "harvest_backlog", "quarantined",
                               "parsed_rows",
                               "records_before", "records_after", "new_records", "duration_s",
                               "newly_blocked_domains")}
         | {"summary_file": str(summary_path.relative_to(ROOT)),
            "indicators": meta.get("indicators") if isinstance(meta, dict) else None})
    return 0 if step["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
