"""
Chunker: cleaned parser blocks -> schema.Chunk objects.

    chunks = chunk_document(blocks, size_fn=token_counter())

A pure function, safe to call in worker processes (no shared state, no torch).

Rules:
  - Sizes are measured with `size_fn` (tokens of the embedding model's tokenizer)
    and no prose chunk exceeds `max_tokens`.
  - A level-1/level-2 heading starts a new chunk. Deeper headings stay inline.
    `section_path` is the heading breadcrumb where the chunk starts.
  - Consecutive prose chunks inside a section overlap by up to `overlap_tokens`.
    Nothing overlaps across a section boundary or a table.
  - Tables are never mixed into prose. Each one goes through
    tabular.table_chunks: whole rows, header repeated, content_kind="table".
    A caption directly before or after a table travels with its first chunk.
  - `page_range` comes from the page numbers of the blocks a chunk actually
    contains; it is None for page-less formats.
  - chunk ids are not assigned here; schema.build_record derives them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, List, Optional, Sequence, Tuple

from config.settings import CHUNK_OVERLAP_TOKENS, CHUNK_SIZE_TOKENS, EMBEDDING_MODEL
from pipeline.schema import (
    BLOCK_CAPTION, BLOCK_HEADER, BLOCK_LIST_ITEM, BLOCK_TABLE, CONTENT_KIND_TABLE, Block, Chunk,
)
from pipeline.tabular import table_chunks

logger = logging.getLogger(__name__)

SizeFn = Callable[[str], int]

_SECTION_BREAK_LEVEL = 2  # headings at this depth or shallower start a new chunk
_SENTENCE_END = re.compile(r"(?<=[.!?।;:])\s+")
_JOIN_COST = 2  # tokens charged for the separator between two pieces


def estimate_tokens(text: str) -> int:
    """Tokenizer-free estimate (about 3 characters per token for mixed text and numbers)."""
    return (len(text) + 2) // 3


@lru_cache(maxsize=1)
def token_counter() -> SizeFn:
    """
    Token counter for EMBEDDING_MODEL, built once per process. Uses the
    `tokenizers` package directly so parse/chunk workers never import torch.
    Falls back to a character estimate if the tokenizer cannot be loaded.
    """
    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_pretrained(EMBEDDING_MODEL)
    except Exception as exc:
        logger.warning(f"tokenizer for {EMBEDDING_MODEL} unavailable ({exc}); sizing chunks by character estimate")
        return estimate_tokens

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count


# ---- internal piece model ------------------------------------------------------

@dataclass
class _Piece:
    text: str
    tokens: int
    page: Optional[int]
    kind: str  # block type
    overlap: bool = False  # carried over from the previous chunk


def _pack(units: Sequence[str], joiner: str, size_fn: SizeFn, max_tokens: int,
          fallback: Callable[[str], List[str]]) -> List[str]:
    """Greedily join units up to max_tokens; a unit that is too large on its own goes to `fallback`."""
    parts: List[str] = []
    current = ""
    for unit in units:
        if not unit:
            continue
        candidate = f"{current}{joiner}{unit}" if current else unit
        if size_fn(candidate) <= max_tokens:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if size_fn(unit) <= max_tokens:
            current = unit
        else:
            parts.extend(fallback(unit))
    if current:
        parts.append(current)
    return parts


def _split_long(text: str, size_fn: SizeFn, max_tokens: int) -> List[str]:
    """Split text that is larger than max_tokens: by sentence, then by word, then by character."""

    def by_chars(unit: str) -> List[str]:
        step = max(1, len(unit) * max_tokens // max(1, size_fn(unit)))
        out: List[str] = []
        while unit:
            cut = min(step, len(unit))
            while cut > 1 and size_fn(unit[:cut]) > max_tokens:
                cut = max(1, cut * 3 // 4)
            out.append(unit[:cut])
            unit = unit[cut:]
        return out

    def by_words(unit: str) -> List[str]:
        return _pack(unit.split(), " ", size_fn, max_tokens, by_chars)

    return _pack(_SENTENCE_END.split(text), " ", size_fn, max_tokens, by_words)


def _render(block: Block) -> str:
    text = block.content.strip()
    if block.type == BLOCK_HEADER:
        return f"{'#' * max(1, min(6, block.level or 1))} {text}"
    if block.type == BLOCK_LIST_ITEM:
        return f"- {text}"
    return text


def _join(pieces: Sequence[_Piece]) -> str:
    out: List[str] = []
    for i, piece in enumerate(pieces):
        if i:
            both_list = piece.kind == BLOCK_LIST_ITEM and pieces[i - 1].kind == BLOCK_LIST_ITEM
            out.append("\n" if both_list else "\n\n")
        out.append(piece.text)
    return "".join(out)


def _page_range(pages: Sequence[Optional[int]]) -> Optional[str]:
    known = [p for p in pages if p is not None]
    if not known:
        return None
    lo, hi = min(known), max(known)
    return str(lo) if lo == hi else f"{lo}-{hi}"


def _tail_for_overlap(pieces: Sequence[_Piece], size_fn: SizeFn, overlap_tokens: int) -> List[_Piece]:
    """The end of a chunk to repeat at the start of the next one."""
    if overlap_tokens <= 0:
        return []
    tail: List[_Piece] = []
    budget = overlap_tokens
    for piece in reversed(pieces):
        if piece.kind == BLOCK_HEADER:
            break
        if piece.tokens <= budget:
            tail.insert(0, _Piece(piece.text, piece.tokens, piece.page, piece.kind, overlap=True))
            budget -= piece.tokens + _JOIN_COST
            continue
        if not tail:  # last piece is larger than the budget: keep its final sentences
            kept: List[str] = []
            used = 0
            for sentence in reversed(_SENTENCE_END.split(piece.text)):
                cost = size_fn(sentence)
                if used + cost > overlap_tokens:
                    break
                kept.insert(0, sentence)
                used += cost
            if kept:
                tail.append(_Piece(" ".join(kept), used, piece.page, piece.kind, overlap=True))
        break
    return tail


# ---- public -------------------------------------------------------------------

def chunk_document(
    blocks: Sequence[Block],
    *,
    size_fn: Optional[SizeFn] = None,
    max_tokens: int = CHUNK_SIZE_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> List[Chunk]:
    size_fn = size_fn or estimate_tokens
    chunks: List[Chunk] = []
    headings: List[Tuple[int, str]] = []  # (level, text) stack
    buffer: List[_Piece] = []
    buffer_path: Optional[str] = None  # section_path where the buffer started
    table_ordinal = 0

    def current_path() -> Optional[str]:
        return " > ".join(text for _, text in headings) or None

    def buffer_tokens() -> int:
        return sum(p.tokens for p in buffer) + _JOIN_COST * max(0, len(buffer) - 1)

    def emit(pieces: Sequence[_Piece], path: Optional[str]) -> None:
        text = _join(pieces)
        if text.strip():
            chunks.append(Chunk(index=len(chunks), text=text, section_path=path,
                                page_range=_page_range([p.page for p in pieces])))

    def flush(*, carry_overlap: bool) -> None:
        """Emit the buffer. Trailing headings are never left dangling at the end of a chunk."""
        nonlocal buffer, buffer_path
        trailing: List[_Piece] = []
        while buffer and buffer[-1].kind == BLOCK_HEADER:
            trailing.insert(0, buffer.pop())
        body = [p for p in buffer if not p.overlap]
        has_content = any(p.kind != BLOCK_HEADER for p in body)
        tail: List[_Piece] = []
        if has_content:
            emit(buffer, buffer_path)
            if carry_overlap:
                tail = _tail_for_overlap(buffer, size_fn, overlap_tokens)
        elif not carry_overlap:
            # only headings (or only overlap) before a boundary: keep headings to lead the next chunk
            trailing = [p for p in buffer if p.kind == BLOCK_HEADER] + trailing
        buffer = tail + trailing
        if not tail:
            buffer_path = None

    def add(piece: _Piece) -> None:
        nonlocal buffer, buffer_path
        if buffer and buffer_tokens() + _JOIN_COST + piece.tokens > max_tokens:
            flush(carry_overlap=True)
            # overlap must never push the new piece out again
            while buffer and buffer_tokens() + _JOIN_COST + piece.tokens > max_tokens:
                buffer.pop(0)
        if not buffer or buffer_path is None:
            buffer_path = current_path()
        buffer.append(piece)

    i = 0
    n = len(blocks)
    while i < n:
        block = blocks[i]
        i += 1

        if block.type == BLOCK_TABLE:
            captions: List[str] = []
            while buffer and buffer[-1].kind == BLOCK_CAPTION and not buffer[-1].overlap:
                captions.insert(0, buffer.pop().text)
            if i < n and blocks[i].type == BLOCK_CAPTION and not captions:
                captions.append(blocks[i].content.strip())
                i += 1
            flush(carry_overlap=False)
            # headings waiting in the buffer describe this table; they are in section_path
            pending_headings, buffer = buffer, []
            caption = " ".join(c for c in captions if c) or None
            page_range = _page_range([block.page_number])
            meta = {"table_ordinal": table_ordinal, **({"caption": caption} if caption else {})}
            table_ordinal += 1
            if block.table is not None and block.table.rows:
                made = table_chunks(block.table, start_index=len(chunks), size_fn=size_fn, max_size=max_tokens,
                                    section_path=current_path(), page_range=page_range, metadata=meta)
            else:
                # no usable structure: keep the rendered text, still isolated as a table
                text = block.content.strip()
                parts = [text] if size_fn(text) <= max_tokens else _split_long(text, size_fn, max_tokens)
                made = [Chunk(index=len(chunks) + k, text=part, content_kind=CONTENT_KIND_TABLE,
                              section_path=current_path(), page_range=page_range, metadata=dict(meta))
                        for k, part in enumerate(parts) if part.strip()]
            if made and caption:
                made[0].text = f"{caption}\n\n{made[0].text}"
            chunks.extend(made)
            if not made:
                buffer = pending_headings  # empty table: headings go back to lead the next prose
            continue

        text = _render(block)
        if not text.strip():
            continue

        if block.type == BLOCK_HEADER:
            level = max(1, block.level or 1)
            if level <= _SECTION_BREAK_LEVEL:
                flush(carry_overlap=False)
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, block.content.strip()))

        tokens = size_fn(text)
        if tokens > max_tokens:
            for part in _split_long(text, size_fn, max_tokens):
                add(_Piece(part, size_fn(part), block.page_number, block.type))
        else:
            add(_Piece(text, tokens, block.page_number, block.type))

    # end of document: emit what is left; a document that is only headings still yields one chunk
    remaining = [p for p in buffer if not p.overlap]
    if any(p.kind != BLOCK_HEADER for p in remaining) or (remaining and not chunks):
        emit(buffer, buffer_path if buffer_path is not None else current_path())

    for index, chunk in enumerate(chunks):
        chunk.index = index
    return chunks
