"""
Pre-flight triage: a cheap look at every delivered file before any parsing.

    probes = [probe(p) for p in paths]      # milliseconds per file: magic bytes + pypdfium2, no docling
    paths = order_by_cost(probes)           # the expensive files start first, so none ends the run alone

Per file: the real type (sniffed, not the extension), size, page count, how many pages have almost no text
layer (the pages OCR would have to read), a size class, a rough cost estimate, and whether it is an IPO offer
document (DRHP / RHP / prospectus): those mention an old delisting in the company history but are hundreds of
pages about a new issue, never a delisting record, so the indexer skips them as out of scope.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from config.settings import COMPUTE_BUDGET
from pipeline.type_router import detect_type

logger = logging.getLogger(__name__)

HEAD_PAGES = 3  # pages read for the offer-document check
# An offer document says what it is on its cover: "DRAFT RED HERRING PROSPECTUS", "RED HERRING PROSPECTUS", or a
# capitalised "PROSPECTUS" title on page 1. A passing "in the Prospectus dated ..." (Reg 30 disclosures) is not
# one, nor is the delisting exit offer's "Letter of Offer" / "Public Announcement".
OFFER_DOC_RE = re.compile(r"red\s+herring\s+prospectus", re.I)
COVER_TITLE_RE = re.compile(r"\bPROSPECTUS\b|INITIAL PUBLIC OFFER")


def is_offer_document(head_pages: Sequence[str]) -> bool:
    if not head_pages:
        return False
    return bool(OFFER_DOC_RE.search("\n".join(head_pages)[:8000]) or COVER_TITLE_RE.search(head_pages[0][:3000]))

# Rough cost model from the 2026-11-24 baseline (M4 Pro, 4 workers, TableFormer ACCURATE): seconds per page
# for the first docling pass and per OCR page, peak MB of a worker per page parsed in one conversion.
SEC_PER_PAGE = 1.6
SEC_PER_OCR_PAGE = 5.5  # RapidOCR on torch CPU, one thread (140-page scan: 125 OCR pages in 723 s)
MB_BASE, MB_PER_PAGE = 1500, 22


@dataclass
class Probe:
    path: str
    kind: str                 # sniffed type: pdf, html, xlsx, csv, json, zip, ... ('' when unknown)
    mb: float
    pages: Optional[int] = None
    low_text_pages: int = 0   # pages under the OCR threshold in the PDF's own text layer
    size_class: str = ""
    est_seconds: float = 0.0
    est_peak_mb: int = 0
    offer_document: bool = False
    error: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name


def size_class(kind: str, pages: Optional[int], mb: float) -> str:
    if kind == "pdf" and pages:
        return ("pdf_1-10p" if pages <= 10 else "pdf_11-50p" if pages <= 50
                else "pdf_51-150p" if pages <= 150 else "pdf_151p+")
    return f"{kind or 'unknown'}_{'big' if mb > 5 else 'small'}"


def probe(path: Path | str, min_chars: Optional[int] = None) -> Probe:
    """Never raises: an unreadable file gets `error` set and is left to the parser to fail properly."""
    p = Path(path)
    threshold = int(min_chars if min_chars is not None else COMPUTE_BUDGET.get("min_chars_per_page_before_ocr", 25))
    try:
        mb = p.stat().st_size / 1e6
    except OSError as exc:
        return Probe(str(p), "", 0.0, error=f"{type(exc).__name__}: {exc}")
    try:
        kind = detect_type(p) or ""
    except Exception as exc:  # sniffing failed: the router will report it
        kind = ""
        logger.debug(f"{p.name}: type sniff failed: {exc}")
    pr = Probe(str(p), kind, round(mb, 2))
    if kind == "pdf":
        _probe_pdf(pr, p, threshold)
    elif kind in ("html", "htm"):
        pr.offer_document = is_offer_document([_html_head(p)])
    pr.size_class = size_class(kind, pr.pages, mb)
    pages = pr.pages or 1
    pr.est_seconds = round(pages * SEC_PER_PAGE + pr.low_text_pages * SEC_PER_OCR_PAGE + mb * 0.5, 1)
    pr.est_peak_mb = int(MB_BASE + MB_PER_PAGE * pages) if kind == "pdf" else MB_BASE
    return pr


def _probe_pdf(pr: Probe, p: Path, threshold: int) -> None:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p))
    except Exception as exc:
        pr.error = f"{type(exc).__name__}: {exc}"
        return
    try:
        pr.pages = len(doc)
        head = []
        for i in range(pr.pages):
            page = doc[i]
            try:
                text = page.get_textpage().get_text_range()
            except Exception:
                text = ""
            finally:
                page.close()
            if len(text.strip()) < threshold:
                pr.low_text_pages += 1
            if i < HEAD_PAGES:
                head.append(text)
        pr.offer_document = is_offer_document(head)
    finally:
        doc.close()


def _html_head(p: Path) -> str:
    try:
        raw = p.read_bytes()[:200_000].decode("utf-8", "ignore")
    except OSError:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    return m.group(1) if m else ""


def order_by_cost(probes: Sequence[Probe]) -> List[str]:
    """Most expensive first (stable for ties): long files start while short ones fill the other workers."""
    return [pr.path for pr in sorted(probes, key=lambda pr: -pr.est_seconds)]


def write_csv(probes: Iterable[Probe], out: Path) -> None:
    rows = [asdict(pr) for pr in probes]
    if not rows:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
