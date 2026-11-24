"""
The Processor's data contract, defined once.

  SourceDocument  input to the pipeline: one purchased file plus whatever
                  vendor metadata arrived with it (all of it optional).
  Chunk           in-memory unit produced by the chunker.
  ChunkRecord     the LanceDB row the LLM side reads. Changing it means
                  bumping config.settings.SCHEMA_VERSION.

Vendor metadata is inconsistent by design, so everything not needed to
identify a row is Optional/nullable, and unknown vendor fields are kept in
`metadata_json` rather than dropped. The downstream-LLM fields (isin, ohlc,
fundamentals, shareholding, announcements, ...) are typed nullable columns listed in
EXTENDED_FIELDS; a value that cannot be coerced to its type becomes null. `allowed_roles` is a top-level nullable
column (not buried in metadata_json) because the OPA policy in
tests/rag_authz.rego reads `document.metadata.allowed_roles`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from lancedb.pydantic import LanceModel, Vector

from config.settings import DISTANCE_METRIC, EMBEDDING_DIM

# LanceDB applies the distance metric at index-build and query time, not in the
# table schema. Every create_index()/search() call must use these kwargs so the
# metric (cosine) is defined in exactly one place next to the vector column.
VECTOR_COLUMN = "embedding"
VECTOR_INDEX_KWARGS: Dict[str, Any] = {"metric": DISTANCE_METRIC, "vector_column_name": VECTOR_COLUMN}

# source_type: the detected file format of the original document.
SOURCE_TYPES = frozenset({"pdf", "docx", "pptx", "html", "json", "csv", "xlsx", "xls", "unknown"})

# content_kind: what the chunk holds. Tables are isolated so they can be
# routed to a structured/relational store later without re-parsing.
CONTENT_KIND_PROSE = "prose"
CONTENT_KIND_TABLE = "table"
CONTENT_KINDS = frozenset({CONTENT_KIND_PROSE, CONTENT_KIND_TABLE})

_DOC_ID_HEX_CHARS = 32  # 128 bits of sha256
_CHUNK_ID_HEX_CHARS = 32


class ChunkRecord(LanceModel):
    """One row in the LanceDB table. Order here is the column order."""

    # --- identity (required) ---
    doc_id: str
    chunk_id: str
    chunk_index: int

    # --- content (required) ---
    text: str
    embedding: Vector(EMBEDDING_DIM)  # type: ignore[valid-type]  # fixed-size float32
    content_kind: str  # CONTENT_KINDS
    source_type: str  # SOURCE_TYPES
    source_filename: str

    # --- provenance (required, set by the pipeline) ---
    content_hash: str  # sha256 of `text`; informational, NOT used for dedup
    ingested_at: str  # ISO-8601 UTC
    embedding_model: str
    pipeline_version: str
    ingest_run_id: str

    # --- optional, vendor-supplied or derived; null when unknown ---
    symbol: Optional[str] = None
    source_url: Optional[str] = None
    doc_type: Optional[str] = None
    vendor: Optional[str] = None
    published_at: Optional[str] = None  # ISO-8601 date or datetime as supplied
    section_path: Optional[str] = None  # "H1 > H2 > H3"
    page_range: Optional[str] = None  # "5" or "5-7"; null for page-less formats
    allowed_roles: Optional[List[str]] = None  # null = no restriction declared

    # --- fields the downstream LLM expects; all nullable, null when the vendor didn't supply them.
    # Keep in sync with EXTENDED_FIELDS below (tests/test_schema.py enforces it).
    # Dates/timestamps are ISO-8601 strings as supplied, like published_at.
    # instrument / classification
    isin: Optional[str] = None
    nse_symbol: Optional[str] = None
    series: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    market_cap_cr: Optional[float] = None
    free_float_pct: Optional[float] = None
    is_fno: Optional[bool] = None
    # price / volume
    trade_date: Optional[str] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    total_traded_qty: Optional[int] = None
    turnover_cr: Optional[float] = None
    deliverable_qty: Optional[int] = None
    delivery_pct: Optional[float] = None
    no_of_trades: Optional[int] = None
    bulk_block_deal_flag: Optional[bool] = None
    futures_oi: Optional[int] = None
    oi_change_pct: Optional[float] = None
    # fundamentals
    quarter_ending: Optional[str] = None
    sales_growth_yoy: Optional[float] = None
    sales_growth_qoq: Optional[float] = None
    ebitda_margin_pct: Optional[float] = None
    pat_growth_yoy: Optional[float] = None
    eps_ttm: Optional[float] = None
    roce_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None
    cash_flow_from_ops: Optional[float] = None
    # valuation
    pe_ratio: Optional[float] = None
    sector_median_pe: Optional[float] = None
    ev_ebitda: Optional[float] = None
    peg_ratio: Optional[float] = None
    # shareholding
    promoter_holding_pct: Optional[float] = None
    promoter_pledge_pct: Optional[float] = None
    fii_holding_chg_qoq: Optional[float] = None
    dii_holding_chg_qoq: Optional[float] = None
    retail_holding_chg_qoq: Optional[float] = None
    # surveillance / announcements / concalls / insider activity
    surveillance_stage: Optional[str] = None
    broadcast_timestamp: Optional[str] = None
    announcement_category: Optional[str] = None
    announcement_headline: Optional[str] = None
    announcement_body_md: Optional[str] = None
    concall_commentary_md: Optional[str] = None
    concall_guidance_md: Optional[str] = None
    sast_insider_activity: Optional[str] = None
    bulk_block_deal_participants: Optional[str] = None

    # --- everything else the vendor sent, JSON-encoded ---
    metadata_json: str = "{}"


# Column name -> python type, for the downstream-LLM fields above. Single place
# that says how a raw vendor value is coerced before it reaches the typed column.
EXTENDED_FIELDS: Dict[str, type] = {
    **{name: str for name in (
        "isin", "nse_symbol", "series", "sector", "industry", "trade_date", "quarter_ending",
        "surveillance_stage", "broadcast_timestamp", "announcement_category", "announcement_headline",
        "announcement_body_md", "concall_commentary_md", "concall_guidance_md", "sast_insider_activity",
        "bulk_block_deal_participants",
    )},
    **{name: float for name in (
        "market_cap_cr", "free_float_pct", "open", "high", "low", "close", "turnover_cr", "delivery_pct",
        "oi_change_pct", "sales_growth_yoy", "sales_growth_qoq", "ebitda_margin_pct", "pat_growth_yoy",
        "eps_ttm", "roce_pct", "roe_pct", "debt_to_equity", "interest_coverage", "cash_flow_from_ops",
        "pe_ratio", "sector_median_pe", "ev_ebitda", "peg_ratio", "promoter_holding_pct",
        "promoter_pledge_pct", "fii_holding_chg_qoq", "dii_holding_chg_qoq", "retail_holding_chg_qoq",
    )},
    **{name: int for name in ("total_traded_qty", "deliverable_qty", "no_of_trades", "futures_oi")},
    **{name: bool for name in ("is_fno", "bulk_block_deal_flag")},
}

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


def _coerce(value: Any, kind: type) -> Any:
    """Coerce one raw vendor value to `kind`; raises ValueError/TypeError when it can't."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if kind is str:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value).strip() or None
    if kind is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if isinstance(value, bool):
        raise ValueError(f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "").rstrip("%").strip()
        if text.startswith("(") and text.endswith(")"):  # accounting negative: (12.5)
            text = "-" + text[1:-1]
        number = float(text)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"not a finite number: {value!r}")
    if kind is int:
        if not number.is_integer():
            raise ValueError(f"not a whole number: {value!r}")
        return int(number)
    return number


def coerce_extended(raw: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """
    Keep only EXTENDED_FIELDS keys and coerce them to their column types.
    A value that cannot be coerced becomes None (never an exception) and its
    key is returned in the rejected list so callers can log it.
    """
    clean: Dict[str, Any] = {}
    rejected: List[str] = []
    for name, kind in EXTENDED_FIELDS.items():
        if name not in raw:
            continue
        try:
            clean[name] = _coerce(raw[name], kind)
        except (ValueError, TypeError):
            clean[name] = None
            rejected.append(name)
    return clean, rejected


@dataclass
class SourceDocument:
    """A purchased file plus its optional vendor metadata."""

    path: Path
    doc_id: str
    source_type: str
    source_filename: str
    symbol: Optional[str] = None
    source_url: Optional[str] = None
    doc_type: Optional[str] = None
    vendor: Optional[str] = None
    published_at: Optional[str] = None
    allowed_roles: Optional[List[str]] = None
    fields: Dict[str, Any] = field(default_factory=dict)  # EXTENDED_FIELDS values, applied to every chunk
    extra: Dict[str, Any] = field(default_factory=dict)  # unmapped vendor fields -> metadata_json


@dataclass
class Chunk:
    """Chunker output. Positional provenance is structured, never encoded in text."""

    index: int
    text: str
    content_kind: str = CONTENT_KIND_PROSE
    section_path: Optional[str] = None
    page_range: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)  # e.g. sheet name, row range
    fields: Dict[str, Any] = field(default_factory=dict)  # EXTENDED_FIELDS for this chunk; overrides the document's
    symbol: Optional[str] = None  # set when one file holds rows for many instruments; overrides the document's


# ---- parser output (the IR consumed by the cleaner and chunker) --------------

BLOCK_HEADER = "header"
BLOCK_PARAGRAPH = "paragraph"
BLOCK_LIST_ITEM = "list_item"
BLOCK_TABLE = "table"
BLOCK_CAPTION = "caption"
BLOCK_FOOTNOTE = "footnote"
BLOCK_CODE = "code"
BLOCK_TYPES = frozenset({
    BLOCK_HEADER, BLOCK_PARAGRAPH, BLOCK_LIST_ITEM, BLOCK_TABLE,
    BLOCK_CAPTION, BLOCK_FOOTNOTE, BLOCK_CODE,
})


def _md_cell(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


@dataclass
class TableData:
    """A table kept structured so it can be re-chunked by row group or routed to a relational store."""

    header: List[str]
    rows: List[List[str]]

    def to_markdown(self, row_start: int = 0, row_end: Optional[int] = None) -> str:
        """Markdown for rows[row_start:row_end], always including the header row."""
        cols = max(1, len(self.header))
        lines = [
            "| " + " | ".join(_md_cell(h) for h in self.header) + " |",
            "| " + " | ".join("---" for _ in range(cols)) + " |",
        ]
        for row in self.rows[row_start:row_end]:
            cells = list(row) + [""] * (cols - len(row))
            lines.append("| " + " | ".join(_md_cell(c) for c in cells[:cols]) + " |")
        return "\n".join(lines)


@dataclass
class Block:
    """One structural element from the parser, in reading order."""

    type: str  # BLOCK_TYPES
    content: str  # text, or the markdown rendering for tables
    page_number: Optional[int] = None  # None for page-less formats (html, docx)
    level: int = 0  # heading depth 1-6 for BLOCK_HEADER, else 0
    table: Optional[TableData] = None  # set for BLOCK_TABLE
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    blocks: List[Block]
    page_count: Optional[int] = None
    ocr_pages: int = 0
    low_text_pages: int = 0  # pages under the OCR threshold before OCR (ocr_pages of them were recovered)
    warnings: List[str] = field(default_factory=list)


def compute_doc_id(path: Path) -> str:
    """Content-addressed id: identical bytes always yield the same doc_id."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:_DOC_ID_HEX_CHARS]


def make_chunk_id(doc_id: str, chunk_index: int, text: str) -> str:
    """
    Deterministic id from doc_id + chunk_index + chunk text.

    Re-running unchanged input reproduces the same ids (idempotent upserts).
    Including the index keeps repeated text inside one document (disclaimers,
    repeated table headers) from colliding; including the text means a changed
    chunk gets a new id instead of silently overwriting its predecessor.
    The \\0 separators make the three parts unambiguous when concatenated.
    """
    payload = f"{doc_id}\0{chunk_index}\0{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:_CHUNK_ID_HEX_CHARS]


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_allowed_roles(value: Any) -> Optional[List[str]]:
    """Accept a list or a comma-separated string from a vendor; return None when absent."""
    if value is None:
        return None
    if isinstance(value, str):
        roles = [r.strip() for r in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        roles = [str(r).strip() for r in value]
    else:
        return None
    roles = [r for r in roles if r]
    return roles or None


def build_record(
    doc: SourceDocument,
    chunk: Chunk,
    embedding: Sequence[float],
    *,
    ingested_at: str,
    ingest_run_id: str,
    embedding_model: str,
    pipeline_version: str,
) -> Dict[str, Any]:
    """Assemble and validate one row. Raises ValueError on a contract violation."""
    if not doc.doc_id:
        raise ValueError("doc_id is required")
    if not chunk.text or not chunk.text.strip():
        raise ValueError(f"chunk {chunk.index} of {doc.doc_id} has empty text")
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"embedding has {len(embedding)} dims, expected {EMBEDDING_DIM}")
    if chunk.content_kind not in CONTENT_KINDS:
        raise ValueError(f"unknown content_kind {chunk.content_kind!r}")

    source_type = doc.source_type if doc.source_type in SOURCE_TYPES else "unknown"
    extended, _ = coerce_extended({**doc.fields, **chunk.fields})

    row = ChunkRecord(
        doc_id=doc.doc_id,
        chunk_id=make_chunk_id(doc.doc_id, chunk.index, chunk.text),
        chunk_index=chunk.index,
        text=chunk.text,
        embedding=list(map(float, embedding)),
        content_kind=chunk.content_kind,
        source_type=source_type,
        source_filename=doc.source_filename,
        content_hash=hash_text(chunk.text),
        ingested_at=ingested_at,
        embedding_model=embedding_model,
        pipeline_version=pipeline_version,
        ingest_run_id=ingest_run_id,
        symbol=chunk.symbol or doc.symbol,
        source_url=doc.source_url,
        doc_type=doc.doc_type,
        vendor=doc.vendor,
        published_at=doc.published_at,
        section_path=chunk.section_path,
        page_range=chunk.page_range,
        allowed_roles=doc.allowed_roles,
        metadata_json=json.dumps({**doc.extra, **chunk.metadata}, default=str, sort_keys=True),
        **extended,
    )
    return row.model_dump()
