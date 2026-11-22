"""Fetch worker: downloads docket items (scripts/docket.py), best first, at each domain's pace.

- Per-domain budget and delay from config/query_plan.json "domain_limits" (NSE: few requests, long
  pauses). Domains are interleaved: while one domain waits out its delay, another is fetched.
  Items over a domain's budget stay pending for the next run (nothing is lost).
- A failed download is retried later from the docket with backoff (30 min, 2 h, 8 h), never by searching
  again; 404/410 and other definitive 4xx are marked dead at once. Domains that fail `block_domain_after_failures`
  times in a row are blocked; their items wait.
- Downloaded HTML pages are scanned for linked documents, which are filed in the docket ("harvested";
  the first harvest_limit_per_page per page pending, the rest parked).
- --follow FILE: keep waiting for new items while FILE exists (Discovery creates it and deletes it when
  done; --follow-pid stops the wait if that process dies without deleting it). Otherwise stop when
  nothing is ready.

  python scripts/fetch_worker.py --max-fetch 40
  python scripts/fetch_worker.py --follow logs/runs/.x.discovering --follow-pid 12345
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

import common
import docket
import polite_fetcher
from common import CONFIG, ROOT, USER_AGENT, check_disk, domain_of, emit, log

IDLE_POLL_S = 5
DISK_CHECK_S = 60


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def discovering(marker: Path | None, pid: int | None) -> bool:
    return bool(marker) and marker.exists() and (pid is None or alive(pid))


def plan() -> dict:
    try:
        return json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def harvest(dk: docket.Docket, entry: dict, policy: dict, per_page: int, run_id: str | None) -> dict:
    links = polite_fetcher.harvest_links(ROOT / entry["local_path"], entry["url"], policy)
    items = [{"url": u, "kind": "harvested", "year_tag": entry.get("year_tag"), "harvested_from": entry["url"],
              "rank": i, "parked": i > per_page, "run_id": run_id} for i, u in enumerate(links, 1)]
    return dk.file(items, policy["known"], polite_fetcher.already_fetched()) if items else {"new": 0}


def run(max_fetch: int, follow: Path | None = None, follow_pid: int | None = None,
        max_seconds: float | None = None) -> dict:
    dk = docket.open_docket()
    p = plan()
    policy = polite_fetcher.fetch_policy()
    block_after = p.get("block_domain_after_failures", 3)
    per_page = p.get("harvest_limit_per_page", 20)
    run_id = os.environ.get("NSE_RUN_ID")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    next_ok: dict[str, float] = {}           # domain_limits key -> earliest next request
    used = polite_fetcher.RUN_COUNTS          # requests per key in this run
    disk_state = dk.get_meta("disk_state", {})
    out = {"fetched_ok": 0, "failed": 0, "dead": 0, "harvested_new": 0, "newly_blocked_domains": [],
           "over_budget_domains": [], "stop_reason": None}
    t0, last_disk = time.time(), 0.0
    try:
        while True:
            if out["fetched_ok"] + out["failed"] + out["dead"] >= max_fetch:
                out["stop_reason"] = "max_fetch"
                break
            if max_seconds and time.time() - t0 > max_seconds:
                out["stop_reason"] = "time_limit"
                break
            if time.time() - last_disk >= DISK_CHECK_S:  # sizing data/ takes a while: once a minute
                last_disk = time.time()
                if check_disk(disk_state)["level"] == "critical":
                    out["stop_reason"] = "low_disk_space"
                    break
            now = time.time()
            cands = dk.candidates()
            pick, soonest, over = None, None, set()
            for c in cands:
                key = polite_fetcher.limit_key(c["url"], policy)
                lim = polite_fetcher.limits_for(key, policy)
                if used[key] >= lim["max_per_run"]:
                    over.add(key)
                    continue
                if next_ok.get(key, 0) > now:
                    soonest = min(soonest or next_ok[key], next_ok[key])
                    continue
                if not polite_fetcher.allowed(c["url"], policy):  # policy changed since it was filed
                    dk.done_fetch(c["url"], {"http_status": None, "quarantined": True,
                                             "error": "not allowed by fetch policy (deny_domains/open_web)"})
                    continue
                pick = (c, key, lim)
                break
            out["over_budget_domains"] = sorted(over)
            if not pick:
                if soonest:                       # only domain delays in the way: wait for the first
                    time.sleep(max(0.2, min(soonest - now, IDLE_POLL_S)))
                    continue
                if discovering(follow, follow_pid):  # Discovery is still filing: wait for more
                    time.sleep(IDLE_POLL_S)
                    continue
                out["stop_reason"] = "budget_exhausted" if over else "idle"
                break
            c, key, lim = pick
            if not dk.lease(c["url"]):            # another worker took it
                continue
            used[key] += 1
            e = polite_fetcher.fetch_one(session, c["url"], c["year_tag"], c["harvested_from"])
            next_ok[key] = time.time() + random.uniform(*lim["delay_s"])
            status = dk.done_fetch(c["url"], e)
            ok = status == "fetched"
            out["fetched_ok" if ok else status] += 1
            if dk.domain_result(domain_of(c["url"]), ok, e.get("http_status"), block_after):
                out["newly_blocked_domains"].append(domain_of(c["url"]))
                log(f"domain blocked after {block_after} consecutive failures: {domain_of(c['url'])}",
                    level="WARN", kind="domain_blocked", domain=domain_of(c["url"]))
            if ok and (e.get("local_path") or "").endswith((".html", ".htm")):
                out["harvested_new"] += harvest(dk, e, policy, per_page, run_id).get("new", 0)
            print(f"  ⇣ {status:<8} {e.get('http_status')} {c['url'][:90]}", flush=True)
    except KeyboardInterrupt:
        out["stop_reason"] = "keyboard_interrupt"
    finally:
        dk.set_meta("disk_state", disk_state)
        out["docket"] = dk.write_stats()
        out["duration_s"] = round(time.time() - t0, 1)
        log(f"fetch worker: {out['fetched_ok']} ok, {out['failed']} failed (will retry), {out['dead']} dead, "
            f"+{out['harvested_new']} linked docs filed; stop: {out['stop_reason']}", kind="fetch_worker",
            **{k: out[k] for k in ("fetched_ok", "failed", "dead", "harvested_new", "stop_reason")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-fetch", type=int, default=60, help="download attempts this run")
    ap.add_argument("--follow", type=Path, metavar="FILE", help="keep waiting for new items while FILE exists")
    ap.add_argument("--follow-pid", type=int, metavar="PID", help="...and while this process is alive")
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--summary-out", type=Path)
    a = ap.parse_args()
    common.graceful_sigterm()
    out = run(a.max_fetch, a.follow, a.follow_pid, a.max_seconds)
    if a.summary_out:
        a.summary_out.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    emit({k: v for k, v in out.items() if k != "docket"} | {"docket_ready": out["docket"]["fetch_ready_now"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
