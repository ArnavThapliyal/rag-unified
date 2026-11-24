"""
Tabular sources (csv, xlsx, xls, json/jsonl) -> table chunks.

Tables are kept isolated: every chunk is content_kind="table", holds a whole
number of rows, and repeats the header row, so each chunk is a self-contained
markdown table. The row range, sheet name and column names travel in
chunk.metadata, which makes it straightforward to route these tables to a
relational store later without re-parsing.

Record mode: a table whose header names several of the downstream-LLM columns
(trade_date, close, roe_pct, ...) is emitted one chunk per row instead, with
those values coerced into Chunk.fields so the typed LanceDB columns are filled.

Files are streamed in row groups, so memory stays bounded for large inputs.
`table_chunks` is shared with the document path, which uses it for tables that
docling extracts out of PDFs/Office files.
"""

from __future__ import annotations

import csv
import datetime as dt
import itertools
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from config.settings import (
    CHUNK_SIZE_TOKENS, TABULAR_MAX_ROWS_PER_FILE, TABULAR_RECORD_MIN_MATCHES, TABULAR_RECORD_MODE,
    TABULAR_ROWS_PER_CHUNK,
)
from pipeline.schema import CONTENT_KIND_TABLE, EXTENDED_FIELDS, Chunk, TableData, coerce_extended

logger = logging.getLogger(__name__)

# Numbers tokenise at roughly 3 characters/token, so this is the default size
# budget when no tokenizer-based size function is supplied (Step 4 supplies one).
_CHARS_PER_TOKEN = 3
_SYMBOL_COLUMNS = ("symbol", "ticker", "scrip", "scrip_code")  # same spellings metadata.py accepts
_MAX_CSV_FIELD_BYTES = 16 * 1024 * 1024

csv.field_size_limit(_MAX_CSV_FIELD_BYTES)

SizeFn = Callable[[str], int]


# ---- shared chunking --------------------------------------------------------

def table_chunks(
    table: TableData,
    *,
    start_index: int = 0,
    rows_per_chunk: int = TABULAR_ROWS_PER_CHUNK,
    max_size: int = CHUNK_SIZE_TOKENS * _CHARS_PER_TOKEN,
    size_fn: SizeFn = len,
    section_path: Optional[str] = None,
    page_range: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[Chunk]:
    """Split an in-memory table into row-group chunks, each with the header repeated."""
    return list(_group_rows(
        table.header, iter(table.rows), start_index=start_index, rows_per_chunk=rows_per_chunk,
        max_size=max_size, size_fn=size_fn, section_path=section_path, page_range=page_range,
        metadata=metadata or {},
    ))


def _group_rows(
    header: List[str],
    rows: Iterator[Sequence[str]],
    *,
    start_index: int,
    rows_per_chunk: int,
    max_size: int,
    size_fn: SizeFn,
    section_path: Optional[str],
    page_range: Optional[str],
    metadata: Dict[str, Any],
) -> Iterator[Chunk]:
    index = start_index
    row_offset = 0  # data rows consumed so far (0-based)
    batch: List[List[str]] = []
    batch_size = 0
    header_size = size_fn(TableData(header, []).to_markdown())

    def flush() -> Chunk:
        nonlocal index, batch, batch_size, row_offset
        table = TableData(header, batch)
        chunk = Chunk(
            index=index,
            text=table.to_markdown(),
            content_kind=CONTENT_KIND_TABLE,
            section_path=section_path,
            page_range=page_range,
            metadata={**metadata, "row_start": row_offset + 1, "row_end": row_offset + len(batch),
                      "columns": list(header)},
        )
        index += 1
        row_offset += len(batch)
        batch, batch_size = [], 0
        return chunk

    for row in rows:
        row = list(row)
        row_size = size_fn(" | ".join(row)) + 4
        if batch and (len(batch) >= rows_per_chunk or header_size + batch_size + row_size > max_size):
            yield flush()
        batch.append(row)
        batch_size += row_size
    if batch:
        yield flush()


# ---- record mode ----------------------------------------------------------------

def _record_columns(header: Sequence[str]) -> Dict[int, str]:
    """column position -> downstream-LLM field, for header names that are exactly such a field."""
    columns: Dict[int, str] = {}
    for position, name in enumerate(header):
        field = name.strip().lower()
        if field in EXTENDED_FIELDS and field not in columns.values():
            columns[position] = field
    return columns


def _record_rows(
    header: List[str],
    rows: Iterator[Sequence[str]],
    columns: Dict[int, str],
    *,
    start_index: int,
    section_path: Optional[str],
    metadata: Dict[str, Any],
) -> Iterator[Chunk]:
    """One chunk per row: 'column: value' lines as text, typed values in Chunk.fields."""
    symbol_at = next((i for i, name in enumerate(header) if name.strip().lower() in _SYMBOL_COLUMNS), None)
    index = start_index
    for row_number, row in enumerate(rows, start=1):
        row = list(row)
        text = "\n".join(f"{name}: {value}" for name, value in zip(header, row) if value != "")
        if not text:
            continue
        fields, rejected = coerce_extended({field: row[i] for i, field in columns.items() if i < len(row)})
        meta = {**metadata, "row_start": row_number, "row_end": row_number, "record": True}
        if rejected:
            meta["unconverted"] = {name: row[i] for i, name in columns.items() if name in rejected}
        symbol = row[symbol_at].strip() if symbol_at is not None and symbol_at < len(row) else ""
        yield Chunk(index=index, text=text, content_kind=CONTENT_KIND_TABLE, section_path=section_path,
                    metadata=meta, fields={k: v for k, v in fields.items() if v is not None},
                    symbol=symbol or None)
        index += 1


# ---- cell / header normalisation ---------------------------------------------

def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ") if value.time() != dt.time(0) else value.date().isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value).strip()


def _clean_header(raw: Sequence[Any]) -> List[str]:
    """Blank and duplicate column names become col_N / name_N, so rows stay addressable."""
    seen: Dict[str, int] = {}
    header: List[str] = []
    for i, value in enumerate(raw, start=1):
        name = _cell(value) or f"col_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        header.append(name)
    return header


def _is_blank(row: Sequence[Any]) -> bool:
    return all(_cell(v) == "" for v in row)


# ---- readers -----------------------------------------------------------------

def _tolerant(reader: Iterator[List[str]], path: Path, warnings: Optional[List[str]]) -> Iterator[List[str]]:
    """Iterate a csv reader, skipping rows it cannot parse instead of failing the file."""
    bad = 0
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            bad += 1
            continue
        yield row
    if bad:
        _warn(warnings, f"{path.name}: skipped {bad} unparseable row(s)")


def _read_csv(path: Path, warnings: Optional[List[str]] = None) -> Iterator[tuple[Optional[str], List[str], Iterator[List[str]]]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                f.read(1 << 20)
            break
        except UnicodeDecodeError:
            continue
    else:
        encoding = "latin-1"

    with open(path, newline="", encoding=encoding, errors="replace") as f:
        sample = f.read(64 * 1024)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = _tolerant(csv.reader(f, dialect), path, warnings)
        header = None
        for row in reader:
            if not _is_blank(row):
                header = _clean_header(row)
                break
        if header is None:
            return
        yield None, header, (_pad(r, len(header)) for r in reader if not _is_blank(r))


def _read_xlsx(path: Path, warnings: Optional[List[str]] = None) -> Iterator[tuple[Optional[str], List[str], Iterator[List[str]]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = None
            for row in rows:
                if not _is_blank(row):
                    header = _clean_header(row)
                    break
            if header is None:
                continue
            yield ws.title, header, (_pad([_cell(v) for v in r], len(header)) for r in rows if not _is_blank(r))
    finally:
        wb.close()


def _read_xls(path: Path, warnings: Optional[List[str]] = None) -> Iterator[tuple[Optional[str], List[str], Iterator[List[str]]]]:
    import pandas as pd

    for sheet, df in pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine="xlrd").items():
        rows = [[_cell(v) if v == v else "" for v in r] for r in df.itertuples(index=False, name=None)]
        rows = [r for r in rows if not _is_blank(r)]
        if not rows:
            continue
        header = _clean_header(rows[0])
        yield str(sheet), header, (_pad(r, len(header)) for r in rows[1:])


def _flatten(obj: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for key, value in obj.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def _json_records(path: Path, warnings: Optional[List[str]] = None) -> Optional[List[Dict[str, Any]]]:
    """Rows from jsonl, a top-level list of objects, or one list-of-objects value inside an object."""
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # newline-delimited JSON
        records = []
        bad = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
        if not records:
            return None  # not JSON at all: the caller reports the file as unusable
        if bad:
            _warn(warnings, f"{path.name}: skipped {bad} malformed JSON line(s)")
        data = records

    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]
        if len(lists) == 1:
            data = lists[0]
        else:
            data = [data]  # a single object: one row of flattened key/values
    if isinstance(data, list) and data and all(isinstance(i, dict) for i in data):
        return [_flatten(r) for r in data]
    return None


def _read_json(path: Path, warnings: Optional[List[str]] = None) -> Iterator[tuple[Optional[str], List[str], Iterator[List[str]]]]:
    records = _json_records(path, warnings)
    if records is None:
        raise ValueError("JSON is not a list of objects (or an object containing one); no tabular shape")
    keys: Dict[str, None] = {}
    for record in records:
        keys.update(dict.fromkeys(record))
    header = _clean_header(list(keys))
    names = list(keys)
    yield None, header, ([_cell(r.get(k)) for k in names] for r in records)


def _pad(row: Sequence[Any], width: int) -> List[str]:
    cells = [_cell(v) for v in row][:width]
    return cells + [""] * (width - len(cells))


_READERS = {"csv": _read_csv, "xlsx": _read_xlsx, "xls": _read_xls, "json": _read_json}


# ---- public entry point --------------------------------------------------------

def read_table_chunks(
    path: Path,
    source_type: str,
    *,
    size_fn: SizeFn = len,
    max_size: int = CHUNK_SIZE_TOKENS * _CHARS_PER_TOKEN,
    rows_per_chunk: int = TABULAR_ROWS_PER_CHUNK,
    max_rows: int = TABULAR_MAX_ROWS_PER_FILE,
    warnings: Optional[List[str]] = None,
    record_mode: str = TABULAR_RECORD_MODE,
) -> Iterator[Chunk]:
    """
    Stream a tabular file as table chunks, numbered consecutively across sheets.

    Raises ValueError for an unsupported type or an unusable shape (the indexer
    isolates that as a per-file failure). Rows beyond `max_rows` are dropped
    with an entry in `warnings`, as are individual rows that cannot be parsed.

    Each sheet is one table; chunk.metadata carries table_ordinal, sheet,
    row_start/row_end and (for row groups) columns, so the same rows can be
    located again when tables are moved to a relational store.
    """
    reader = _READERS.get(source_type)
    if reader is None:
        raise ValueError(f"no tabular reader for source_type {source_type!r}")

    index = 0
    rows_seen = 0
    for ordinal, (sheet, header, rows) in enumerate(reader(path, warnings)):
        remaining = max_rows - rows_seen
        if remaining <= 0:
            _warn(warnings, f"{path.name}: row limit {max_rows} reached; remaining sheets/rows skipped")
            return

        counted = _Counter(itertools.islice(rows, remaining))
        meta: Dict[str, Any] = {"table_ordinal": ordinal, **({"sheet": sheet} if sheet else {})}
        columns = _record_columns(header) if record_mode == "auto" else {}
        if len(columns) >= TABULAR_RECORD_MIN_MATCHES:
            produced = _record_rows(header, iter(counted), columns, start_index=index, section_path=sheet,
                                    metadata=meta)
        else:
            produced = _group_rows(header, iter(counted), start_index=index, rows_per_chunk=rows_per_chunk,
                                   max_size=max_size, size_fn=size_fn, section_path=sheet, page_range=None,
                                   metadata=meta)
        for chunk in produced:
            index = chunk.index + 1
            yield chunk
        rows_seen += counted.count
        if counted.count >= remaining and next(rows, None) is not None:
            _warn(warnings, f"{path.name}: truncated at {max_rows} rows")


class _Counter:
    """Iterator wrapper that counts how many items were consumed."""

    def __init__(self, it: Iterable[List[str]]):
        self._it = iter(it)
        self.count = 0

    def __iter__(self) -> "_Counter":
        return self

    def __next__(self) -> List[str]:
        item = next(self._it)
        self.count += 1
        return item


def _warn(warnings: Optional[List[str]], message: str) -> None:
    logger.warning(message)
    if warnings is not None:
        warnings.append(message)
