"""Query the local SearXNG instance and cache results in data/search_cache/.

Uses the JSON API; if SearXNG answers 403 (json format disabled) it falls back
to parsing the HTML results page.

Engine rotation: each request asks only `engines_per_query` engines (the ones rested longest), so no
single engine sees every query. An engine that comes back rate-limited / CAPTCHA'd / denied is benched
for a cooldown that depends on the reason, and one whose answers are junk (no result on a known source
domain and none mentioning delisting, e.g. Bing answering with unrelated forums) is benched too. State: data/metadata/engine_state.json.
Settings: the "search" block of config/query_plan.json.

Examples:
  python scripts/searxng_client.py --query "site:nseindia.com compulsory delisting 2017" --pages 2 --year-tag 2017
  python scripts/searxng_client.py --list-cache
  python scripts/searxng_client.py --engines          # rotation state: usable / benched engines
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from common import (
    CONFIG, METADATA, SEARCH_CACHE, SEARXNG_URL, USER_AGENT, domain_matches, emit, log, now_iso,
    polite_sleep, sha256_text,
)

ENGINE_STATE = METADATA / "engine_state.json"
SEARCH_DEFAULTS = {
    "engines": ["google cse", "brave", "duckduckgo", "startpage", "bing", "mojeek",
                "yahoo", "qwant", "dogpile", "presearch", "yep"],
    "engines_per_query": 2,
    # minutes an engine is benched, by the reason SearXNG reports (substring match, lower case)
    "cooldown_min": {"too many requests": 60, "suspended": 60, "captcha": 360, "access denied": 720,
                     "irrelevant": 360, "default": 120},
    "irrelevant_after": 3,          # consecutive answers with no relevant result -> benched
    "query_delay_s": [20, 40],      # pause between queries (collect.py)
}


# A result is relevant if it is on a known source domain or its text is about delisting.
RELEVANT_RESULT_RE = re.compile(r"delist|exit offer|reverse book|compulsor|nseindia|bseindia|sebi", re.I)


def relevant(r: dict, domains) -> bool:
    text = " ".join(str(r.get(k) or "") for k in ("url", "title", "content"))
    return domain_matches(r.get("url", ""), domains) or bool(RELEVANT_RESULT_RE.search(text))


class EnginesExhausted(RuntimeError):
    """Every engine in the pool is benched; `until` is when the first one is usable again."""
    def __init__(self, until: float):
        super().__init__(f"all search engines benched; next usable in {(until - time.time()) / 60:.0f} min")
        self.until = until


def search_settings() -> dict:
    try:
        cfg = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    s = {**SEARCH_DEFAULTS, **cfg.get("search", {})}
    s["cooldown_min"] = {**SEARCH_DEFAULTS["cooldown_min"], **cfg.get("search", {}).get("cooldown_min", {})}
    s["official_domains"] = cfg.get("fetch_domains", ["nseindia.com", "sebi.gov.in", "bseindia.com"])
    return s


class Rotation:
    def __init__(self, settings: dict | None = None):
        self.s = settings or search_settings()
        try:
            self.state = json.loads(ENGINE_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        for e in self.s["engines"]:
            self.state.setdefault(e, {"benched_until": 0, "reason": None, "last_used": 0, "uses": 0,
                                      "results": 0, "official": 0, "irrelevant_streak": 0})

    def save(self) -> None:
        ENGINE_STATE.parent.mkdir(parents=True, exist_ok=True)
        ENGINE_STATE.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def usable(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        free = [e for e in self.s["engines"] if self.state[e]["benched_until"] <= now]
        return sorted(free, key=lambda e: self.state[e]["last_used"])  # longest-rested first

    def pick(self, exclude: set[str] = frozenset()) -> list[str]:
        free = [e for e in self.usable() if e not in exclude]
        if not free:
            raise EnginesExhausted(min(v["benched_until"] for k, v in self.state.items() if k in self.s["engines"]))
        return free[: self.s["engines_per_query"]]

    def bench(self, engine: str, reason: str) -> None:
        r = reason.lower()
        # "Suspended: access denied" matches both keys: the longer cooldown wins
        minutes = max((m for k, m in self.s["cooldown_min"].items() if k != "default" and k in r),
                      default=self.s["cooldown_min"]["default"])
        st = self.state[engine]
        st.update(benched_until=time.time() + 60 * minutes, reason=reason)
        log(f"engine {engine} benched {minutes} min: {reason}", level="WARN", kind="engine_benched",
            engine=engine, reason=reason, minutes=minutes)

    def record(self, engines: list[str], results: list[dict], unresponsive: list) -> None:
        failed = {str(e[0]): str(e[1]) if isinstance(e, list) and len(e) > 1 else "unresponsive"
                  for e in unresponsive}
        for e in engines:
            st = self.state[e]
            st["last_used"], st["uses"] = time.time(), st["uses"] + 1
            if e in failed:
                self.bench(e, failed[e])
                continue
            mine = [r for r in results if e in (r.get("engines") or [])]
            official = sum(domain_matches(r.get("url", ""), self.s["official_domains"]) for r in mine)
            useful = sum(relevant(r, self.s["official_domains"]) for r in mine)
            st["results"] += len(mine)
            st["official"] += official
            # empty or junk-only answers count toward benching the engine
            st["irrelevant_streak"] = 0 if useful else st["irrelevant_streak"] + 1
            if st["irrelevant_streak"] >= self.s["irrelevant_after"]:
                st["irrelevant_streak"] = 0
                self.bench(e, f"irrelevant: {self.s['irrelevant_after']} answers in a row with no relevant result")
        self.save()

    def report(self) -> dict:
        now = time.time()
        return {"usable": self.usable(now),
                "benched": {e: {"minutes_left": round((v["benched_until"] - now) / 60), "reason": v["reason"]}
                            for e, v in self.state.items() if e in self.s["engines"] and v["benched_until"] > now},
                "stats": {e: {k: v[k] for k in ("uses", "results", "official")} for e, v in self.state.items()
                          if e in self.s["engines"]}}


def cache_path(query: str, page: int):
    return SEARCH_CACHE / f"{sha256_text(f'{query}|{page}')[:16]}.json"


def parse_html_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for art in soup.select("article.result"):
        a = art.select_one("h3 a") or art.select_one("a.url_header") or art.select_one("a")
        if not a or not a.get("href"):
            continue
        content = art.select_one(".content")
        results.append({
            "url": a["href"],
            "title": a.get_text(" ", strip=True),
            "content": content.get_text(" ", strip=True) if content else "",
            "engines": [],
        })
    return results


def search_page(session: requests.Session, query: str, page: int, engines: list[str] | None = None,
                retries: int = 3) -> tuple[list[dict], str, list]:
    """Returns (results, mode, unresponsive_engines)."""
    params = {"q": query, "pageno": page, "format": "json"}
    if engines:
        params["engines"] = ",".join(engines)
    for attempt in range(retries):
        r = session.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
        if r.status_code == 200:
            d = r.json()
            return d.get("results", []), "json", d.get("unresponsive_engines", [])
        if r.status_code == 403:
            params.pop("format")
            h = session.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
            h.raise_for_status()
            return parse_html_results(h.text), "html_fallback", []
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"SearXNG failed after {retries} attempts for page {page}")


def run(query: str, pages: int, year_tag: str | None) -> dict:
    SEARCH_CACHE.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    seen: set[str] = set()
    cache_files, new_urls, mode, cached_pages = [], [], "json", 0

    unresponsive: set[str] = set()
    rotation = Rotation()
    used_engines: set[str] = set()
    for page in range(1, pages + 1):
        path = cache_path(query, page)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if payload and payload["results"]:
            cached_pages += 1
        else:  # not cached, or cached empty (engines may have been throttled) -> ask again
            if page > 1:
                polite_sleep()
            tried: set[str] = set()
            while True:  # if both engines fail, try the next pair once; EnginesExhausted if none left
                engines = rotation.pick(exclude=tried)
                tried.update(engines)
                results, mode, unresp = search_page(session, query, page, engines)
                rotation.record(engines, results, unresp)
                used_engines.update(engines)
                unresponsive.update(str(e[0]) if isinstance(e, list) else str(e) for e in unresp)
                if results or len(tried) >= 2 * rotation.s["engines_per_query"]:
                    break
                polite_sleep()
            payload = {
                "query": query, "page": page, "year_tag": year_tag, "mode": mode,
                "retrieved_at": now_iso(), "unresponsive_engines": unresp,
                "results": [
                    {k: r.get(k) for k in ("url", "title", "content", "engines", "publishedDate")}
                    for r in results
                ],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        cache_files.append(str(path.relative_to(SEARCH_CACHE.parent.parent)))
        page_new = [r["url"] for r in payload["results"] if r.get("url") and r["url"] not in seen]
        seen.update(page_new)
        new_urls.extend(page_new)
        if not page_new:
            break

    log(f"query={query!r} pages={len(cache_files)} urls={len(new_urls)} mode={mode} tag={year_tag}",
        kind="search", query=query, pages=len(cache_files), cached_pages=cached_pages,
        urls=len(new_urls), mode=mode, year_tag=year_tag, unresponsive_engines=sorted(unresponsive),
        engines=sorted(used_engines))
    return {
        "query": query, "year_tag": year_tag, "mode": mode,
        "cache_files": cache_files, "cached_pages": cached_pages,
        "unresponsive_engines": sorted(unresponsive), "engines_used": sorted(used_engines),
        "result_count": len(new_urls), "top_urls": new_urls[:10],
    }


def list_cache() -> dict:
    entries = []
    for p in sorted(SEARCH_CACHE.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries.append({"file": p.name, "query": d.get("query"), "page": d.get("page"),
                        "year_tag": d.get("year_tag"), "results": len(d.get("results", []))})
    return {"cached": len(entries), "entries": entries}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", "-q")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--year-tag", help="2007|2017|2020|2024|master|<year>")
    ap.add_argument("--list-cache", action="store_true")
    ap.add_argument("--engines", action="store_true", help="show engine rotation state")
    a = ap.parse_args()

    if a.engines:
        emit(Rotation().report())
        return 0
    if a.list_cache:
        emit(list_cache())
        return 0
    if not a.query:
        ap.error("--query is required")
    try:
        emit(run(a.query, max(1, a.pages), a.year_tag))
    except (requests.RequestException, RuntimeError) as e:  # incl. EnginesExhausted
        log(f"query={a.query!r} failed: {e}", level="ERROR", kind="search_error", query=a.query)
        emit({"error": str(e), "query": a.query})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
