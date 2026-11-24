"""
Document parser: pdf, docx, pptx, html -> ordered structural Blocks (via docling).

    result = Parser().parse(path)   # ParseResult(blocks, page_count, ocr_pages, warnings)

Headings, list items, tables, captions, footnotes and code are told apart by
docling's item *class* and label (SectionHeaderItem, ListItem, TableItem, ...);
tables keep their cell structure (Block.table) next to a markdown rendering.
Page headers/footers live in docling's furniture layer and are not returned.

OCR is on demand: PDFs are parsed first without OCR, pages that came back with
almost no text are counted, and only if there are some (and few enough to fit
the budget) is the document re-converted with OCR enabled.

Each Parser owns heavyweight docling converters, built lazily on first use, so
create one per worker process and never share it across processes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docling_core.types.doc import DocItemLabel, ListItem, SectionHeaderItem, TableItem, TextItem

from config.settings import COMPUTE_BUDGET, PARSER_DEVICE, PARSER_THREADS
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

        doc = self._converter(use_ocr=False).convert(path, raises_on_error=True).document
        blocks, warnings = self._extract_blocks(doc)
        page_count = doc.num_pages() or None
        ocr_pages = 0

        if path.suffix.lower() == ".pdf" and page_count and self.compute_budget.get("enable_ml_ocr"):
            low_pages = self._low_text_pages(blocks, page_count)
            if low_pages:
                budget = int(self.compute_budget.get("max_pages_for_ocr", 0))
                if len(low_pages) > budget:
                    warnings.append(
                        f"{len(low_pages)} page(s) have little extractable text but exceed the OCR budget "
                        f"of {budget}; left un-OCRed"
                    )
                else:
                    logger.info(f"{path.name}: OCR pass for {len(low_pages)} low-text page(s)")
                    ocr_doc = self._converter(use_ocr=True).convert(path, raises_on_error=True).document
                    ocr_blocks, ocr_warnings = self._extract_blocks(ocr_doc)
                    if _text_length(ocr_blocks) > _text_length(blocks):
                        blocks, ocr_pages = ocr_blocks, len(low_pages)
                        warnings.extend(ocr_warnings)
                    else:
                        warnings.append("OCR pass found no additional text; kept the original parse")

        return ParseResult(blocks=blocks, page_count=page_count, ocr_pages=ocr_pages, warnings=warnings)

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

    @staticmethod
    def _low_text_pages(blocks: List[Block], page_count: int) -> List[int]:
        threshold = int(COMPUTE_BUDGET.get("min_chars_per_page_before_ocr", 25))
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
