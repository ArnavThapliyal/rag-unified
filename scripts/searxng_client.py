"""Query the local SearXNG instance and cache results in data/search_cache/.

Uses the JSON API; if SearXNG answers 403 (json format disabled) it falls back
to parsing the HTML results page.

Examples:
  python scripts/searxng_client.py --query "site:nseindia.com compulsory delisting 2017" --pages 2 --year-tag 2017
  python scripts/searxng_client.py --list-cache
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests
from bs4 import BeautifulSoup

from common import (
    SEARCH_CACHE, SEARXNG_URL, USER_AGENT, emit, log, now_iso, polite_sleep, sha256_text,
)


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


def search_page(session: requests.Session, query: str, page: int,
                retries: int = 3) -> tuple[list[dict], str, list]:
    """Returns (results, mode, unresponsive_engines)."""
    params = {"q": query, "pageno": page, "format": "json"}
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
    for page in range(1, pages + 1):
        path = cache_path(query, page)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if payload and payload["results"]:
            cached_pages += 1
        else:  # not cached, or cached empty (engines may have been throttled) -> ask again
            if page > 1:
                polite_sleep()
            results, mode, unresp = search_page(session, query, page)
            unresponsive.update(str(e[0]) if isinstance(e, list) else str(e) for e in unresp)
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
        urls=len(new_urls), mode=mode, year_tag=year_tag, unresponsive_engines=sorted(unresponsive))
    return {
        "query": query, "year_tag": year_tag, "mode": mode,
        "cache_files": cache_files, "cached_pages": cached_pages,
        "unresponsive_engines": sorted(unresponsive),
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
    a = ap.parse_args()

    if a.list_cache:
        emit(list_cache())
        return 0
    if not a.query:
        ap.error("--query is required")
    try:
        emit(run(a.query, max(1, a.pages), a.year_tag))
    except (requests.RequestException, RuntimeError) as e:
        log(f"query={a.query!r} failed: {e}", level="ERROR", kind="search_error", query=a.query)
        emit({"error": str(e), "query": a.query})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
