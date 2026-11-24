"""
CPU-side work for one delivered file: route -> metadata -> parse/clean/chunk
(documents) or stream rows (tabular). No embedding and no database writes
happen here, so this code is safe to run in many worker processes at once.

    for message in process_path(path, ctx):
        ...

Messages, in order, for each routed item (a zip yields one item per member):

    (MSG_CHUNKS, key, SourceDocument, [Chunk, ...])   zero or more, at most ctx.batch_size chunks each
    (MSG_DONE, ItemResult)                            exactly one, always last for that item

Nothing in here raises for a bad file or missing metadata: every problem ends
up as an ItemResult with status "failed" and a reason. Chunks already emitted
for an item that then fails are discarded by the receiver.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Tuple

from config.settings import EMBED_BATCH_SIZE, SCRATCH_DIR
from pipeline.chunker import SizeFn, chunk_document, token_counter
from pipeline.cleaner import clean_blocks
from pipeline.metadata import MetadataResolver, build_source_document
from pipeline.schema import CONTENT_KIND_TABLE, Chunk, SourceDocument
from pipeline.tabular import read_table_chunks
from pipeline.type_router import HANDLER_DOCUMENT, HANDLER_TABULAR, RoutedFile, open_routed

logger = logging.getLogger(__name__)

MSG_CHUNKS = "chunks"
MSG_DONE = "done"

STATUS_OK = "indexed"
STATUS_EMPTY = "empty"                    # readable, but nothing to index
STATUS_DUPLICATE = "skipped_duplicate"    # same bytes already in the table or seen earlier in this run
STATUS_UNSUPPORTED = "skipped_unsupported"
STATUS_FAILED = "failed"
STATUSES = (STATUS_OK, STATUS_EMPTY, STATUS_DUPLICATE, STATUS_UNSUPPORTED, STATUS_FAILED)

ItemKey = Tuple[str, str]  # (delivered path, display name)
Message = Tuple[Any, ...]


@dataclass
class ItemResult:
    key: ItemKey
    status: str
    reason: Optional[str] = None
    doc_id: Optional[str] = None
    source_type: Optional[str] = None
    chunks: int = 0  # emitted by the worker; replaced by rows written once stored
    warnings: List[str] = field(default_factory=list)
    missing: Tuple[str, ...] = ()  # vendor metadata fields that were absent

    @property
    def name(self) -> str:
        return self.key[1]


class WorkerContext:
    """Per-process state: metadata cache, tokenizer and (lazily) the docling parser."""

    def __init__(
        self,
        root: Path,
        *,
        scratch_dir: Path = SCRATCH_DIR,
        is_known: Optional[Callable[[str], bool]] = None,
        size_fn: Optional[SizeFn] = None,
        parser: Any = None,
        batch_size: int = EMBED_BATCH_SIZE,
    ):
        self.resolver = MetadataResolver(Path(root))
        self.scratch_dir = Path(scratch_dir)
        self.is_known = is_known or (lambda doc_id: False)
        self._size_fn = size_fn
        self._parser = parser
        self.batch_size = max(1, batch_size)

    @property
    def size_fn(self) -> SizeFn:
        if self._size_fn is None:
            self._size_fn = token_counter()
        return self._size_fn

    @property
    def parser(self) -> Any:
        # Created on the first document so a worker that only sees tabular files never loads docling.
        if self._parser is None:
            from pipeline.parser import Parser

            self._parser = Parser()
        return self._parser


def _missing_metadata(doc: SourceDocument) -> Tuple[str, ...]:
    return tuple(name for name in ("symbol", "doc_type", "allowed_roles") if getattr(doc, name) in (None, "", []))


def _document_chunks(item: RoutedFile, ctx: WorkerContext, warnings: List[str]) -> Iterator[Chunk]:
    result = ctx.parser.parse(item.path)
    warnings.extend(result.warnings)
    blocks, _removed = clean_blocks(result.blocks, result.page_count)
    return iter(chunk_document(blocks, size_fn=ctx.size_fn))


def _tabular_chunks(item: RoutedFile, ctx: WorkerContext, warnings: List[str]) -> Iterator[Chunk]:
    return read_table_chunks(item.path, item.source_type, size_fn=ctx.size_fn, warnings=warnings)


def _tag_table(chunk: Chunk, doc_id: str) -> Chunk:
    """Give every table chunk the id of the table it came from, stable across runs."""
    if chunk.content_kind == CONTENT_KIND_TABLE and "table_ordinal" in chunk.metadata:
        chunk.metadata["table_id"] = f"{doc_id}:{chunk.metadata['table_ordinal']}"
    return chunk


def _process_item(item: RoutedFile, delivered: Path, ctx: WorkerContext) -> Iterator[Message]:
    key: ItemKey = (str(delivered), item.display_name)
    result = ItemResult(key=key, status=STATUS_FAILED, source_type=item.source_type)
    try:
        if item.handler not in (HANDLER_DOCUMENT, HANDLER_TABULAR):
            result.status, result.reason = STATUS_UNSUPPORTED, item.reason or "unsupported"
            yield (MSG_DONE, result)
            return

        doc = build_source_document(item, ctx.resolver)  # metadata never raises; an unreadable file does
        result.doc_id = doc.doc_id
        result.missing = _missing_metadata(doc)
        if ctx.is_known(doc.doc_id):
            result.status, result.reason = STATUS_DUPLICATE, "already in the table"
            yield (MSG_DONE, result)
            return

        produce = _document_chunks if item.handler == HANDLER_DOCUMENT else _tabular_chunks
        chunks = (_tag_table(c, doc.doc_id) for c in produce(item, ctx, result.warnings))
        while True:
            batch = list(itertools.islice(chunks, ctx.batch_size))
            if not batch:
                break
            result.chunks += len(batch)
            yield (MSG_CHUNKS, key, doc, batch)

        result.status = STATUS_OK if result.chunks else STATUS_EMPTY
        if not result.chunks:
            result.reason = "no text or rows found"
    except Exception as exc:
        logger.warning(f"failed on {item.display_name}: {exc}", exc_info=True)
        result.status, result.reason = STATUS_FAILED, f"{type(exc).__name__}: {exc}"
    yield (MSG_DONE, result)


def process_path(path: Path, ctx: WorkerContext) -> Iterator[Message]:
    """All messages for one delivered file. Always ends with at least one MSG_DONE."""
    path = Path(path)
    try:
        # Zip members stay on disk until this with-block exits, i.e. until they are processed.
        with open_routed(path, ctx.scratch_dir) as routed:
            if not routed:
                yield (MSG_DONE, ItemResult((str(path), path.name), STATUS_UNSUPPORTED, "not a regular data file"))
                return
            for item in routed:
                yield from _process_item(item, path, ctx)
    except Exception as exc:  # routing or extraction problem (corrupt zip, limits exceeded, ...)
        logger.warning(f"failed to route {path}: {exc}", exc_info=True)
        yield (MSG_DONE, ItemResult((str(path), path.name), STATUS_FAILED, f"{type(exc).__name__}: {exc}"))

