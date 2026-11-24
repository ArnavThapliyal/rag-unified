"""Deterministic qualitative filing extractor, HTML/PDF converter, and timeline renderer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any

from typing import Protocol

from pipeline.quant.store import (
    QuantStore,
    _is_valid_equity_isin,
    _normalize_nse_entity_name,
    normalize_market_record,
)
from pipeline.schema import EXTENDED_FIELDS as MARKET_DATA_COLUMNS


class MarketDocumentApiClient(Protocol):
    def extract_market_records(
        self, markdown: str, source_name: str, columns: Sequence[str]
    ) -> list[dict[str, Any]]: ...


_FILING_EXTENSIONS = frozenset({".pdf", ".md", ".txt", ".html", ".htm"})

_ISIN_RE = re.compile(r"\bIN[E9][A-Z0-9]{8}[0-9]\b")
_SYMBOL_LABEL_RE = re.compile(
    r"(?im)\b(?:NSE\s+Symbol|Trading\s+Symbol|Scrip\s+Symbol|Symbol)\s*[:\-]\s*([A-Z][A-Z0-9&\-]{1,19})\b"
)
_FILENAME_NSE_RE = re.compile(
    r"^([A-Z][A-Z0-9&\-]{1,19})_(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(?:_|$)"
)
_NOT_SYMBOLS = frozenset(
    {
        "ISIN",
        "NSE",
        "BSE",
        "SEBI",
        "LIMITED",
        "LTD",
        "DATE",
        "DATED",
        "INDIA",
        "CORP",
        "COMPANY",
        "SUBJECT",
        "SUB",
        "CIRCULAR",
        "NOTICE",
        "ORDER",
        "QUARTER",
        "PAGE",
    }
)
_BROADCAST_LINE_RE = re.compile(
    r"(?im)(?:^|[|;])\s*(?:#+\s*)?(?:Broadcast\s+(?:Date(?:/Time)?|Timestamp)|Filing\s+Date|Date\s+of\s+Filing|Dated|Date)\s*[:\-]\s*([^\n|;]+)"
)
_QUARTER_ENDED_RE = re.compile(
    r"(?i)(?:Quarter\s+(?:and\s+(?:Nine|Six)\s+Months\s+)?ended|Year\s+ended|Period\s+ended)\s*(?:on\s*)?[:\-|]?\s*([A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?[-/ ](?:[A-Za-z]{3,9}|\d{1,2})[-/ ]\d{2,4}|\d{4}-\d{2}-\d{2})"
)
_Q_FY_RE = re.compile(r"(?i)\bQ([1-4])\s*[-/]?\s*FY\s*'?(\d{2,4})\b")
_SURVEILLANCE_RE = re.compile(
    r"(?i)\b((?:ASM|GSM)\s+Stage\s+(?:I{1,3}V?|V?I?|[1-6])|Trading\s+Suspended)\b"
)
_SUBJECT_RE = re.compile(r"(?im)^(?:Subject|Sub)\s*[:\-]\s*(.+)$")
_GUIDANCE_SENTENCE_RE = re.compile(
    r"(?i)\b(?:guidance|outlook|target|expect\s+to\s+grow|capex\s+plan|order\s+book|margin\s+expansion|FY2\d)\b"
)

_METRIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pe_ratio", re.compile(r"(?im)\b(?:P/E(?:\s+Ratio)?|PE\s+Ratio)\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)")),
    ("roce_pct", re.compile(r"(?im)\bROCE\b(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
    ("roe_pct", re.compile(r"(?im)\bROE\b(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
    ("debt_to_equity", re.compile(r"(?im)\bDebt\s*(?:to|[-/])\s*Equity(?:\s+Ratio)?\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)")),
    ("ebitda_margin_pct", re.compile(r"(?im)\bEBITDA\s+Margin(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
    ("sales_growth_yoy", re.compile(r"(?im)\b(?:Sales|Revenue)\s+Growth\s*\(?YoY\)?(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
    ("pat_growth_yoy", re.compile(r"(?im)\bPAT\s+Growth\s*\(?YoY\)?(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(-?\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
    ("eps_ttm", re.compile(r"(?im)\b(?:EPS\s*\(?TTM\)?|Basic\s+EPS|Diluted\s+EPS|EPS)\s*[:\-=|]\s*(?:Rs\.?|INR|₹)?\s*(-?\d+(?:,\d+)*(?:\.\d+)?)")),
    ("promoter_holding_pct", re.compile(r"(?im)\bPromoter\s+Holding(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
    ("promoter_pledge_pct", re.compile(r"(?im)\bPromoter\s+Pledge(?:\s*(?:\(%?\)|%))?\s*[:\-=|]\s*(\d+(?:,\d+)*(?:\.\d+)?)\s*%?")),
)


class _HtmlToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self._in_row = False
        self._row_cells: list[str] = []
        self._in_cell = False
        self._cell_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(t[1])
            self._parts.append(f"\n{'#' * level} ")
        elif t in ("p", "div", "section", "br"):
            self._parts.append("\n")
        elif t == "li":
            self._parts.append("\n- ")
        elif t == "tr":
            self._in_row = True
            self._row_cells = []
        elif t in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style"):
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "li"):
            self._parts.append("\n")
        elif t in ("td", "th") and self._in_cell:
            cell_text = " ".join("".join(self._cell_buf).split())
            self._row_cells.append(cell_text)
            self._in_cell = False
            self._cell_buf = []
        elif t == "tr" and self._in_row:
            if any(self._row_cells):
                self._parts.append("\n" + " | ".join(self._row_cells) + "\n")
            self._in_row = False
            self._row_cells = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_buf.append(data)
        else:
            self._parts.append(data)

    def markdown(self) -> str:
        raw = "".join(self._parts)
        lines = [line.rstrip() for line in raw.splitlines()]
        collapsed: list[str] = []
        prev_blank = False
        for line in lines:
            is_blank = not line.strip()
            if is_blank and prev_blank:
                continue
            collapsed.append(line)
            prev_blank = is_blank
        return "\n".join(collapsed).strip() + "\n"


def _extract_pdf_markdown(path: Path) -> str:
    """Extract PDF text page-by-page using pdfplumber or pypdf if available."""
    pages: list[str] = []
    try:
        import pdfplumber  # type: ignore[import-not-found]
        with pdfplumber.open(str(path)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                pages.append(f"## Page {idx}\n\n{text}")
    except ImportError:
        from pypdf import PdfReader  # type: ignore[import-not-found]
        reader = PdfReader(str(path))
        for idx, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            pages.append(f"## Page {idx}\n\n{text}")
    return f"# {path.stem}\n\n" + "\n\n".join(pages).strip() + "\n"


def extract_document_markdown(path: str | Path, ocr: Any = None) -> str:
    """Read a PDF, HTML, Markdown, or text filing into clean Markdown."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Document not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_markdown(p)
    if suffix in (".html", ".htm"):
        parser = _HtmlToMarkdownParser()
        parser.feed(p.read_text(encoding="utf-8", errors="replace"))
        parser.close()
        return parser.markdown()
    if suffix in (".md", ".txt"):
        return p.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported qualitative document extension: {p.suffix}")


class DeterministicFilingExtractor:
    """Deterministic extractor implementing MarketDocumentApiClient and CompanyDocumentApiClient."""

    def extract_market_records(
        self,
        markdown_text: str,
        fields: Sequence[str] = MARKET_DATA_COLUMNS,
        source_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Extract only source-supported identifiers, metrics, and qualitative narratives."""
        text = (markdown_text or "").strip()
        if not text:
            return []

        extracted: dict[str, Any] = {}

        # 1. Valid Indian Equity ISIN
        for match in _ISIN_RE.finditer(text.upper()):
            candidate = match.group(0)
            if _is_valid_equity_isin(candidate):
                extracted["isin"] = candidate
                break

        # 2. NSE Symbol (label first, then NSE corporate filename convention)
        sym_match = _SYMBOL_LABEL_RE.search(text)
        if sym_match:
            cand_sym = sym_match.group(1).upper()
            if cand_sym not in _NOT_SYMBOLS:
                extracted["nse_symbol"] = cand_sym
        if "nse_symbol" not in extracted and source_name:
            fn_match = _FILENAME_NSE_RE.match(Path(source_name).stem.upper())
            if fn_match:
                cand_sym = fn_match.group(1)
                if cand_sym not in _NOT_SYMBOLS:
                    extracted["nse_symbol"] = cand_sym

        # 3. Broadcast Timestamp
        ts_val = _extract_broadcast_timestamp(text, source_name)
        if ts_val:
            extracted["broadcast_timestamp"] = ts_val

        # 4. Quarter Ending
        q_val = _extract_quarter_ending(text)
        if q_val:
            extracted["quarter_ending"] = q_val

        # 5. Surveillance stage
        surv_match = _SURVEILLANCE_RE.search(text)
        if surv_match:
            extracted["surveillance_stage"] = " ".join(surv_match.group(1).split()).upper()

        # 6. Headline & Category
        headline = _extract_headline(text)
        if headline:
            extracted["announcement_headline"] = headline

        category = _classify_announcement(text, headline)
        extracted["announcement_category"] = category

        # 7. Substantive body excerpt
        body_excerpt = _clean_excerpt(text, max_chars=4000)
        if body_excerpt:
            extracted["announcement_body_md"] = body_excerpt

        # 8. Concall commentary & guidance
        if _is_concall_text(text, category):
            extracted["concall_commentary_md"] = _clean_excerpt(text, max_chars=4000)
            guidance = _extract_guidance(text)
            if guidance:
                extracted["concall_guidance_md"] = guidance

        # 9. SAST / Insider activity
        sast = _extract_sast(text)
        if sast:
            extracted["sast_insider_activity"] = sast

        # 10. Explicit labeled financial metrics
        for col, pattern in _METRIC_PATTERNS:
            m = pattern.search(text)
            if m:
                try:
                    extracted[col] = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

        has_signal = any(
            extracted.get(k) is not None
            for k in (
                "isin",
                "nse_symbol",
                "quarter_ending",
                "surveillance_stage",
                "concall_commentary_md",
                "concall_guidance_md",
                "sast_insider_activity",
                "pe_ratio",
                "roce_pct",
                "roe_pct",
                "eps_ttm",
                "ebitda_margin_pct",
                "debt_to_equity",
                "promoter_holding_pct",
            )
        ) or (
            category != "CORPORATE_ANNOUNCEMENT" and bool(headline)
        ) or bool(headline and ts_val)

        if not has_signal:
            return []

        normalized = normalize_market_record(extracted)
        if fields and tuple(fields) != MARKET_DATA_COLUMNS:
            return [{k: normalized.get(k) for k in fields if k in normalized}]
        return [normalized]

    def extract_companies(self, markdown_text: str) -> list[dict[str, Any]]:
        """Extract basic structured company directory entries from Markdown."""
        records = self.extract_market_records(markdown_text)
        if not records:
            return []
        rec = records[0]
        name = _extract_company_name_from_text(markdown_text, rec.get("announcement_headline"))
        if not name:
            return []
        out: dict[str, Any] = {
            "name": name,
            "country": "India",
            "source": "document-extractor",
        }
        if rec.get("isin"):
            out["isin"] = rec["isin"]
        if rec.get("nse_symbol"):
            out["nse_symbol"] = rec["nse_symbol"]
        if rec.get("announcement_headline"):
            out["description"] = rec["announcement_headline"]
        return [out]


def ingest_qualitative_paths(
    paths: Sequence[str | Path],
    store: QuantStore,
    client: MarketDocumentApiClient | None = None,
    ocr: Any = None,
) -> dict[str, int]:
    """Extract and store qualitative filings & fundamentals from PDF/HTML/MD/TXT files."""
    files: list[Path] = []
    for p in (Path(x) for x in paths):
        if not p.exists():
            raise FileNotFoundError(f"Qualitative input not found: {p}")
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in _FILING_EXTENSIONS:
                    files.append(child)
        elif p.is_file():
            files.append(p)

    extractor = client if client is not None else DeterministicFilingExtractor()
    entity_index = _build_entity_name_index(store)

    files_processed = 0
    filings_inserted = 0
    fundamentals_upserted = 0
    entities_matched = 0

    for file_path in files:
        markdown_text = extract_document_markdown(file_path, ocr=ocr)
        files_processed += 1

        if isinstance(extractor, DeterministicFilingExtractor):
            records = extractor.extract_market_records(
                markdown_text, MARKET_DATA_COLUMNS, source_name=file_path.name
            )
        else:
            records = list(
                extractor.extract_market_records(markdown_text, MARKET_DATA_COLUMNS)
            )

        for raw in records:
            rec = normalize_market_record(raw)
            entity_id: int | None = None
            if not rec.get("nse_symbol") or not rec.get("isin"):
                matched = _resolve_entity_from_text(
                    markdown_text, rec.get("announcement_headline"), entity_index
                )
                if matched is not None:
                    entity_id = int(matched["id"])
                    if not rec.get("nse_symbol") and matched.get("nse_symbol"):
                        rec["nse_symbol"] = matched["nse_symbol"]
                    if not rec.get("isin") and matched.get("isin"):
                        rec["isin"] = matched["isin"]
            if entity_id is None and (rec.get("isin") or rec.get("nse_symbol")):
                entity_id = store._resolve_entity_id(
                    rec.get("isin"),
                    rec.get("nse_symbol"),
                    observed_date=(rec.get("broadcast_timestamp") or rec.get("trade_date")),
                )

            if entity_id is not None:
                entities_matched += 1

            if rec.get("nse_symbol") and rec.get("trade_date"):
                store.upsert_daily_market_row(rec)
            if rec.get("nse_symbol") and rec.get("quarter_ending"):
                bcast_ts = rec.get("broadcast_timestamp") or rec.get("trade_date")
                if bcast_ts:
                    fund_payload = dict(rec)
                    fund_payload["broadcast_timestamp"] = bcast_ts
                    store.upsert_quarterly_fundamental(fund_payload)
                    fundamentals_upserted += 1

            filing_payload = {
                **rec,
                "entity_id": entity_id,
                "local_artifact_path": str(file_path),
                "confidence_status": "VERIFIED_PRIMARY",
            }
            if store.upsert_qualitative_filing(filing_payload):
                filings_inserted += 1

    return {
        "files_processed": files_processed,
        "filings_inserted": filings_inserted,
        "fundamentals_upserted": fundamentals_upserted,
        "entities_matched": entities_matched,
    }


def render_company_status_markdown(profile: Mapping[str, Any]) -> str:
    """Render a company's point-in-time status, quant metrics, and qualitative timeline."""
    ent = profile["entity"]
    lines: list[str] = [
        f"# {ent['company_name']} ({ent.get('nse_symbol') or 'UNKNOWN'})",
        "",
        f"- **Status ({profile.get('as_of_date') or 'current'}):** `{profile['status_as_of']}`",
        f"- **ISIN:** `{ent.get('isin') or 'N/A'}`",
        f"- **Series:** `{ent.get('series') or 'N/A'}`",
        f"- **Listing Date:** `{ent.get('listing_date') or 'N/A'}`",
    ]
    d_cut = str(profile["as_of_date"])[:10] if profile.get("as_of_date") else None
    if ent.get("suspension_date") and (
        d_cut is None or ent["suspension_date"] <= d_cut
    ):
        lines.append(
            f"- **Suspension:** `{ent['suspension_date']}` ({ent.get('suspension_reason') or 'N/A'})"
        )
    if ent.get("surveillance_stage") and d_cut is None:
        lines.append(f"- **Surveillance Stage:** `{ent['surveillance_stage']}`")
    if d_cut is None:
        if ent.get("latest_delisting_date"):
            lines.append(
                f"- **Delisting:** `{ent['latest_delisting_date']}` (`{ent.get('latest_delisting_type') or 'N/A'}`)"
            )
    else:
        past_delistings = profile.get("delisting_events") or []
        if past_delistings:
            last_ev = past_delistings[-1]
            ev_dt = last_ev.get("effective_delisting_date") or last_ev.get("order_date")
            lines.append(
                f"- **Delisting:** `{ev_dt}` (`{last_ev.get('delisting_type') or 'N/A'}`)"
            )

    mkt = profile.get("latest_market_snapshot")
    if mkt:
        lines.extend(
            [
                "",
                f"## Latest Quantitative Snapshot ({mkt.get('trade_date')})",
                f"- **Close:** {mkt.get('close')} | **Market Cap (Cr):** {mkt.get('market_cap_cr')}",
                f"- **Z-Factor:** {mkt.get('z_factor_mcap')} | **Size Bracket:** `{mkt.get('size_bracket')}` | **Equivalent MCap (Cr):** {mkt.get('equivalent_mcap_cr')} | **In Target Bracket:** {bool(mkt.get('in_target_bracket'))}",
            ]
        )

    fund = profile.get("latest_fundamentals")
    if fund:
        lines.extend(
            [
                "",
                f"## Latest Fundamentals (Qtr End `{fund.get('quarter_ending')}`, Broadcast `{fund.get('broadcast_timestamp')}`)",
                f"- **EPS TTM:** {fund.get('eps_ttm')} | **ROCE %:** {fund.get('roce_pct')} | **ROE %:** {fund.get('roe_pct')} | **Debt/Equity:** {fund.get('debt_to_equity')}",
            ]
        )

    timeline = profile.get("timeline") or []
    lines.extend(["", "## Qualitative & Lifecycle Timeline"])
    if not timeline:
        lines.append("- _No dated timeline events recorded._")
    else:
        for ev in timeline:
            src = f" (`{ev['local_artifact_path'] or ev['source_url']}`)" if (ev.get("local_artifact_path") or ev.get("source_url")) else ""
            lines.append(f"- **{ev['date']}** [`{ev['event_type']}`] — {ev['summary']}{src}")

    return "\n".join(lines).rstrip() + "\n"


def _extract_broadcast_timestamp(text: str, source_name: str | None) -> str | None:
    m = _BROADCAST_LINE_RE.search(text)
    if m:
        parsed = _parse_timestamp_str(m.group(1))
        if parsed:
            return parsed
    if source_name:
        fn_match = _FILENAME_NSE_RE.match(Path(source_name).stem.upper())
        if fn_match:
            dd, mm, yyyy, hh, mi, ss = fn_match.groups()[1:]
            try:
                dt = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi), int(ss))
                return dt.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass
    return None


def _parse_timestamp_str(raw: str) -> str | None:
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip(), flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Try ISO datetime or date first
    iso_dt = re.search(r"\b(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2}(?::\d{2})?)\b", cleaned)
    if iso_dt:
        t_part = iso_dt.group(2) if len(iso_dt.group(2)) == 8 else f"{iso_dt.group(2)}:00"
        return f"{iso_dt.group(1)}T{t_part}"
    iso_d = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", cleaned)
    if iso_d:
        return iso_d.group(1)

    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(cleaned[:24].strip(", "), fmt)
            if "%H" in fmt:
                return dt.strftime("%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_quarter_ending(text: str) -> str | None:
    m = _QUARTER_ENDED_RE.search(text)
    if m:
        parsed = _parse_timestamp_str(m.group(1))
        if parsed:
            return parsed[:10]
    qm = _Q_FY_RE.search(text)
    if qm:
        q_num = int(qm.group(1))
        yr_raw = int(qm.group(2))
        fy_end_year = 2000 + yr_raw if yr_raw < 100 else yr_raw
        month_day = {1: "06-30", 2: "09-30", 3: "12-31", 4: "03-31"}[q_num]
        cal_year = fy_end_year - 1 if q_num in (1, 2, 3) else fy_end_year
        return f"{cal_year}-{month_day}"
    return None


def _extract_headline(text: str) -> str | None:
    subj = _SUBJECT_RE.search(text)
    if subj:
        return " ".join(subj.group(1).split())[:240]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title and not title.lower().startswith("page "):
                return " ".join(title.split())[:240]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("Date:", "Dated:", "ISIN:", "Symbol:")):
            return " ".join(stripped.split())[:240]
    return None


def _classify_announcement(text: str, headline: str | None) -> str:
    combined = f"{headline or ''}\n{text[:1500]}".lower()
    if any(k in combined for k in ("earnings call", "conference call", "concall", "transcript", "q&a session")):
        return "CONCALL_TRANSCRIPT"
    if any(k in combined for k in ("sast", "regulation 29", "insider trading", "pit regulations", "regulation 7(2)")):
        return "SAST_INSIDER"
    if any(k in combined for k in ("suspended from trading", "suspension of trading")):
        return "SUSPENSION"
    if any(k in combined for k in ("asm stage", "gsm stage", "additional surveillance", "graded surveillance")):
        return "SURVEILLANCE"
    if any(k in combined for k in ("delisting", "delisted", "exit offer", "compulsory delisting")):
        return "DELISTING"
    if any(k in combined for k in ("financial results", "unaudited financial", "audited financial", "quarter ended")):
        return "FINANCIAL_RESULTS"
    if "annual report" in combined:
        return "ANNUAL_REPORT"
    if "credit rating" in combined or "rating reaffirmed" in combined or "rating downgraded" in combined:
        return "CREDIT_RATING"
    if "board meeting" in combined or "outcome of board" in combined:
        return "BOARD_OUTCOME"
    return "CORPORATE_ANNOUNCEMENT"


def _is_concall_text(text: str, category: str) -> bool:
    if category == "CONCALL_TRANSCRIPT":
        return True
    low = text.lower()
    return "management commentary" in low or ("moderator:" in low and "question" in low)


def _extract_guidance(text: str) -> str | None:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    hits: list[str] = []
    for s in sentences:
        clean = " ".join(s.split())
        if len(clean) >= 20 and _GUIDANCE_SENTENCE_RE.search(clean):
            hits.append(clean)
        if len(" ".join(hits)) >= 1500:
            break
    return " ".join(hits)[:1500] if hits else None


def _extract_sast(text: str) -> str | None:
    low = text.lower()
    if not any(k in low for k in ("sast", "regulation 29", "regulation 7(2)", "insider", "acquisition", "disposal of shares")):
        return None
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    hits: list[str] = []
    for s in sentences:
        sl = s.lower()
        if any(k in sl for k in ("sast", "regulation 29", "regulation 7(2)", "insider", "acquir", "dispos", "pledge", "shares")):
            clean = " ".join(s.split())
            if len(clean) >= 15:
                hits.append(clean)
        if len(" ".join(hits)) >= 800:
            break
    return " ".join(hits)[:800] if hits else None


def _clean_excerpt(text: str, max_chars: int = 4000) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    return cleaned[:max_chars]


def _extract_company_name_from_text(text: str, headline: str | None) -> str | None:
    if headline:
        m = re.search(
            r"([A-Z][A-Za-z0-9&.,()' \-]{2,80}?\b(?:Limited|Ltd\.?|Corporation|Industries|Enterprises))",
            headline,
        )
        if m:
            return " ".join(m.group(1).split())
    m = re.search(
        r"([A-Z][A-Za-z0-9&.,()' \-]{2,80}?\b(?:Limited|Ltd\.?|Corporation|Industries|Enterprises))",
        text[:1000],
    )
    return " ".join(m.group(1).split()) if m else None


def _build_entity_name_index(store: QuantStore) -> list[tuple[str, dict[str, Any]]]:
    entities = store.list_nse_entities()
    return [
        (ent["normalized_name"], ent)
        for ent in entities
        if ent.get("normalized_name") and len(ent["normalized_name"]) >= 4
    ]


def _resolve_entity_from_text(
    text: str,
    headline: str | None,
    entity_index: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    cand_name = _extract_company_name_from_text(text, headline)
    if cand_name:
        norm_cand = _normalize_nse_entity_name(cand_name)
        for ent_norm, ent in entity_index:
            if ent_norm == norm_cand:
                return ent
    upper_slice = f"{headline or ''}\n{text[:800]}".upper()
    norm_slice = re.sub(r"\s+", " ", re.sub(r"[.,&()'\"\-/]", " ", upper_slice))
    for ent_norm, ent in sorted(entity_index, key=lambda x: len(x[0]), reverse=True):
        if len(ent_norm) >= 8 and f" {ent_norm} " in f" {norm_slice} ":
            return ent
    return None
