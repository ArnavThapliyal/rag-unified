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

import ocr

from common import (
    MANIFEST, NON_LISTING_RE, PARSED, PROCEDURAL_RE, RAW, ROOT, confidence_for_url, emit, event, isin_equity, isin_valid, log, read_jsonl,
)

warnings.filterwarnings("ignore")

ISIN_RE = re.compile(r"\bIN[E9][A-Z0-9]{8}[0-9]\b")  # candidates; confirmed with common.isin_valid
DATE_RE = re.compile(
    r"\b(\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:\d{1,2}\s+)?[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,\s*|\s+)\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4})\b"
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
    ("reason", "_reason"), ("remark", "_reason"), ("category", "_reason"), ("type", "_reason"), ("details", "_reason"),
]

TYPE_KEYWORDS = [
    ("IBC_NCLT", ("nclt", "insolvency", "resolution plan", "ibc", "liquidation")),
    ("AMALGAMATION_MERGER", ("amalgamat", "merger", "scheme of arrangement", "merged")),
    ("COMPULSORY", ("compulsor",)),
    ("VOLUNTARY_EXIT_OFFER", ("exit offer", "reverse book", "exit price", "discovered price")),
    ("VOLUNTARY_NO_EXIT", ("voluntar",)),
]


# Listing tables are never delisting lists. Tables about people/shareholdings are rejected unless
# they are keyed by a company symbol/ISIN (e.g. NSE's proposed-delisting list has a promoter column).
# Listing and market-activity tables (Factbook "most active days", turnover, market cap) are never delisting lists.
LISTING_HEADER_RE = re.compile(r"(?<!de)listing date|date of (?<!de)listing|listed on|traded value|turnover|"
                               r"traded quantity|market capitali|\brank\b|no\.? of trades", re.I)
PEOPLE_HEADER_RE = re.compile(r"director|designation|\bdin\b|shareholder|promoter|particulars|no\.? of shares|"
                              r"holding|\bpan\b|address", re.I)
COMPANY_RE = re.compile(r"\b(ltd|limited|corporation|corpn?|industries|company|co\.|inc|plc|bank|enterprises|"
                        r"holdings|mills|private|pvt|infra\w*|finance|financial|textiles|chemicals|steels?|"
                        r"pharma\w*|power|projects|exports|technologies|systems|services)\b", re.I)


LEGAL_TEXT_RE = re.compile(r"\bu/s\b|\bAct,?\s+\d{4}|\bSection\s+\d|\bRegulation\s+\d", re.I)


FRAGMENT_END_RE = re.compile(r"\s(is|are|was|were|has|have|had|the|of|and|to|for|by|in|on|with|from|that)$", re.I)


def name_ok(name: str, has_id: bool) -> bool:
    """Plausible company name. Without a symbol/ISIN the name must look like a company (filters
    sentences, people and OCR noise)."""
    if not 3 <= len(name) <= 120 or "\n" in name or "•" in name or name[0] in "(-*•" or len(name.split()) > 12:
        return False
    if not re.search(r"[A-Za-z]{3}", name):
        return False
    if LEGAL_TEXT_RE.search(name):  # "Financial Institutions u/s. 4A of Companies Act, 1956"
        return False
    if FRAGMENT_END_RE.search(name):  # "Company is", "shares of the": a cut-off sentence
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
    ts = pd.to_datetime(re.sub(r",(?=\d)", ", ", m.group(0)).replace("th ", " ").replace("st ", " ").replace("nd ", " ").replace("rd ", " "),
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


def find_header_row(df: pd.DataFrame, scan: int = 15, allow_people: bool = False) -> int | None:
    """Index of the header row of a delisting table (-1 = the DataFrame's own columns), else None.
    Needs a company-name column plus a symbol, ISIN or explicit delisting/order date column, and no
    listing/people/shareholding columns. People columns (promoter etc.) are allowed only in
    proposed-delisting lists (allow_people), which name the promoters of each listed company; in
    NSE's delisted.xlsx the promoter sheet is one row per person, not per delisting."""
    candidates = [(-1, list(df.columns))] + [(i, list(df.iloc[i].values)) for i in range(min(scan, len(df)))]
    for idx, cells in candidates:
        if any(LISTING_HEADER_RE.search(str(c)) for c in cells):
            continue
        m = set(map_headers(cells).values())
        if not (allow_people and m & {"nse_symbol", "isin"}) and any(PEOPLE_HEADER_RE.search(str(c)) for c in cells):
            continue
        if "company_name" in m and m & {"nse_symbol", "isin", "effective_delisting_date", "order_date"}:
            return idx
    return None


def rows_from_table(df: pd.DataFrame, doc_type: str, allow_people: bool = False) -> list[dict]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return []
    hdr = find_header_row(df, allow_people=allow_people)
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
        if isin_m and not isin_valid(isin_m.group(0)):
            isin_m = None
        if not name:
            continue
        if name.lower() in ("company name", "name of the company", "total", "nan", "nat", "none"):
            continue
        symbol = str(rec.get("nse_symbol", "")).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9&\-]{0,19}", symbol):  # cells of bullet text are not symbols
            symbol = ""
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
            "delisting_type": row_type if (row_type := classify(str(rec.get("_reason", "")) + " " + row_text)) != "OTHER"
            else doc_type,  # the row's own words first; the document-wide guess only if the row says nothing
            "extraction": "table",
        })
    return out


# Form labels and letterhead text that sit next to registration numbers in offer documents.
LABEL_RE = re.compile(r"SEBI Reg|E-?mail|@|\bCIN\b|Target Company|Registration No|Website|Tel\b|Fax\b", re.I)


# NSE's monthly "Processing Status of Voluntary Delisting Applications": one row per application; only
# rows whose status says the company is (or shall stand) delisted w.e.f. a date are delistings.
STATUS_DELISTED_RE = re.compile(r"delisted\s+(?:w\.?e\.?f\.?|with effect from)\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4}|"
                                r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", re.I)


def rows_from_status_table(df: pd.DataFrame) -> list[dict]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    for i in range(min(10, len(df))):
        cells = [str(c) for c in df.iloc[i].values]
        name_col = next((j for j, c in enumerate(cells) if re.search(r"name of (?:the )?company", c, re.I)), None)
        status_col = next((j for j, c in enumerate(cells) if re.search(r"\bstatus\b", c, re.I)), None)
        if name_col is None or status_col is None:
            continue
        nature_col = next((j for j, c in enumerate(cells) if re.search(r"nature", c, re.I)), None)
        out = []
        for vals in df.iloc[i + 1:].itertuples(index=False):
            status, name = str(vals[status_col]), re.sub(r"\s+", " ", str(vals[name_col])).strip()
            m = STATUS_DELISTED_RE.search(status)
            if not m or not name_ok(name, False):
                continue
            nature = str(vals[nature_col]) if nature_col is not None else ""
            out.append({"company_name": name, "nse_symbol": None, "isin": None, "order_date": None,
                        "effective_delisting_date": to_iso(m.group(1)), "exit_or_fair_value_inr": None,
                        "delisting_type": classify(nature + " " + status), "extraction": "status_table"})
        return out
    return []


def rows_from_text(text: str, doc_type: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = ISIN_RE.search(line.upper())
        if not m or not isin_valid(m.group(0)):
            continue
        before = line[: m.start()].strip(" |,-:\t")
        before = re.sub(r"^\s*\d+[.)]?\s*", "", before)  # strip serial number
        syms = [s for s in SYMBOL_RE.findall(line[m.end():]) + SYMBOL_RE.findall(before)
                if s not in ("LTD", "LIMITED", "INE", "THE", "AND", "OF") and not ISIN_RE.fullmatch(s)]
        dates = [d for d in (to_iso(x) for x in DATE_RE.findall(line)) if d]
        if not before or not name_ok(before, True) or LABEL_RE.search(before):
            continue  # an ISIN without a usable company name on the line is not a record
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
PROPOSED_RE = re.compile(r"propos|show.?cause|to.?be.?delisted", re.I)
# "In the matter of the Company: SecUR Credentials Limited (SECURCRED)", or without the symbol and with
# the name on the next line: "In the matter of the company\nM/s Petron Engineering Construction Limited".
MATTER_RE = re.compile(
    r"In the matter of(?: the [Cc]ompany)?\s*:?\s*(?:M/s\.?\s*)?([^\n(]{3,120}?)\s*"
    r"(?:\(\s*([A-Z][A-Z0-9&\-]{1,19})\s*\)|(?<=Limited)|(?<=Ltd)|(?<=Ltd\.)|\n)")
# A company name followed by its symbol anywhere in the order: "Cox & Kings Limited (COX&KINGS)"
NAME_SYMBOL_RE = re.compile(r"(?:Limited|Ltd\.?)\s*\(\s*([A-Z][A-Z0-9&\-]{2,19})\s*\)")
# Trading-member disciplinary orders (MCSGFC) share the order template but are not about listed companies.
MEMBER_ORDER_RE = re.compile(r"Trading Member|Settlement Guarantee Fund|MCSGFC")
ORDER_DATE_RE = re.compile(r"\bDate[d]?\s*:?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
DECISION_RE = re.compile(r"(?:be|is hereby|are hereby|shall be)\s+(compulsorily\s+)?(?:delisted|restrained and withdrawn)"
                         r"\s+from\s+the\s+Exchange[^.]{0,200}", re.I)
# Compulsory-delisting provisions: Reg 22(1) of the 2009 Regulations, Reg 32(1) of the 2021 Regulations.
COMPULSORY_REG_RE = re.compile(r"Regulation\s+(?:22|32)\s*\(\s*1\s*\)|compulsor", re.I)
EFFECTIVE_RE = re.compile(r"delisted[^.]{0,120}?(?:w\.e\.f\.?|with effect from)\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", re.I)


def rows_from_order(text: str) -> list[dict]:
    """Single-company Delisting Committee / exchange orders: one row from the order template."""
    head = text[:4000]
    if not re.search(r"Delisting Committee|In the matter of", head, re.I) or MEMBER_ORDER_RE.search(head):
        return []
    m = MATTER_RE.search(head)
    if not m:
        return []
    name = re.sub(r"\s+", " ", m.group(1)).strip(" :,-")
    symbol = m.group(2)
    if not symbol:
        s = NAME_SYMBOL_RE.search(text)
        symbol = s.group(1) if s else None
    if not name_ok(name, bool(symbol)):  # "In the matter of Non-Obtaining of SCORES Authentication"
        return []
    dates = ORDER_DATE_RE.findall(text[-3000:])
    found = list(DECISION_RE.finditer(text))
    # Orders warn "failing which ... shall be delisted" before the actual decision: prefer the
    # compulsory decision, else the last delisting sentence.
    decision = next((d for d in found if d.group(1)), found[-1] if found else None)
    eff = EFFECTIVE_RE.search(decision.group(0)) if decision else None
    isin = next((i for i in ISIN_RE.finditer(text.upper()) if isin_valid(i.group(0))), None)
    dtype = classify(decision.group(0)) if decision else classify(head)
    if decision and (decision.group(1) or COMPULSORY_REG_RE.search(decision.group(0))):
        dtype = "COMPULSORY"
    return [{
        "company_name": name,
        "nse_symbol": symbol,
        "isin": isin.group(0) if isin else None,
        "order_date": to_iso(dates[-1]) if dates else None,
        "effective_delisting_date": to_iso(eff.group(1)) if eff else None,
        "exit_or_fair_value_inr": None,
        "delisting_type": dtype,
        "extraction": "order" if decision else "order_no_decision",
    }]


# NSE listing-department letters to a company ("Ref: NSE/LIST/... <date>", "The Company Secretary\n<name>",
# "... delisted (withdrawn from admission to dealings) w.e.f <date>"), often scanned.
LETTER_REF_RE = re.compile(r"Ref\s*[:.]?\s*NSE/LIST/[^\n]*?([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4})")
LETTER_TO_RE = re.compile(r"The Company Secretary\W*\n+\W*([^\n]{3,100})")
LETTER_SUBJECT_RE = re.compile(r"Sub(?:ject)?\s*[:.\-]+\s*([^\n]{3,160})")
LETTER_EFFECTIVE_RE = re.compile(r"delist\w*[^.]{0,220}?(?:w\.?e\.?f\.?|with effect from)\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4})",
                                 re.I | re.S)


def rows_from_letter(text: str) -> list[dict]:
    text = text.replace("_", " ")  # OCR reads "March 24" as "March_24"
    ref, to = LETTER_REF_RE.search(text), LETTER_TO_RE.search(text)
    # The Exchange's own decision is the last "delist ... w.e.f." sentence; earlier ones quote BSE's
    # delisting or the original listing date.
    effs = [m for m in LETTER_EFFECTIVE_RE.finditer(text) if not re.search(r"\bBSE\b|listed on", m.group(0))]
    eff = effs[-1] if effs else None
    if not (ref and to and eff):
        return []
    name = re.sub(r"\s+", " ", re.sub(r"[^\w&.,()' -]", " ", to.group(1))).strip(" .,-")
    name = re.sub(r"^M/s\.?\s*", "", name)
    if not name_ok(name, False):
        return []
    subject = LETTER_SUBJECT_RE.search(text)
    dtype = classify(subject.group(1)) if subject else "OTHER"
    if dtype == "OTHER":
        dtype = "COMPULSORY" if re.search(r"withdrawn from admission to dealings|compulsor", text, re.I) else classify(text)
    return [{"company_name": name, "nse_symbol": None, "isin": None, "order_date": to_iso(ref.group(1)),
             "effective_delisting_date": to_iso(eff.group(1)), "exit_or_fair_value_inr": None,
             "delisting_type": dtype, "extraction": "letter"}]


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


def parse_file(path: Path, proposed: bool = False) -> tuple[list[dict], dict]:
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
            rows += rows_from_table(df, doc_type, proposed) or rows_from_status_table(df)
    elif ext == ".csv":
        df = pd.read_csv(path, header=None, dtype=str, on_bad_lines="skip", encoding_errors="ignore")
        stats["tables_seen"] = 1
        text = df.head(50).to_string()
        rows = rows_from_table(df, classify(df.to_string()[:20000]), proposed)
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
            rows += rows_from_table(df, doc_type, proposed) or rows_from_status_table(df)
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
        if stats["no_text"]:  # scanned PDF: OCR it (cached), then use the text extractors
            text = ocr.ocr_pdf(path)
            stats["ocr"] = bool(text.strip())
            stats["text_head"] = text[:2000]
        doc_type = classify(text)
        order_rows = rows_from_order(text) or rows_from_letter(text)
        if order_rows:  # order template wins: its tables are compliance lists, not delisted companies
            rows = order_rows
        else:
            for df in tables:
                rows += rows_from_table(df, doc_type, proposed) or rows_from_status_table(df)
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
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
            rows, stats = parse_file(p, proposed=bool(PROPOSED_RE.search(idx.get(rel, {}).get("url") or rel)))
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
        if stats.get("ocr"):  # OCR can misread names and dates: a human should confirm
            conf = "NEEDS_REVIEW"
        # Lists of companies *proposed* to be delisted are leads, not delistings.
        record_status = "PROPOSED" if PROPOSED_RE.search(src or rel) else "DELISTED"
        # debt securities (NCDs, bonds, CP) are suspended/redeemed, not delisted companies
        rows = [r for r in rows if not r.get("isin") or isin_equity(r["isin"])]
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
