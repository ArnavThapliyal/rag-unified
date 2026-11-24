"""Point-in-time NSE universe store (SQLite): entities, delisting events, delisting-notice filings.

Built from the collector's outputs by rag_collector.nse_bridge; read by universe-as-of and shipped to
rag-processor as a consistent snapshot (rag_collector.export uses sqlite3's backup API).
Extracted from Unified_Pipeline's storage.py (market-data, fundamentals and Z-factor
tables live in rag-processor's quant stage).
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

_NAME_STOP_RE = re.compile(
    r"\b(LTD|LIMITED|PVT|PRIVATE|THE|CO|COMPANY|CORPN?|CORPORATION|OF)\b"
)


class UniverseStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.database_path), timeout=30.0, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._txn_depth = 0
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        if str(self.database_path) != ":memory:":
            for _attempt in range(10):
                try:
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or _attempt == 9:
                        raise
                    time.sleep(0.05 * (_attempt + 1))
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()
        self._migrate_schema()

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers with BEGIN IMMEDIATE and support nested savepoints."""
        if self._txn_depth == 0:
            self._connection.execute("BEGIN IMMEDIATE")
            self._txn_depth = 1
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._txn_depth = 0
        else:
            sp = f"sp_{self._txn_depth}"
            self._connection.execute(f"SAVEPOINT {sp}")
            self._txn_depth += 1
            try:
                yield self._connection
                self._connection.execute(f"RELEASE SAVEPOINT {sp}")
            except BaseException:
                try:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    self._connection.execute(f"RELEASE SAVEPOINT {sp}")
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._txn_depth -= 1

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Public write transaction context manager (supports nesting via savepoints)."""
        with self._write_txn() as conn:
            yield conn

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "UniverseStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert_nse_entity(self, entity: Mapping[str, Any]) -> tuple[int, bool]:
        """Insert or update an NSE entity; return (entity_id, inserted)."""
        company_name = _clean_str(entity.get("company_name"))
        if not company_name:
            raise ValueError("NSE entity requires a non-empty 'company_name'.")

        isin = _clean_upper(entity.get("isin"))
        nse_symbol = _clean_symbol(entity.get("nse_symbol"))
        normalized_name = _normalize_nse_entity_name(company_name)
        series = _clean_upper(entity.get("series"))
        sector = _clean_str(entity.get("sector"))
        industry = _clean_str(entity.get("industry"))
        listing_date = _clean_str(entity.get("listing_date"))
        incoming_status = _clean_upper(entity.get("listing_status")) or "ACTIVE"
        paid_up_value = _to_float(entity.get("paid_up_value"))
        face_value = _to_float(entity.get("face_value"))
        latest_delisting_date = _clean_str(entity.get("latest_delisting_date"))
        latest_delisting_type = _clean_upper(entity.get("latest_delisting_type"))
        suspension_date = _clean_str(entity.get("suspension_date"))
        suspension_reason = _clean_str(entity.get("suspension_reason"))
        surveillance_stage = _clean_str(entity.get("surveillance_stage"))
        shares_outstanding = _to_int(entity.get("shares_outstanding"))
        source_url = _clean_str(entity.get("source_url"))

        with self._write_txn():
            existing = self._find_existing_nse_entity(isin, nse_symbol, normalized_name)
            if existing is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO nse_entities (
                        isin, nse_symbol, company_name, normalized_name, series,
                        sector, industry, listing_date, listing_status,
                        paid_up_value, face_value, latest_delisting_date,
                        latest_delisting_type, suspension_date, suspension_reason,
                        surveillance_stage, shares_outstanding, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        isin,
                        nse_symbol,
                        company_name,
                        normalized_name,
                        series,
                        sector,
                        industry,
                        listing_date,
                        incoming_status,
                        paid_up_value,
                        face_value,
                        latest_delisting_date,
                        latest_delisting_type,
                        suspension_date,
                        suspension_reason,
                        surveillance_stage,
                        shares_outstanding,
                        source_url,
                    ),
                )
                return int(cursor.lastrowid), True

            merged_status = _resolve_merged_status(
                existing_status=existing["listing_status"],
                existing_listing_date=existing["listing_date"],
                existing_delisting_date=existing["latest_delisting_date"],
                existing_suspension_date=existing["suspension_date"],
                incoming_status=incoming_status,
                incoming_listing_date=listing_date,
                incoming_delisting_date=latest_delisting_date,
                incoming_suspension_date=suspension_date,
            )

            merged_delisting_date = _max_non_empty(
                existing["latest_delisting_date"], latest_delisting_date
            )
            merged_delisting_type = (
                latest_delisting_type
                if (
                    latest_delisting_type
                    and (
                        not existing["latest_delisting_date"]
                        or (
                            latest_delisting_date
                            and latest_delisting_date >= existing["latest_delisting_date"]
                        )
                    )
                )
                else existing["latest_delisting_type"] or latest_delisting_type
            )
            merged_suspension_date = _max_non_empty(
                existing["suspension_date"], suspension_date
            )
            merged_suspension_reason = (
                suspension_reason or existing["suspension_reason"]
            )
            merged_surveillance_stage = (
                surveillance_stage or existing["surveillance_stage"]
            )
            merged_shares = (
                shares_outstanding
                if shares_outstanding is not None and shares_outstanding > 0
                else existing["shares_outstanding"]
            )

            # Prefer active listing symbol/name when entity is ACTIVE, else preserve existing non-empty.
            if incoming_status == "ACTIVE" and nse_symbol:
                merged_symbol = nse_symbol
            else:
                merged_symbol = existing["nse_symbol"] or nse_symbol

            merged_name = (
                company_name
                if (incoming_status == "ACTIVE" and not existing["listing_date"])
                else existing["company_name"] or company_name
            )

            self._connection.execute(
                """
                UPDATE nse_entities SET
                    isin = ?,
                    nse_symbol = ?,
                    company_name = ?,
                    normalized_name = ?,
                    series = ?,
                    sector = ?,
                    industry = ?,
                    listing_date = ?,
                    listing_status = ?,
                    paid_up_value = ?,
                    face_value = ?,
                    latest_delisting_date = ?,
                    latest_delisting_type = ?,
                    suspension_date = ?,
                    suspension_reason = ?,
                    surveillance_stage = ?,
                    shares_outstanding = ?,
                    source_url = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    existing["isin"] or isin,
                    merged_symbol,
                    merged_name,
                    normalized_name or existing["normalized_name"],
                    series or existing["series"],
                    sector or existing["sector"],
                    industry or existing["industry"],
                    listing_date or existing["listing_date"],
                    merged_status,
                    paid_up_value if paid_up_value is not None else existing["paid_up_value"],
                    face_value if face_value is not None else existing["face_value"],
                    merged_delisting_date,
                    merged_delisting_type,
                    merged_suspension_date,
                    merged_suspension_reason,
                    merged_surveillance_stage,
                    merged_shares,
                    existing["source_url"] or source_url,
                    existing["id"],
                ),
            )
            return int(existing["id"]), False

    def upsert_delisting_event(self, entity_id: int, event: Mapping[str, Any]) -> bool:
        """Insert or update a delisting event/lead; return True if newly inserted."""
        record_id = _clean_str(event.get("record_id"))
        if not record_id:
            raise ValueError("Delisting event requires a non-empty 'record_id'.")
        scope = _clean_upper(event.get("scope")) or "NSE_2006_2026"
        company_name = _clean_str(event.get("company_name")) or ""

        values = (
            entity_id,
            record_id,
            scope,
            company_name,
            _clean_str(event.get("nse_symbol")),
            _clean_upper(event.get("isin")),
            _clean_upper(event.get("delisting_type")),
            _clean_str(event.get("order_date")),
            _clean_str(event.get("effective_delisting_date")),
            _to_int(event.get("calendar_year")),
            _clean_str(event.get("financial_year")),
            _to_sqlite_bool(event.get("is_priority_year")),
            _clean_str(event.get("regulatory_regime")),
            _to_float(event.get("exit_or_fair_value_inr")),
            _clean_str(event.get("source_url")),
            _clean_str(event.get("local_artifact_path")),
            _clean_upper(event.get("confidence_status")),
            _to_int(event.get("source_count")),
            _clean_str(event.get("all_source_urls")),
            _clean_str(event.get("all_artifact_paths")),
            _clean_str(event.get("source_conflicts")),
            _clean_str(event.get("review_flags")),
            _clean_str(event.get("date_source")),
        )

        with self._write_txn():
            existing = self._connection.execute(
                "SELECT id FROM delisting_events WHERE record_id = ?", (record_id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO delisting_events (
                    entity_id, record_id, scope, company_name, nse_symbol, isin,
                    delisting_type, order_date, effective_delisting_date,
                    calendar_year, financial_year, is_priority_year,
                    regulatory_regime, exit_or_fair_value_inr, source_url,
                    local_artifact_path, confidence_status, source_count,
                    all_source_urls, all_artifact_paths, source_conflicts,
                    review_flags, date_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    entity_id = excluded.entity_id,
                    scope = excluded.scope,
                    company_name = excluded.company_name,
                    nse_symbol = excluded.nse_symbol,
                    isin = excluded.isin,
                    delisting_type = excluded.delisting_type,
                    order_date = excluded.order_date,
                    effective_delisting_date = excluded.effective_delisting_date,
                    calendar_year = excluded.calendar_year,
                    financial_year = excluded.financial_year,
                    is_priority_year = excluded.is_priority_year,
                    regulatory_regime = excluded.regulatory_regime,
                    exit_or_fair_value_inr = excluded.exit_or_fair_value_inr,
                    source_url = excluded.source_url,
                    local_artifact_path = excluded.local_artifact_path,
                    confidence_status = excluded.confidence_status,
                    source_count = excluded.source_count,
                    all_source_urls = excluded.all_source_urls,
                    all_artifact_paths = excluded.all_artifact_paths,
                    source_conflicts = excluded.source_conflicts,
                    review_flags = excluded.review_flags,
                    date_source = excluded.date_source
                """,
                values,
            )
            return existing is None

    def list_delisting_events(
        self, scope: str | None = None, entity_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM delisting_events {where} ORDER BY COALESCE(effective_delisting_date, order_date, ''), id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_delisting_events(self, scope: str, keep_record_ids: set[str]) -> int:
        """Delete delisting_events in `scope` whose record_id is no longer in `keep_record_ids`."""
        with self._write_txn():
            existing = [
                row["record_id"]
                for row in self._connection.execute(
                    "SELECT record_id FROM delisting_events WHERE scope = ?", (scope,)
                ).fetchall()
            ]
            stale = [rid for rid in existing if rid not in keep_record_ids]
            if not stale:
                return 0
            self._connection.executemany(
                "DELETE FROM delisting_events WHERE scope = ? AND record_id = ?",
                [(scope, rid) for rid in stale],
            )
            return len(stale)

    def count_nse_entities(self, include_bse_only: bool = False) -> int:
        if include_bse_only:
            row = self._connection.execute("SELECT COUNT(*) AS cnt FROM nse_entities").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS cnt FROM nse_entities WHERE listing_status != 'BSE_ONLY'"
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_delisting_events(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS cnt FROM delisting_events").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_qualitative_filings(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS cnt FROM qualitative_filings").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def list_nse_entities(
        self,
        status: str | None = None,
        as_of_date: str | None = None,
        include_bse_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List NSE entities, optionally reconstructed point-in-time at as_of_date."""
        rows = self._connection.execute(
            "SELECT * FROM nse_entities ORDER BY normalized_name, id"
        ).fetchall()

        if as_of_date is None:
            out: list[dict[str, Any]] = []
            for row in rows:
                if not include_bse_only and row["listing_status"] == "BSE_ONLY":
                    continue
                if status is not None and row["listing_status"] != status:
                    continue
                out.append(dict(row))
            return out

        events_by_entity: dict[int, list[str]] = {}
        ev_rows = self._connection.execute(
            """
            SELECT entity_id, COALESCE(effective_delisting_date, order_date) AS ev_date
            FROM delisting_events
            WHERE scope IN ('NSE_2006_2026', 'PRE_2006')
            """
        ).fetchall()
        for ev in ev_rows:
            if ev["ev_date"]:
                events_by_entity.setdefault(ev["entity_id"], []).append(ev["ev_date"])
        for dates in events_by_entity.values():
            dates.sort()

        results: list[dict[str, Any]] = []
        for row in rows:
            if not include_bse_only and row["listing_status"] == "BSE_ONLY":
                continue
            if status is not None and row["listing_status"] != status:
                continue

            listing_date = row["listing_date"]
            ev_dates = events_by_entity.get(row["id"], [])
            included = False
            subsequent_delisting_date: str | None = None

            if not ev_dates:
                if row["listing_status"] not in ("DELISTED", "DELISTED_PRE_2006"):
                    if listing_date is None or listing_date <= as_of_date:
                        included = True
            elif row["listing_status"] != "ACTIVE":
                future_dates = [d for d in ev_dates if d > as_of_date]
                if future_dates and (listing_date is None or listing_date <= as_of_date):
                    included = True
                    subsequent_delisting_date = future_dates[0]
            else:
                prior_dates = [d for d in ev_dates if listing_date is None or d < listing_date]
                if prior_dates:
                    d_prev = prior_dates[-1]
                    if listing_date is not None:
                        if as_of_date >= listing_date:
                            included = True
                            future_after = [d for d in ev_dates if d > as_of_date]
                            subsequent_delisting_date = future_after[0] if future_after else None
                        elif as_of_date < prior_dates[0]:
                            included = True
                            subsequent_delisting_date = prior_dates[0]
                    elif as_of_date < d_prev:
                        included = True
                        subsequent_delisting_date = prior_dates[0]
                else:
                    future_dates = [d for d in ev_dates if d > as_of_date]
                    if future_dates and (listing_date is None or listing_date <= as_of_date):
                        included = True
                        subsequent_delisting_date = future_dates[0]

            if included:
                item = dict(row)
                item["as_of_status"] = "ACTIVE_AT_DATE"
                item["subsequent_delisting_date"] = subsequent_delisting_date
                results.append(item)
        return results

    def _resolve_entity_id(self, isin: str | None, nse_symbol: str | None) -> int | None:
        if isin:
            row = self._connection.execute(
                "SELECT id FROM nse_entities WHERE isin = ? LIMIT 1", (isin,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
        if nse_symbol:
            row = self._connection.execute(
                "SELECT id FROM nse_entities WHERE nse_symbol = ? LIMIT 1", (nse_symbol,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
        return None

    def upsert_qualitative_filing(self, record: Mapping[str, Any]) -> bool:
        """Insert a qualitative filing deterministically by content hash."""
        isin = _clean_upper(record.get("isin"))
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
            isin, nse_symbol
        )

        payload = {
            "isin": isin or "",
            "nse_symbol": nse_symbol or "",
            "broadcast_timestamp": _clean_str(record.get("broadcast_timestamp")) or "",
            "surveillance_stage": _clean_str(record.get("surveillance_stage")) or "",
            "announcement_category": _clean_str(record.get("announcement_category")) or "",
            "announcement_headline": _clean_str(record.get("announcement_headline")) or "",
            "announcement_body_md": _clean_str(record.get("announcement_body_md")) or "",
            "concall_commentary_md": _clean_str(record.get("concall_commentary_md")) or "",
            "concall_guidance_md": _clean_str(record.get("concall_guidance_md")) or "",
            "sast_insider_activity": _serialize_optional_text(
                record.get("sast_insider_activity")
            )
            or "",
            "bulk_block_deal_participants": _serialize_optional_text(
                record.get("bulk_block_deal_participants")
            )
            or "",
            "source_url": _clean_str(record.get("source_url")) or "",
            "local_artifact_path": _clean_str(record.get("local_artifact_path")) or "",
        }
        filing_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

        with self._write_txn():
            existing = self._connection.execute(
                "SELECT id FROM qualitative_filings WHERE filing_hash = ?",
                (filing_hash,),
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO qualitative_filings (
                    entity_id, filing_hash, isin, nse_symbol, broadcast_timestamp,
                    surveillance_stage, announcement_category, announcement_headline,
                    announcement_body_md, concall_commentary_md, concall_guidance_md,
                    sast_insider_activity, bulk_block_deal_participants, source_url,
                    local_artifact_path, confidence_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filing_hash) DO UPDATE SET
                    entity_id = COALESCE(qualitative_filings.entity_id, excluded.entity_id)
                """,
                (
                    entity_id,
                    filing_hash,
                    isin,
                    nse_symbol,
                    _clean_str(record.get("broadcast_timestamp")),
                    _clean_str(record.get("surveillance_stage")),
                    _clean_str(record.get("announcement_category")),
                    _clean_str(record.get("announcement_headline")),
                    _clean_str(record.get("announcement_body_md")),
                    _clean_str(record.get("concall_commentary_md")),
                    _clean_str(record.get("concall_guidance_md")),
                    _serialize_optional_text(record.get("sast_insider_activity")),
                    _serialize_optional_text(record.get("bulk_block_deal_participants")),
                    _clean_str(record.get("source_url")),
                    _clean_str(record.get("local_artifact_path")),
                    _clean_upper(record.get("confidence_status")),
                ),
            )
            return existing is None

    def list_qualitative_filings(
        self, nse_symbol: str | None = None, as_of_timestamp: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if nse_symbol is not None:
            clauses.append("nse_symbol = ?")
            params.append(nse_symbol.strip().upper())
        if as_of_timestamp is not None:
            clauses.append("(broadcast_timestamp IS NOT NULL AND broadcast_timestamp <= ?)")
            params.append(as_of_timestamp)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM qualitative_filings {where} ORDER BY COALESCE(broadcast_timestamp, ''), id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _find_existing_nse_entity(
        self,
        isin: str | None,
        nse_symbol: str | None,
        normalized_name: str,
    ) -> sqlite3.Row | None:
        if isin:
            row = self._connection.execute(
                "SELECT * FROM nse_entities WHERE isin = ? LIMIT 1", (isin,)
            ).fetchone()
            if row is not None:
                return row

        if nse_symbol:
            rows = self._connection.execute(
                "SELECT * FROM nse_entities WHERE nse_symbol = ? ORDER BY id",
                (nse_symbol,),
            ).fetchall()
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                return row

        if normalized_name:
            rows = self._connection.execute(
                "SELECT * FROM nse_entities WHERE normalized_name = ? ORDER BY id",
                (normalized_name,),
            ).fetchall()
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                if nse_symbol and row["nse_symbol"] == nse_symbol:
                    return row
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                return row
        return None

    def _create_schema(self) -> None:
        ddl = """
            CREATE TABLE IF NOT EXISTS nse_entities (
                id INTEGER PRIMARY KEY,
                isin TEXT,
                nse_symbol TEXT,
                company_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                series TEXT,
                sector TEXT,
                industry TEXT,
                listing_date TEXT,
                listing_status TEXT NOT NULL,
                paid_up_value REAL,
                face_value REAL,
                latest_delisting_date TEXT,
                latest_delisting_type TEXT,
                suspension_date TEXT,
                suspension_reason TEXT,
                surveillance_stage TEXT,
                shares_outstanding INTEGER,
                source_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS nse_entities_isin_idx
                ON nse_entities(isin);

            CREATE INDEX IF NOT EXISTS nse_entities_symbol_idx
                ON nse_entities(nse_symbol);

            CREATE INDEX IF NOT EXISTS nse_entities_norm_name_idx
                ON nse_entities(normalized_name);

            CREATE INDEX IF NOT EXISTS nse_entities_status_idx
                ON nse_entities(listing_status);

            CREATE TABLE IF NOT EXISTS delisting_events (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL REFERENCES nse_entities(id),
                record_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                company_name TEXT NOT NULL,
                nse_symbol TEXT,
                isin TEXT,
                delisting_type TEXT,
                order_date TEXT,
                effective_delisting_date TEXT,
                calendar_year INTEGER,
                financial_year TEXT,
                is_priority_year INTEGER,
                regulatory_regime TEXT,
                exit_or_fair_value_inr REAL,
                source_url TEXT,
                local_artifact_path TEXT,
                confidence_status TEXT,
                source_count INTEGER,
                all_source_urls TEXT,
                all_artifact_paths TEXT,
                source_conflicts TEXT,
                review_flags TEXT,
                date_source TEXT
            );

            CREATE INDEX IF NOT EXISTS delisting_events_entity_idx
                ON delisting_events(entity_id);

            CREATE INDEX IF NOT EXISTS delisting_events_scope_idx
                ON delisting_events(scope);

            CREATE TABLE IF NOT EXISTS qualitative_filings (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER REFERENCES nse_entities(id),
                filing_hash TEXT NOT NULL UNIQUE,
                isin TEXT,
                nse_symbol TEXT,
                broadcast_timestamp TEXT,
                surveillance_stage TEXT,
                announcement_category TEXT,
                announcement_headline TEXT,
                announcement_body_md TEXT,
                concall_commentary_md TEXT,
                concall_guidance_md TEXT,
                sast_insider_activity TEXT,
                bulk_block_deal_participants TEXT,
                source_url TEXT,
                local_artifact_path TEXT,
                confidence_status TEXT
            );

            CREATE INDEX IF NOT EXISTS qualitative_filings_sym_ts_idx
                ON qualitative_filings(nse_symbol, broadcast_timestamp);

            CREATE INDEX IF NOT EXISTS qualitative_filings_entity_idx
                ON qualitative_filings(entity_id);
        """
        with self._write_txn():
            for stmt in ddl.split(";"):
                stmt = stmt.strip()
                if stmt:
                    self._connection.execute(stmt)

    def _migrate_schema(self) -> None:
        new_entity_cols = (
            ("suspension_date", "TEXT"),
            ("suspension_reason", "TEXT"),
            ("surveillance_stage", "TEXT"),
            ("shares_outstanding", "INTEGER"),
        )
        with self._write_txn():
            entity_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(nse_entities)"
                ).fetchall()
            }
            for col_name, col_type in new_entity_cols:
                if col_name not in entity_cols:
                    self._connection.execute(
                        f"ALTER TABLE nse_entities ADD COLUMN {col_name} {col_type}"
                    )
            dup_isin = self._connection.execute(
                """
                SELECT isin FROM nse_entities
                WHERE isin IS NOT NULL AND isin != ''
                GROUP BY isin HAVING COUNT(*) > 1 LIMIT 1
                """
            ).fetchone()
            if dup_isin is None:
                self._connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS nse_entities_isin_unique
                        ON nse_entities(isin)
                        WHERE isin IS NOT NULL AND isin != ''
                    """
                )


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalize_nse_entity_name(value: str | None) -> str:
    s = re.sub(r"[.,&()'\"\-/]", " ", (value or "").upper())
    return re.sub(r"\s+", " ", _NAME_STOP_RE.sub(" ", s)).strip()


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_upper(value: Any) -> str | None:
    text = _clean_str(value)
    return text.upper() if text else None


def _clean_symbol(value: Any) -> str | None:
    text = _clean_upper(value)
    if not text or text == "UNKNOWN":
        return None
    return text


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_sqlite_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return 1
    if text in {"0", "false", "no", "n"}:
        return 0
    return None


def _serialize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _max_non_empty(first: str | None, second: str | None) -> str | None:
    if first and second:
        return max(first, second)
    return first or second


def _resolve_merged_status(
    *,
    existing_status: str,
    existing_listing_date: str | None,
    existing_delisting_date: str | None,
    existing_suspension_date: str | None = None,
    incoming_status: str,
    incoming_listing_date: str | None,
    incoming_delisting_date: str | None,
    incoming_suspension_date: str | None = None,
) -> str:
    delisted_statuses = {"DELISTED", "DELISTED_PRE_2006"}

    if existing_status == "BSE_ONLY" and incoming_status != "BSE_ONLY":
        return incoming_status
    if incoming_status == "BSE_ONLY" and existing_status != "BSE_ONLY":
        return existing_status

    if existing_status in delisted_statuses and incoming_status in ("PROPOSED_DELISTING", "SUSPENDED"):
        return existing_status

    if existing_status == "SUSPENDED" and incoming_status in delisted_statuses:
        return incoming_status

    if existing_status == "PROPOSED_DELISTING" and incoming_status == "SUSPENDED":
        return "SUSPENDED"

    if existing_status == "PROPOSED_DELISTING" and incoming_status == "ACTIVE":
        return "PROPOSED_DELISTING"

    if existing_status == "SUSPENDED" and incoming_status == "PROPOSED_DELISTING":
        return "SUSPENDED"

    if existing_status == "ACTIVE" and incoming_status == "SUSPENDED":
        if (
            existing_listing_date
            and incoming_suspension_date
            and incoming_suspension_date < existing_listing_date
        ):
            return "ACTIVE"
        return "SUSPENDED"

    if existing_status == "SUSPENDED" and incoming_status == "ACTIVE":
        if (
            incoming_listing_date
            and existing_suspension_date
            and existing_suspension_date < incoming_listing_date
        ):
            return "ACTIVE"
        return "SUSPENDED"

    if existing_status == "ACTIVE" and incoming_status in delisted_statuses:
        if (
            existing_listing_date
            and incoming_delisting_date
            and incoming_delisting_date < existing_listing_date
        ):
            return "ACTIVE"
        return incoming_status

    if existing_status in delisted_statuses and incoming_status == "ACTIVE":
        if (
            incoming_listing_date
            and existing_delisting_date
            and existing_delisting_date < incoming_listing_date
        ):
            return "ACTIVE"
        return existing_status

    if existing_status in delisted_statuses and incoming_status in delisted_statuses:
        if existing_status == "DELISTED" or incoming_status == "DELISTED":
            return "DELISTED"
        return incoming_status

    return incoming_status or existing_status
