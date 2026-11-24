"""
LanceDB writer for the `documents` table (schema: pipeline.schema.ChunkRecord).

    store = VectorStore()
    store.open_or_create()          # refuses a table whose schema differs
    store.recover()                 # drop documents a crashed run left half-written
    store.add(records)              # append; one call is atomic
    store.finalize()                # scalar + cosine vector indexes, once per run

Append-only: there is no upsert. "A doc_id is present" must always mean "that
document is complete", so a document is normally written with a single add().
A document too large for one add is written in parts between begin_partial()
and finish_partial(); the doc_id sits in a small ledger file meanwhile, and
recover() deletes whatever such a document left behind after a crash.

Retrieval belongs to the LLM stage. Only count() and get_by_doc_id() are
provided, for verification and tests.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set

import lancedb
import pyarrow as pa

from config.settings import DISTANCE_METRIC, LANCEDB_PATH, LANCEDB_TABLE, VECTOR_INDEX_MIN_ROWS
from pipeline.schema import VECTOR_COLUMN, ChunkRecord

logger = logging.getLogger(__name__)

_DOC_ID = re.compile(r"[0-9a-f]{32}")
_ID_QUERY_BATCH = 500
_SCALAR_INDEX_COLUMNS = ("doc_id", "symbol")
_LEDGER_NAME = "_inflight_docs.json"


class SchemaMismatchError(RuntimeError):
    """The existing table does not match ChunkRecord; writing would corrupt the contract."""


def _check_doc_id(doc_id: str) -> str:
    # doc ids are interpolated into filter strings; only our own hex ids are ever accepted
    if not isinstance(doc_id, str) or not _DOC_ID.fullmatch(doc_id):
        raise ValueError(f"not a valid doc_id: {doc_id!r}")
    return doc_id


class VectorStore:
    def __init__(self, path: Path | str = LANCEDB_PATH, table_name: str = LANCEDB_TABLE):
        self.path = Path(path)
        self.table_name = table_name
        self._schema: pa.Schema = ChunkRecord.to_arrow_schema()
        self._db: Any = None
        self._table: Any = None

    # ---- open ------------------------------------------------------------------

    def open_or_create(self) -> "VectorStore":
        self.path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.path))
        try:
            table = self._db.open_table(self.table_name)
        except ValueError as exc:
            if "not found" not in str(exc).lower():  # only a missing table means "create"
                raise
            self._table = self._db.create_table(self.table_name, schema=ChunkRecord)
            logger.info(f"created table {self.table_name!r} at {self.path}")
        else:
            self._require_matching_schema(table.schema)
            self._table = table
        return self

    def _require_matching_schema(self, existing: pa.Schema) -> None:
        if existing.equals(self._schema):
            return
        have = {f.name: str(f.type) for f in existing}
        want = {f.name: str(f.type) for f in self._schema}
        problems = (
            [f"missing column {n}" for n in want if n not in have]
            + [f"unexpected column {n}" for n in have if n not in want]
            + [f"{n}: is {have[n]}, expected {want[n]}" for n in want if n in have and have[n] != want[n]]
        ) or ["column order differs"]
        raise SchemaMismatchError(
            f"table {self.table_name!r} at {self.path} does not match the current schema "
            f"({'; '.join(problems[:8])}). Load into a new table or path instead of this one."
        )

    @property
    def table(self) -> Any:
        if self._table is None:
            self.open_or_create()
        return self._table

    # ---- write -----------------------------------------------------------------

    def add(self, records: Sequence[Dict[str, Any]]) -> int:
        """Append rows built by schema.build_record. Returns the number written."""
        if not records:
            return 0
        # Building against the schema first means a malformed row fails here, before any write.
        batch = pa.Table.from_pylist(list(records), schema=self._schema)
        self.table.add(batch)
        return batch.num_rows

    def delete_doc(self, doc_id: str) -> None:
        self.table.delete(f"doc_id = '{_check_doc_id(doc_id)}'")

    # ---- partial-document ledger ---------------------------------------------------

    @property
    def _ledger_path(self) -> Path:
        return self.path / _LEDGER_NAME

    def _read_ledger(self) -> List[str]:
        try:
            data = json.loads(self._ledger_path.read_text())
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, str) and _DOC_ID.fullmatch(d)] if isinstance(data, list) else []

    def _write_ledger(self, doc_ids: Iterable[str]) -> None:
        ids = sorted(set(doc_ids))
        if not ids:
            self._ledger_path.unlink(missing_ok=True)
            return
        tmp = self._ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ids))
        tmp.replace(self._ledger_path)

    def begin_partial(self, doc_id: str) -> None:
        """Call before the first of several add() calls for one document."""
        self._write_ledger([*self._read_ledger(), _check_doc_id(doc_id)])

    def finish_partial(self, doc_id: str) -> None:
        """Call after the last add() for that document succeeded."""
        self._write_ledger(d for d in self._read_ledger() if d != doc_id)

    def abort_partial(self, doc_id: str) -> None:
        """Remove whatever was written for a document that failed part-way."""
        self.delete_doc(doc_id)
        self.finish_partial(doc_id)

    def recover(self) -> List[str]:
        """Delete documents an interrupted run left half-written. Returns their ids."""
        stale = self._read_ledger()
        for doc_id in stale:
            logger.warning(f"removing partially written document {doc_id} left by an interrupted run")
            self.delete_doc(doc_id)
        self._write_ledger([])
        return stale

    # ---- read (verification only) --------------------------------------------------

    def count(self) -> int:
        return self.table.count_rows()

    def has_doc(self, doc_id: str) -> bool:
        return self.table.count_rows(f"doc_id = '{_check_doc_id(doc_id)}'") > 0

    def existing_doc_ids(self, doc_ids: Iterable[str]) -> Set[str]:
        """Which of these doc_ids already have rows in the table."""
        wanted = sorted({_check_doc_id(d) for d in doc_ids})
        found: Set[str] = set()
        for start in range(0, len(wanted), _ID_QUERY_BATCH):
            id_list = ", ".join(f"'{d}'" for d in wanted[start:start + _ID_QUERY_BATCH])
            hits = self.table.search().where(f"doc_id IN ({id_list})").select(["doc_id"]).to_arrow()
            found.update(hits.column("doc_id").to_pylist())
        return found

    def get_by_doc_id(self, doc_id: str) -> List[Dict[str, Any]]:
        rows = self.table.search().where(f"doc_id = '{_check_doc_id(doc_id)}'").to_list()
        return sorted(rows, key=lambda r: r["chunk_index"])

    # ---- indexes ---------------------------------------------------------------

    def finalize(self) -> Dict[str, Any]:
        """
        Once per run, after all writes: scalar indexes on the filter columns and
        the cosine vector index. Small tables get no vector index (a brute-force
        scan is exact and fast); readers must then pass metric="cosine" themselves.
        """
        table = self.table
        rows = table.count_rows()
        report: Dict[str, Any] = {"rows": rows, "vector_index": "none"}
        if rows == 0:
            return report

        from lancedb.index import BTree, IvfPq

        existing = {tuple(i.columns): i for i in table.list_indices()}
        for column in _SCALAR_INDEX_COLUMNS:
            if (column,) not in existing:
                table.create_index(column, config=BTree())

        if (VECTOR_COLUMN,) in existing:
            table.optimize()  # folds newly appended rows into the existing indexes
            report["vector_index"] = "updated"
        elif rows >= VECTOR_INDEX_MIN_ROWS:
            table.create_index(VECTOR_COLUMN, config=IvfPq(distance_type=DISTANCE_METRIC))
            report["vector_index"] = "created"
        else:
            table.optimize()
        logger.info(f"finalised {self.table_name!r}: {report}")
        return report
