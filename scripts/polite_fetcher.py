"""Cached, rate-limited downloader.

- Never fetches the same URL twice (checked against logs/fetch_manifest.jsonl).
- Random 2-5s delay between requests; exponential backoff on 429/5xx.
- Non-200 or empty responses go to data/quarantine/.
- Every attempt is appended to logs/fetch_manifest.jsonl.

Examples:
  python scripts/polite_fetcher.py --url "https://www.nseindia.com/..." --year-tag 2017
  python scripts/polite_fetcher.py --from-cache-file data/search_cache/ab12.json --limit 10 \
      --domains nseindia.com,sebi.gov.in,bseindia.com
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from common import (
    MANIFEST, PRIMARY_DOMAINS, PROCEDURAL_RE, QUARANTINE, ROOT, USER_AGENT, append_jsonl, domain_matches, domain_of, emit, log,
    now_iso, polite_sleep, raw_dir_for_tag, read_jsonl, sha256_bytes, sha256_text,
)

MAX_RETRIES = 4
CONTENT_EXT = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/plain": ".txt",
}


# No .zip: on BSE/NSE these are bundles of scanned newspaper notices (5–15 MB each, no extractable text).
DOC_LINK_RE = re.compile(r"\.(pdf|xlsx?|csv)(\?|#|$)", re.I)
RELEVANT_RE = re.compile(r"delist|exit|compuls|order|suspend|reverse.?book", re.I)


def harvest_links(path: Path, page_url: str) -> list[str]:
    """Document links (PDF/XLS/XLSX/CSV/ZIP) on official domains found in a downloaded HTML page.
    On pages whose URL mentions delisting every document link is kept; elsewhere only links whose
    URL or anchor text looks delisting-related."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "lxml")
    page_relevant = bool(re.search(r"delist", page_url, re.I))
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        url = re.sub(r"(?<!:)//+", "/", url)  # NSE links often contain '//web/...'
        if not DOC_LINK_RE.search(urlparse(url).path) or not domain_matches(url, PRIMARY_DOMAINS):
            continue
        if PROCEDURAL_RE.search(unquote(urlparse(url).path.rsplit("/", 1)[-1])):
            continue
        if page_relevant or RELEVANT_RE.search(url) or RELEVANT_RE.search(a.get_text(" ", strip=True)):
            out.append(url)
    return list(dict.fromkeys(out))


def already_fetched() -> dict[str, dict]:
    """URL -> manifest entry for URLs that must not be requested again: successful downloads and
    definitive client errors (4xx except 429). Timeouts, 429 and 5xx stay retryable."""
    done = {}
    for e in read_jsonl(MANIFEST):
        st = e.get("http_status")
        if (st == 200 and not e.get("quarantined")) or (isinstance(st, int) and 400 <= st < 500 and st != 429):
            done[e["url"]] = e
    return done


def pick_ext(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_EXT:
        return CONTENT_EXT[ct]
    suffix = Path(urlparse(url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return mimetypes.guess_extension(ct) or ".bin"


def fetch_one(session: requests.Session, url: str, year_tag: str | None, harvested_from: str | None = None) -> dict:
    status, body, ctype, err = None, b"", "", None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, timeout=45, allow_redirects=True)
            status, body, ctype = r.status_code, r.content, r.headers.get("Content-Type", "")
            if status == 429 or status >= 500:
                wait = int(r.headers.get("Retry-After", 0) or 0) or 2 ** (attempt + 2)
                time.sleep(min(wait, 120))
                continue
            break
        except requests.RequestException as e:
            err = str(e)
            time.sleep(2 ** (attempt + 2))

    digest = sha256_bytes(body) if body else None
    ok = status == 200 and len(body) > 0
    base = sha256_text(url)[:16]
    ext = pick_ext(url, ctype)
    target_dir = raw_dir_for_tag(year_tag) if ok else QUARANTINE
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{base}{ext}"
    if body:
        path.write_bytes(body)

    entry = {
        "timestamp": now_iso(), "url": url, "http_status": status, "sha256": digest,
        "local_path": str(path.relative_to(ROOT)) if body else None, "year_tag": year_tag,
        "content_type": ctype.split(";")[0] if ctype else None, "bytes": len(body),
        "quarantined": not ok,
    }
    if harvested_from:
        entry["harvested_from"] = harvested_from
    if err and status is None:
        entry["error"] = err
    append_jsonl(MANIFEST, entry)
    log(f"{'OK' if ok else 'QUARANTINE'} {status} {url} -> {entry['local_path']}",
        level="INFO" if ok else "WARN", kind="fetch", url=url, domain=domain_of(url),
        http_status=status, bytes=len(body), quarantined=not ok, year_tag=year_tag,
        local_path=entry["local_path"])
    return entry


def urls_from_cache_file(path: Path, domains: list[str] | None) -> tuple[list[str], str | None]:
    d = json.loads(path.read_text(encoding="utf-8"))
    urls = [r["url"] for r in d.get("results", []) if r.get("url")]
    if domains:
        urls = [u for u in urls if domain_matches(u, domains)]
    return urls, d.get("year_tag")


def fetch_urls(urls: list[str], tag: str | None, limit: int,
               blocked_domains: set[str] | None = None, block_after: int = 3,
               harvested_from: str | None = None) -> dict:
    """Fetch up to `limit` not-yet-fetched URLs. Domains that fail `block_after` times in a row
    (403/429/5xx/timeouts; 404/410 don't count) are added to `blocked_domains` and skipped."""
    blocked = blocked_domains if blocked_domains is not None else set()
    done = already_fetched()
    unique = list(dict.fromkeys(urls))
    pending = [u for u in unique if u not in done]
    skipped_blocked = [u for u in pending if domain_of(u) in blocked]
    todo = [u for u in pending if domain_of(u) not in blocked][:limit]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    ok, quarantined, saved, fails = 0, 0, [], {}
    newly_blocked = []
    for i, url in enumerate(todo):
        d = domain_of(url)
        if d in blocked:
            continue
        if i:
            polite_sleep()
        e = fetch_one(session, url, tag, harvested_from)
        if e["quarantined"]:
            quarantined += 1
            if e["http_status"] in (404, 410):  # missing file: not a sign the site is blocking us
                continue
            fails[d] = fails.get(d, 0) + 1
            if fails[d] >= block_after:
                blocked.add(d)
                newly_blocked.append(d)
                log(f"domain blocked after {fails[d]} consecutive failures: {d}", level="WARN",
                    kind="domain_blocked", domain=d)
        else:
            ok += 1
            fails[d] = 0
            saved.append(e["local_path"])

    return {"requested": len(unique), "skipped_cached": len(unique) - len(pending),
            "skipped_blocked": len(skipped_blocked), "fetched_ok": ok, "quarantined": quarantined,
            "newly_blocked_domains": newly_blocked, "year_tag": tag, "saved": saved}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--from-cache-file", type=Path)
    src.add_argument("--harvest", type=Path, metavar="HTML", help="fetch document links found in a downloaded page")
    ap.add_argument("--year-tag", help="overrides the cache file's year_tag")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--domains", help="comma-separated allowlist, e.g. nseindia.com,sebi.gov.in")
    a = ap.parse_args()

    domains = [x.strip() for x in a.domains.split(",")] if a.domains else None
    m: dict = {}
    if a.url:
        urls, tag = [a.url], a.year_tag
    elif a.harvest:
        p = a.harvest if a.harvest.is_absolute() else ROOT / a.harvest
        m = next((e for e in read_jsonl(MANIFEST) if e.get("local_path") == str(p.relative_to(ROOT))), {})
        urls, tag = harvest_links(p, m.get("url", "")), a.year_tag or m.get("year_tag")
    else:
        p = a.from_cache_file if a.from_cache_file.is_absolute() else ROOT / a.from_cache_file
        urls, tag = urls_from_cache_file(p, domains)
        tag = a.year_tag or tag

    emit(fetch_urls(urls, tag, a.limit, harvested_from=m.get("url") if a.harvest else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
