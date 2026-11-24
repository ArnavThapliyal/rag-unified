"""
Document parser: pdf, docx, pptx, html -> ordered structural Blocks (via docling).

    result = Parser().parse(path)   # ParseResult(blocks, page_count, ocr_pages, warnings)

Headings, list items, tables, captions, footnotes and code are told apart by
docling's item *class* and label (SectionHeaderItem, ListItem, TableItem, ...);
tables keep their cell structure (Block.table) next to a markdown rendering.
Page headers/footers live in docling's furniture layer and are not returned.

OCR is on demand: PDFs are parsed first without OCR, pages that came back with
almost no text are counted, and only if there are some (and few enough to fit
the budget) are those page ranges converted again with OCR enabled; the rest of
the document is not re-read. PDFs longer than PDF_WINDOW_PAGES are converted in
page windows so a worker's memory does not grow with the document.

Each Parser owns heavyweight docling converters, built lazily on first use, so
create one per worker process and never share it across processes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docling_core.types.doc import DocItemLabel, ListItem, SectionHeaderItem, TableItem, TextItem

from config.settings import COMPUTE_BUDGET, PARSER_DEVICE, PARSER_THREADS, PDF_WINDOW_PAGES
from pipeline.schema import (
    BLOCK_CAPTION, BLOCK_CODE, BLOCK_FOOTNOTE, BLOCK_HEADER, BLOCK_LIST_ITEM,
    BLOCK_PARAGRAPH, BLOCK_TABLE, Block, ParseResult, TableData,
)

logger = logging.getLogger(__name__)

_MAX_WARNING_DETAILS = 5
_OCR_BACKEND = "torch"  # rapidocr on torch: CPU-safe in workers and needs no extra runtime


class Parser:
    def __init__(self, compute_budget: Optional[Dict[str, Any]] = None):
        self.compute_budget = {**COMPUTE_BUDGET, **(compute_budget or {})}
        self._converters: Dict[bool, Any] = {}  # use_ocr -> DocumentConverter

    # ---- public --------------------------------------------------------------

    def parse(self, file_path: str | Path) -> ParseResult:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Input file not found: {path}")

        if path.suffix.lower() != ".pdf":
            doc = self._converter(use_ocr=False).convert(path, raises_on_error=True).document
            blocks, warnings = self._extract_blocks(doc)
            return ParseResult(blocks=blocks, page_count=doc.num_pages() or None, warnings=warnings)

        total = _pdf_page_count(path)
        blocks, warnings, page_count = self._convert_windows(path, total, use_ocr=False)
        ocr_pages = 0
        low_pages = self._low_text_pages(blocks, page_count) if page_count else []
        if low_pages and self.compute_budget.get("enable_ml_ocr"):
            budget = int(self.compute_budget.get("max_pages_for_ocr", 0))
            if len(low_pages) > budget:
                warnings.append(
                    f"{len(low_pages)} page(s) have little extractable text but exceed the OCR budget "
                    f"of {budget}; left un-OCRed"
                )
            else:
                logger.info(f"{path.name}: OCR pass for {len(low_pages)} low-text page(s)")
                blocks, ocr_pages = self._ocr_pages(path, blocks, low_pages, warnings)

        return ParseResult(blocks=blocks, page_count=page_count, ocr_pages=ocr_pages,
                           low_text_pages=len(low_pages), warnings=warnings)

    def _convert_windows(self, path: Path, total: Optional[int], use_ocr: bool,
                         ranges: Optional[List[Tuple[int, int]]] = None) -> Tuple[List[Block], List[str], Optional[int]]:
        """Convert `ranges` (default: the whole file, in PDF_WINDOW_PAGES windows when it is longer than that).
        docling keeps absolute page numbers with page_range, so the windows' blocks simply concatenate."""
        converter = self._converter(use_ocr=use_ocr)
        if ranges is None:
            if not total or total <= PDF_WINDOW_PAGES:
                doc = converter.convert(path, raises_on_error=True).document
                blocks, warnings = self._extract_blocks(doc)
                return blocks, warnings, doc.num_pages() or total
            ranges = [(a, min(a + PDF_WINDOW_PAGES - 1, total)) for a in range(1, total + 1, PDF_WINDOW_PAGES)]
        blocks: List[Block] = []
        warnings: List[str] = []
        for a, b in ranges:
            doc = converter.convert(path, raises_on_error=True, page_range=(a, b)).document
            part, part_warnings = self._extract_blocks(doc)
            blocks.extend(part)
            warnings.extend(part_warnings)
            del doc  # free the window's pages before the next one
        return blocks, warnings, total

    def _ocr_pages(self, path: Path, blocks: List[Block], low_pages: List[int],
                   warnings: List[str]) -> Tuple[List[Block], int]:
        """OCR only the runs of low-text pages; a run's OCR blocks replace its original blocks when they carry more
        text. Returns the blocks in page order and how many pages were taken from OCR."""
        runs = _page_runs(low_pages)
        ocr_blocks, ocr_warnings, _ = self._convert_windows(path, None, use_ocr=True, ranges=runs)
        taken: set = set()
        for a, b in runs:
            new = [x for x in ocr_blocks if x.page_number is not None and a <= x.page_number <= b]
            old = [x for x in blocks if x.page_number is not None and a <= x.page_number <= b]
            if _text_length(new) > _text_length(old):
                taken.update(range(a, b + 1))
        if not taken:
            warnings.append("OCR pass found no additional text; kept the original parse")
            return blocks, 0
        warnings.extend(ocr_warnings)
        kept = [x for x in blocks if x.page_number not in taken]
        added = [x for x in ocr_blocks if x.page_number in taken]
        merged = sorted(kept + added, key=lambda x: (x.page_number is None, x.page_number or 0))  # stable sort
        return merged, len(taken & set(low_pages))

    # ---- converters ------------------------------------------------------------

    def _converter(self, use_ocr: bool) -> Any:
        if use_ocr in self._converters:
            return self._converters[use_ocr]

        # Imported here: docling is slow to import and only parser workers need it.
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions, RapidOcrOptions, TableFormerMode, TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        mode = (
            TableFormerMode.FAST
            if str(self.compute_budget.get("table_structure_mode", "accurate")).lower() == "fast"
            else TableFormerMode.ACCURATE
        )
        options = PdfPipelineOptions(
            do_ocr=use_ocr,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(mode=mode, do_cell_matching=True),
            accelerator_options=AcceleratorOptions(device=PARSER_DEVICE, num_threads=PARSER_THREADS),
        )
        if use_ocr:
            options.ocr_options = RapidOcrOptions(backend=_OCR_BACKEND)

        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.HTML],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )
        self._converters[use_ocr] = converter
        return converter

    # ---- docling -> Blocks -----------------------------------------------------

    def _extract_blocks(self, doc: Any) -> Tuple[List[Block], List[str]]:
        blocks: List[Block] = []
        errors: List[str] = []
        skipped = 0

        for item, _ in doc.iterate_items():
            try:
                block = self._to_block(item, doc)
            except Exception as exc:
                skipped += 1
                if len(errors) < _MAX_WARNING_DETAILS:
                    errors.append(f"{type(item).__name__}: {exc}")
                continue
            if block is not None:
                blocks.append(block)

        warnings: List[str] = []
        if skipped:
            warnings.append(f"skipped {skipped} unparseable item(s): " + "; ".join(errors))
        return blocks, warnings

    @staticmethod
    def _to_block(item: Any, doc: Any) -> Optional[Block]:
        prov = getattr(item, "prov", None)
        page = prov[0].page_no if prov else None

        if isinstance(item, TableItem):
            return _table_block(item, doc, page)

        if not isinstance(item, TextItem):
            return None  # pictures, key-value regions, etc. carry no text of their own

        text = (item.text or "").strip()
        if not text:
            return None

        label = item.label
        if label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER):
            return None
        if isinstance(item, SectionHeaderItem):
            return Block(BLOCK_HEADER, text, page, level=max(1, min(6, int(item.level))))
        if label == DocItemLabel.TITLE:
            return Block(BLOCK_HEADER, text, page, level=1)
        if isinstance(item, ListItem) or label == DocItemLabel.LIST_ITEM:
            return Block(BLOCK_LIST_ITEM, text, page)
        if label == DocItemLabel.CAPTION:
            return Block(BLOCK_CAPTION, text, page)
        if label == DocItemLabel.FOOTNOTE:
            return Block(BLOCK_FOOTNOTE, text, page)
        if label == DocItemLabel.CODE:
            return Block(BLOCK_CODE, text, page)
        return Block(BLOCK_PARAGRAPH, text, page)

    def _low_text_pages(self, blocks: List[Block], page_count: int) -> List[int]:
        threshold = int(self.compute_budget.get("min_chars_per_page_before_ocr", 25))
        chars = dict.fromkeys(range(1, page_count + 1), 0)
        for block in blocks:
            if block.page_number in chars:
                chars[block.page_number] += len(block.content)
        return [page for page, n in chars.items() if n < threshold]


def _table_block(item: Any, doc: Any, page: Optional[int]) -> Optional[Block]:
    df = item.export_to_dataframe(doc=doc)
    if df is None or df.empty:
        return None

    columns = [str(c).strip() for c in df.columns]
    rows = [["" if v is None else str(v).strip() for v in row] for row in df.itertuples(index=False, name=None)]
    # With no detected header docling numbers the columns 0..n-1. Promote the first
    # row when it reads like a header; otherwise keep every row as data under
    # neutral names, so nothing is lost either way.
    if columns == [str(i) for i in range(len(columns))]:
        if len(rows) > 1 and _looks_like_header(rows[0]):
            columns, rows = rows[0], rows[1:]
        else:
            columns = [f"col_{i + 1}" for i in range(len(columns))]
    table = TableData(header=columns, rows=rows)

    meta: Dict[str, Any] = {}
    try:
        caption = item.caption_text(doc)
        if caption:
            meta["caption"] = caption
    except Exception:
        pass
    return Block(BLOCK_TABLE, table.to_markdown(), page, table=table, meta=meta)


_NUMERIC_LIKE = re.compile(r"^[\s()+\-–—,.%$₹€£\d]+$")


def _looks_like_header(row: List[str]) -> bool:
    """All cells filled in and none purely numeric ('FY25' qualifies; '1,200' or '2024' do not)."""
    return all(cell and not _NUMERIC_LIKE.match(cell) for cell in row)


def _text_length(blocks: List[Block]) -> int:
    return sum(len(b.content) for b in blocks)


def _pdf_page_count(path: Path) -> Optional[int]:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:  # docling reports a broken file properly; just don't window it
        return None


def _page_runs(pages: List[int], gap: int = 2) -> List[Tuple[int, int]]:
    """[3, 4, 5, 7, 20] -> [(3, 7), (20, 20)]: nearby pages share one conversion, capped at PDF_WINDOW_PAGES."""
    runs: List[Tuple[int, int]] = []
    for p in sorted(set(pages)):
        if runs and p - runs[-1][1] <= gap and p - runs[-1][0] < PDF_WINDOW_PAGES:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    return runs
