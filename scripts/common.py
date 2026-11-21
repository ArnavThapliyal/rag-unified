"""Shared paths, logging, and helpers for the NSE delisting pipeline.

CLI: `python scripts/common.py log "message"` appends a milestone to logs/agent_run.log
(and a structured `agent_note` event to logs/events.jsonl).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEARCH_CACHE = DATA / "search_cache"
RAW = DATA / "raw"
QUARANTINE = DATA / "quarantine"
PROCESSED = DATA / "processed"
PARSED = PROCESSED / "parsed"
LOGS = ROOT / "logs"
MANIFEST = LOGS / "fetch_manifest.jsonl"
AGENT_LOG = LOGS / "agent_run.log"
EVENTS = LOGS / "events.jsonl"
RUNS = LOGS / "runs"
AGENT_SESSIONS = LOGS / "agent_sessions"
METADATA = DATA / "metadata"
CONFIG = ROOT / "config"
SCHEMA_CSV = ROOT / "nse_delisted_master.csv"

SEARXNG_URL = "http://localhost:8082"
PRIORITY_YEARS = {2007, 2017, 2020, 2024}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 nse-delisting-research/1.0"
)

PRIMARY_DOMAINS = ("nseindia.com", "sebi.gov.in", "bseindia.com")
SECONDARY_DOMAINS = (
    "business-standard.com", "economictimes.indiatimes.com", "moneycontrol.com",
    "livemint.com", "thehindubusinessline.com", "financialexpress.com",
    "ibbi.gov.in", "nclt.gov.in", "taxguru.in", "indiankanoon.org",
)

SCHEMA_FIELDS = [
    "record_id", "company_name", "nse_symbol", "isin", "delisting_type",
    "order_date", "effective_delisting_date", "calendar_year", "financial_year",
    "is_priority_year", "regulatory_regime", "exit_or_fair_value_inr",
    "source_url", "local_artifact_path", "confidence_status",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    """Shared id for one collection run. collect.py sets NSE_RUN_ID; standalone scripts get their own."""
    rid = os.environ.get("NSE_RUN_ID")
    if not rid:
        rid = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        os.environ["NSE_RUN_ID"] = rid
    return rid


def event(kind: str, script: str | None = None, **fields) -> None:
    """Append one machine-readable event to logs/events.jsonl."""
    rec = {"ts": now_iso(), "run_id": run_id(), "script": script or Path(sys.argv[0]).stem,
           "event": kind, **fields}
    if os.environ.get("NSE_AGENT_SESSION"):  # set by scripts/run_agent.sh
        rec["agent_session"] = os.environ["NSE_AGENT_SESSION"]
    append_jsonl(EVENTS, rec)


def log(msg: str, level: str = "INFO", script: str | None = None, kind: str = "log", **fields) -> None:
    """Human-readable line in logs/agent_run.log plus a structured event in logs/events.jsonl."""
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    name = script or Path(sys.argv[0]).stem
    with AGENT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now_iso()} [{level}] [{run_id()}] {name}: {msg}\n")
    event(kind, script=name, level=level, msg=msg, **fields)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def polite_sleep(lo: float = 2.0, hi: float = 5.0) -> None:
    time.sleep(random.uniform(lo, hi))


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, domains) -> bool:
    d = domain_of(url)
    return any(d == x or d.endswith("." + x) for x in domains)


def confidence_for_url(url: str) -> str:
    if domain_matches(url, PRIMARY_DOMAINS):
        return "VERIFIED_PRIMARY"
    if domain_matches(url, SECONDARY_DOMAINS):
        return "CORROBORATED_SECONDARY"
    return "NEEDS_REVIEW"


def raw_dir_for_tag(year_tag: str | None) -> Path:
    tag = (year_tag or "").strip().lower()
    if tag == "master":
        return RAW / "master_lists"
    if tag.isdigit() and int(tag) in PRIORITY_YEARS:
        return RAW / f"priority_{tag}"
    return RAW / "archive_2006_2026"


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def emit(summary: dict) -> None:
    """Print a compact JSON summary for the agent."""
    print(json.dumps(summary, ensure_ascii=False, default=str))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "log":
        log(" ".join(sys.argv[2:]), script="agent", kind="agent_note")
        emit({"logged": True})
    else:
        print('usage: python scripts/common.py log "message"')
        sys.exit(2)
