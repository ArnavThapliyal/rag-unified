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

import pandas as pd

from common import (
    MANIFEST, PARSED, RAW, ROOT, confidence_for_url, emit, log, read_jsonl,
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
    ("date", "effective_delisting_date"),
    ("reason", "_reason"), ("remark", "_reason"), ("category", "_reason"), ("type", "_reason"),
]

TYPE_KEYWORDS = [
    ("IBC_NCLT", ("nclt", "insolvency", "resolution plan", "ibc", "liquidation")),
    ("AMALGAMATION_MERGER", ("amalgamat", "merger", "scheme of arrangement", "merged")),
    ("COMPULSORY", ("compulsor",)),
    ("VOLUNTARY_EXIT_OFFER", ("exit offer", "reverse book", "exit price", "discovered price")),
    ("VOLUNTARY_NO_EXIT", ("voluntar",)),
]


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
    """Return the index of the row that looks like a header (>=2 mapped fields incl. a name/isin)."""
    candidates = [(-1, list(df.columns))] + [(i, list(df.iloc[i].values)) for i in range(min(scan, len(df)))]
    for idx, cells in candidates:
        m = set(map_headers(cells).values())
        if len(m - {"_reason"}) >= 2 and ({"company_name", "isin"} & m):
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
    out = []
    for _, r in df.iterrows():
        vals = list(r.values)
        rec: dict = {}
        for i, field in mapping.items():
            v = vals[i] if i < len(vals) else None
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
                continue
            rec[field] = v
        name = str(rec.get("company_name", "")).strip()
        isin_m = ISIN_RE.search(str(rec.get("isin", "")).upper())
        if not name and not isin_m:
            continue
        if name.lower() in ("company name", "name of the company", "total"):
            continue
        row_text = " ".join(str(v) for v in vals)
        price = rec.get("exit_or_fair_value_inr")
        try:
            price = float(re.sub(r"[^\d.]", "", str(price))) if price is not None else None
        except ValueError:
            price = None
        out.append({
            "company_name": name or None,
            "nse_symbol": str(rec.get("nse_symbol", "")).strip().upper() or None,
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


def parse_file(path: Path) -> tuple[list[dict], dict]:
    ext = path.suffix.lower()
    stats = {"tables_seen": 0, "no_text": False}
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
        rows = rows_from_table(df, classify(df.to_string()[:20000]))
    elif ext in (".html", ".htm"):
        from bs4 import BeautifulSoup
        html = path.read_text(encoding="utf-8", errors="ignore")
        text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
        doc_type = classify(text)
        try:
            tables = pd.read_html(StringIO(html), header=None)
        except ValueError:
            tables = []
        stats["tables_seen"] = len(tables)
        for df in tables:
            rows += rows_from_table(df, doc_type)
        if not rows:
            rows = rows_from_text(text, doc_type)
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
        for df in tables:
            rows += rows_from_table(df, doc_type)
        if not rows:
            rows = rows_from_text(text, doc_type)
    elif ext == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
        rows = rows_from_text(text, classify(text))
    else:
        stats["skipped_ext"] = ext
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
    summary = {"files_seen": len(files), "parsed": 0, "skipped_up_to_date": 0,
               "rows": 0, "zero_row_files": [], "no_text": [], "errors": []}

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
            log(f"parse error {rel}: {e}", level="ERROR")
            continue
        m = idx.get(rel, {})
        src = m.get("url")
        conf = confidence_for_url(src) if src else "NEEDS_REVIEW"
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                r.update({"source_url": src, "local_artifact_path": rel, "year_tag": m.get("year_tag"),
                          "confidence_status": conf if (r.get("isin") or r.get("effective_delisting_date")
                                                        or r.get("order_date")) else "NEEDS_REVIEW"})
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        summary["parsed"] += 1
        summary["rows"] += len(rows)
        if not rows:
            summary["zero_row_files"].append({"file": rel, "tables_seen": stats["tables_seen"]})
        if stats.get("no_text"):
            summary["no_text"].append(rel)

    summary["zero_row_files"] = summary["zero_row_files"][:20]
    log(f"parsed={summary['parsed']} rows={summary['rows']} errors={len(summary['errors'])}")
    emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
