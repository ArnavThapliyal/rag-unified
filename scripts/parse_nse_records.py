"""Extract candidate delisting records from cached artifacts in data/raw/.

Handles XLSX/XLS/CSV sheets, HTML (tables + text), and PDFs (tables + text).
Writes data/processed/parsed/<artifact>.jsonl and prints a JSON summary.

Examples:
  python scripts/parse_nse_records.py
  python scripts/parse_nse_records.py --force
  python scripts/parse_nse_records.py --file data/raw/priority_2017/ab12cd34.pdf
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from io import StringIO
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

from common import (
    MANIFEST, PARSED, PROCEDURAL_RE, RAW, ROOT, confidence_for_url, emit, event, log, read_jsonl,
)

warnings.filterwarnings("ignore")

ISIN_RE = re.compile(r"\bIN[A-Z0-9]{9}[0-9]\b")
DATE_RE = re.compile(
    r"\b(\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:\d{1,2}\s+)?[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4})\b"
)
SYMBOL_RE = re.compile(r"\b[A-Z][A-Z0-9&\-]{1,19}\b")

# header keyword -> schema field (checked in order; first match wins per column)
HEADER_MAP = [
    ("isin", "isin"),
    ("symbol", "nse_symbol"), ("ticker", "nse_symbol"), ("scrip code", "nse_symbol"),
    ("delist", "effective_delisting_date"),  # refined below: needs 'date'
    ("order", "order_date"), ("notice", "order_date"), ("approval", "order_date"),
    ("exit price", "exit_or_fair_value_inr"), ("fair value", "exit_or_fair_value_inr"),
    ("offer price", "exit_or_fair_value_inr"), ("price", "exit_or_fair_value_inr"),
    ("company", "company_name"), ("name of", "company_name"),
    ("security name", "company_name"), ("scrip name", "company_name"), ("name", "company_name"),
    ("date", "_plain_date"),  # only trusted as the delisting date in tables that also carry a symbol/ISIN
    ("reason", "_reason"), ("remark", "_reason"), ("category", "_reason"), ("type", "_reason"),
]

TYPE_KEYWORDS = [
    ("IBC_NCLT", ("nclt", "insolvency", "resolution plan", "ibc", "liquidation")),
    ("AMALGAMATION_MERGER", ("amalgamat", "merger", "scheme of arrangement", "merged")),
    ("COMPULSORY", ("compulsor",)),
    ("VOLUNTARY_EXIT_OFFER", ("exit offer", "reverse book", "exit price", "discovered price")),
    ("VOLUNTARY_NO_EXIT", ("voluntar",)),
]


# Tables about listings, people or shareholdings are never delisting lists.
REJECT_HEADER_RE = re.compile(r"(?<!de)listing date|date of (?<!de)listing|listed on|director|designation|\bdin\b|"
                              r"shareholder|promoter|particulars|no\.? of shares|holding|\bpan\b|address", re.I)
COMPANY_RE = re.compile(r"\b(ltd|limited|corporation|corpn?|industries|company|co\.|inc|plc|bank|enterprises|"
                        r"holdings|mills|private|pvt|infra\w*|finance|financial|textiles|chemicals|steels?|"
                        r"pharma\w*|power|projects|exports|technologies|systems|services)\b", re.I)


def name_ok(name: str, has_id: bool) -> bool:
    """Plausible company name. Without a symbol/ISIN the name must look like a company (filters
    sentences, people and OCR noise)."""
    if not 3 <= len(name) <= 120 or "\n" in name or "•" in name or name[0] in "(-*•" or len(name.split()) > 12:
        return False
    if not re.search(r"[A-Za-z]{3}", name):
        return False
    return has_id or bool(COMPANY_RE.search(name))


def classify(text: str) -> str:
    t = (text or "").lower()
    for label, kws in TYPE_KEYWORDS:
        if any(k in t for k in kws):
            return label
    return "OTHER"


def to_iso(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    ts = pd.to_datetime(m.group(0).replace("th ", " ").replace("st ", " ").replace("nd ", " ").replace("rd ", " "),
                        dayfirst=True, errors="coerce")
    if pd.isna(ts) or not (1990 <= ts.year <= 2030):
        return None
    return ts.strftime("%Y-%m-%d")


def map_headers(cols) -> dict[int, str]:
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for i, c in enumerate(cols):
        h = re.sub(r"\s+", " ", str(c).lower()).strip()
        for kw, field in HEADER_MAP:
            if kw not in h:
                continue
            if kw == "delist" and "date" not in h:
                continue
            if field in used and field != "_reason":
                continue
            mapping[i] = field
            used.add(field)
            break
    return mapping


def find_header_row(df: pd.DataFrame, scan: int = 15) -> int | None:
    """Index of the header row of a delisting table (-1 = the DataFrame's own columns), else None.
    Needs a company-name column plus a symbol, ISIN or explicit delisting/order date column, and no
    listing/people/shareholding columns."""
    candidates = [(-1, list(df.columns))] + [(i, list(df.iloc[i].values)) for i in range(min(scan, len(df)))]
    for idx, cells in candidates:
        if any(REJECT_HEADER_RE.search(str(c)) for c in cells):
            continue
        m = set(map_headers(cells).values())
        if "company_name" in m and m & {"nse_symbol", "isin", "effective_delisting_date", "order_date"}:
            return idx
    return None


def rows_from_table(df: pd.DataFrame, doc_type: str) -> list[dict]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return []
    hdr = find_header_row(df)
    if hdr is None:
        return []
    if hdr >= 0:
        df.columns = list(df.iloc[hdr].values)
        df = df.iloc[hdr + 1:]
    mapping = map_headers(df.columns)
    fields = set(mapping.values())
    if "_plain_date" in fields and "effective_delisting_date" not in fields and fields & {"nse_symbol", "isin"}:
        mapping = {i: ("effective_delisting_date" if f == "_plain_date" else f) for i, f in mapping.items()}
    out = []
    for _, r in df.iterrows():
        vals = list(r.values)
        rec: dict = {}
        for i, field in mapping.items():
            v = vals[i] if i < len(vals) else None
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
                continue
            rec[field] = v
        name = re.sub(r"\s+", " ", str(rec.get("company_name", ""))).strip()  # cells often wrap names
        isin_m = ISIN_RE.search(str(rec.get("isin", "")).upper())
        if not name and not isin_m:
            continue
        if name.lower() in ("company name", "name of the company", "total", "nan", "nat", "none"):
            continue
        symbol = str(rec.get("nse_symbol", "")).strip().upper()
        if name and not name_ok(name, bool(isin_m or symbol)):
            continue
        row_text = " ".join(str(v) for v in vals)
        price = rec.get("exit_or_fair_value_inr")
        try:
            price = float(re.sub(r"[^\d.]", "", str(price))) if price is not None else None
        except ValueError:
            price = None
        out.append({
            "company_name": name or None,
            "nse_symbol": symbol or None,
            "isin": isin_m.group(0) if isin_m else None,
            "order_date": to_iso(rec.get("order_date")),
            "effective_delisting_date": to_iso(rec.get("effective_delisting_date")),
            "exit_or_fair_value_inr": price,
            "delisting_type": classify(str(rec.get("_reason", "")) + " " + row_text)
            if rec.get("_reason") else doc_type,
            "extraction": "table",
        })
    return out


def rows_from_text(text: str, doc_type: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = ISIN_RE.search(line.upper())
        if not m:
            continue
        before = line[: m.start()].strip(" |,-:\t")
        before = re.sub(r"^\s*\d+[.)]?\s*", "", before)  # strip serial number
        syms = [s for s in SYMBOL_RE.findall(line[m.end():]) + SYMBOL_RE.findall(before)
                if s not in ("LTD", "LIMITED", "INE", "THE", "AND", "OF") and not ISIN_RE.fullmatch(s)]
        dates = [d for d in (to_iso(x) for x in DATE_RE.findall(line)) if d]
        if before and not name_ok(before, True):
            before = ""
        out.append({
            "company_name": before or None,
            "nse_symbol": syms[0] if syms else None,
            "isin": m.group(0),
            "order_date": None,
            "effective_delisting_date": dates[0] if dates else None,
            "exit_or_fair_value_inr": None,
            "delisting_type": doc_type,
            "extraction": "text",
        })
    return out


DELIST_RE = re.compile(r"delist", re.I)
# NSE/BSE pages about trading members, brokers, arbitration etc. list firms, not delisted companies.
NON_LISTING_RE = re.compile(r"member|broker|defaulter|expelled|arbitrat|complaint|debarred|"
                            r"authori[sz]ed.?person|sub.?broker|depositor", re.I)
PROPOSED_RE = re.compile(r"propos|show.?cause|to.?be.?delisted", re.I)
MATTER_RE = re.compile(
    r"In the matter of(?: the Company)?\s*:?\s*(?:M/s\.?\s*)?(.{3,120}?)\s*\(\s*([A-Z][A-Z0-9&\-]{1,19})\s*\)", re.S)
ORDER_DATE_RE = re.compile(r"\bDate[d]?\s*:?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
DECISION_RE = re.compile(r"(?:be|is hereby|are hereby|shall be)\s+(compulsorily\s+)?delisted\s+from\s+the\s+Exchange[^.]{0,200}", re.I)
EFFECTIVE_RE = re.compile(r"delisted[^.]{0,120}?(?:w\.e\.f\.?|with effect from)\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", re.I)


def rows_from_order(text: str) -> list[dict]:
    """Single-company Delisting Committee / exchange orders: one row from the order template."""
    head = text[:4000]
    if not re.search(r"Delisting Committee|In the matter of", head, re.I):
        return []
    m = MATTER_RE.search(head)
    if not m:
        return []
    name = re.sub(r"\s+", " ", m.group(1)).strip(" :,-")
    dates = ORDER_DATE_RE.findall(text[-3000:])
    found = list(DECISION_RE.finditer(text))
    # Orders warn "failing which ... shall be delisted" before the actual decision: prefer the
    # compulsory decision, else the last delisting sentence.
    decision = next((d for d in found if d.group(1)), found[-1] if found else None)
    eff = EFFECTIVE_RE.search(decision.group(0)) if decision else None
    isin = ISIN_RE.search(text.upper())
    dtype = classify(decision.group(0)) if decision else classify(head)
    if decision and decision.group(1):
        dtype = "COMPULSORY"
    return [{
        "company_name": name,
        "nse_symbol": m.group(2),
        "isin": isin.group(0) if isin else None,
        "order_date": to_iso(dates[-1]) if dates else None,
        "effective_delisting_date": to_iso(eff.group(1)) if eff else None,
        "exit_or_fair_value_inr": None,
        "delisting_type": dtype,
        "extraction": "order" if decision else "order_no_decision",
    }]


def is_relevant(path: Path, src: str | None, harvested_from: str | None, stats: dict) -> tuple[bool, str]:
    """Only documents about delisting may produce rows. Returns (relevant, reason)."""
    where = " ".join(x for x in (src, harvested_from, path.name) if x)
    if NON_LISTING_RE.search(src or ""):
        return False, "non_listing_page"
    if PROCEDURAL_RE.search(unquote((src or path.name).rsplit("/", 1)[-1])):
        return False, "procedural_document"
    if DELIST_RE.search(src or "") or DELIST_RE.search(harvested_from or ""):
        return True, "url"
    if path.suffix.lower() in (".html", ".htm"):  # page bodies include site-wide nav menus: trust the title only
        return (True, "title") if DELIST_RE.search(stats.get("title", "")) else (False, "html_title_unrelated")
    if DELIST_RE.search(stats.get("text_head", "")):
        return True, "content"
    return False, f"no_delisting_signal ({where[-60:]})"


def parse_file(path: Path) -> tuple[list[dict], dict]:
    ext = path.suffix.lower()
    stats = {"tables_seen": 0, "no_text": False, "title": "", "text_head": ""}
    rows: list[dict] = []
    text = ""
    if ext in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None, header=None)
        for df in sheets.values():
            stats["tables_seen"] += 1
            text += df.to_string()[:20000]
        doc_type = classify(text)
        for df in sheets.values():
            rows += rows_from_table(df, doc_type)
    elif ext == ".csv":
        df = pd.read_csv(path, header=None, dtype=str, on_bad_lines="skip", encoding_errors="ignore")
        stats["tables_seen"] = 1
        text = df.head(50).to_string()
        rows = rows_from_table(df, classify(df.to_string()[:20000]))
    elif ext in (".html", ".htm"):
        from bs4 import BeautifulSoup
        html = path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "lxml")
        stats["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = soup.get_text("\n", strip=True)
        doc_type = classify(text)
        try:
            tables = pd.read_html(StringIO(html), header=None)
        except (ValueError, ImportError):  # no tables, or a parser backend missing: fall back to text
            tables = []
        stats["tables_seen"] = len(tables)
        for df in tables:
            rows += rows_from_table(df, doc_type)
        if not rows:
            rows = rows_from_text(text, doc_type)
        if not rows:
            rows = rows_from_order(text)
    elif ext == ".pdf":
        import pdfplumber
        tables = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
                for t in page.extract_tables() or []:
                    if t and len(t) > 1:
                        tables.append(pd.DataFrame(t))
        stats["tables_seen"] = len(tables)
        stats["no_text"] = not text.strip()
        doc_type = classify(text)
        order_rows = rows_from_order(text)
        if order_rows:  # order template wins: its tables are compliance lists, not delisted companies
            rows = order_rows
        else:
            for df in tables:
                rows += rows_from_table(df, doc_type)
            if not rows:
                rows = rows_from_text(text, doc_type)
    elif ext == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
        rows = rows_from_text(text, classify(text))
    else:
        stats["skipped_ext"] = ext
    stats["text_head"] = text[:6000]
    return rows, stats


def manifest_index() -> dict[str, dict]:
    return {e["local_path"]: e for e in read_jsonl(MANIFEST) if e.get("local_path")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    PARSED.mkdir(parents=True, exist_ok=True)
    files = [a.file if a.file.is_absolute() else ROOT / a.file] if a.file else \
        sorted(p for p in RAW.rglob("*") if p.is_file() and not p.name.startswith("."))
    idx = manifest_index()
    relevance_path = PARSED / "_relevance.json"
    relevance = json.loads(relevance_path.read_text(encoding="utf-8")) if relevance_path.exists() else {}
    summary = {"files_seen": len(files), "parsed": 0, "skipped_up_to_date": 0, "rows": 0,
               "irrelevant_files": 0, "zero_row_files": [], "no_text": [], "errors": []}

    for p in files:
        rel = str(p.relative_to(ROOT))
        out = PARSED / f"{p.parent.name}__{p.stem}.jsonl"
        if out.exists() and not a.force and out.stat().st_mtime >= p.stat().st_mtime:
            summary["skipped_up_to_date"] += 1
            continue
        try:
            rows, stats = parse_file(p)
        except Exception as e:  # keep going; one bad artifact shouldn't stop the batch
            summary["errors"].append({"file": rel, "error": f"{type(e).__name__}: {e}"[:200]})
            log(f"parse error {rel}: {e}", level="ERROR", kind="parse_error", file=rel)
            continue
        m = idx.get(rel, {})
        src = m.get("url")
        relevant, reason = is_relevant(p, src, m.get("harvested_from"), stats)
        relevance[rel] = reason if relevant else f"irrelevant:{reason}"
        if not relevant:
            rows = []
            summary["irrelevant_files"] += 1
        conf = confidence_for_url(src) if src else "NEEDS_REVIEW"
        # Lists of companies *proposed* to be delisted are leads, not delistings.
        record_status = "PROPOSED" if PROPOSED_RE.search(src or rel) else "DELISTED"
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                r.update({"source_url": src, "local_artifact_path": rel, "year_tag": m.get("year_tag"),
                          "record_status": record_status,
                          "confidence_status": conf if (r.get("isin") or r.get("effective_delisting_date")
                                                        or r.get("order_date")) else "NEEDS_REVIEW"})
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        summary["parsed"] += 1
        summary["rows"] += len(rows)
        event("parse_file", file=rel, rows=len(rows), tables_seen=stats["tables_seen"],
              no_text=stats.get("no_text", False), source_url=src, relevant=relevant, relevance=reason)
        if not rows and relevant:
            summary["zero_row_files"].append({"file": rel, "tables_seen": stats["tables_seen"]})
        if stats.get("no_text"):
            summary["no_text"].append(rel)

    relevance_path.write_text(json.dumps(relevance, indent=1), encoding="utf-8")
    summary["zero_row_files"] = summary["zero_row_files"][:20]
    log(f"parsed={summary['parsed']} rows={summary['rows']} errors={len(summary['errors'])}",
        kind="parse", files=summary["parsed"], rows=summary["rows"], errors=len(summary["errors"]))
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
