"""Quant store (SQLite): point-in-time universe, daily market data, quarterly fundamentals, Z calibrations.

Hardened from Unified_Pipeline's company_pipeline/storage.py (WAL + BEGIN IMMEDIATE + nested savepoints,
nse_entities_isin_unique, PIT & recycled-symbol fixes, company profile/timeline, ISIN backfill).
Market records are normalized against pipeline.schema.EXTENDED_FIELDS.
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

from pipeline.schema import EXTENDED_FIELDS


def normalize_market_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """One record with exactly the 50 market columns, absent ones null (unknown keys are an error)."""
    unknown = set(record) - set(EXTENDED_FIELDS)
    if unknown:
        raise ValueError(f"Unknown market data column(s): {', '.join(sorted(unknown))}")
    return {column: record.get(column) for column in EXTENDED_FIELDS}


_NAME_STOP_RE = re.compile(
    r"\b(LTD|LIMITED|PVT|PRIVATE|THE|CO|COMPANY|CORPN?|CORPORATION|OF)\b"
)


class QuantStore:
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

    def __enter__(self) -> "QuantStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
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
            ev_date = _clean_str(event.get("effective_delisting_date")) or _clean_str(
                event.get("order_date")
            )
            ev_type = _clean_upper(event.get("delisting_type"))
            if scope in ("NSE_2006_2026", "PRE_2006") and ev_date:
                self._connection.execute(
                    """
                    UPDATE nse_entities
                    SET latest_delisting_date = ?,
                        latest_delisting_type = COALESCE(?, latest_delisting_type),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                      AND (latest_delisting_date IS NULL OR latest_delisting_date <= ?)
                    """,
                    (ev_date, ev_type, entity_id, ev_date),
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
            row = self._connection.execute(
                "SELECT COUNT(*) AS cnt FROM nse_entities"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS cnt FROM nse_entities WHERE listing_status != 'BSE_ONLY'"
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_delisting_events(self, scope: str | None = None) -> int:
        if scope is not None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS cnt FROM delisting_events WHERE scope = ?", (scope,)
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS cnt FROM delisting_events"
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_qualitative_filings(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS cnt FROM qualitative_filings"
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def get_nse_entity(
        self, *, isin: str | None = None, nse_symbol: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch a single NSE entity record by exact ISIN or NSE symbol."""
        code = _clean_upper(isin)
        sym = _clean_symbol(nse_symbol)
        if code:
            row = self._connection.execute(
                "SELECT * FROM nse_entities WHERE isin = ? LIMIT 1", (code,)
            ).fetchone()
            if row is not None:
                return dict(row)
        if sym:
            row = self._connection.execute(
                "SELECT * FROM nse_entities WHERE nse_symbol = ? "
                "ORDER BY (listing_status = 'ACTIVE') DESC, id ASC LIMIT 1",
                (sym,),
            ).fetchone()
            if row is not None:
                return dict(row)
        return None

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
                susp_date = row["suspension_date"]
                is_relisted_after_susp = bool(
                    row["listing_status"] == "ACTIVE"
                    and listing_date
                    and susp_date
                    and susp_date < listing_date <= as_of_date
                )
                if (
                    susp_date
                    and susp_date <= as_of_date
                    and not is_relisted_after_susp
                    and (not subsequent_delisting_date or as_of_date < subsequent_delisting_date)
                ):
                    item["as_of_status"] = "SUSPENDED_AT_DATE"
                else:
                    item["as_of_status"] = "ACTIVE_AT_DATE"
                item["subsequent_delisting_date"] = subsequent_delisting_date
                results.append(item)
        return results

    def backfill_entity_isin(
        self,
        nse_symbol: str,
        isin: str,
        observed_date: str | None = None,
    ) -> bool:
        """Safely attach a validated equity ISIN to an ISIN-less entity matching nse_symbol."""
        sym = _clean_symbol(nse_symbol)
        code = _clean_upper(isin)
        if not sym or not code or not _is_valid_equity_isin(code):
            return False

        with self._write_txn():
            owner = self._connection.execute(
                "SELECT id FROM nse_entities WHERE isin = ? LIMIT 1", (code,)
            ).fetchone()
            if owner is not None:
                return False

            target = self._connection.execute(
                """
                SELECT * FROM nse_entities
                WHERE nse_symbol = ? AND (isin IS NULL OR isin = '')
                ORDER BY id LIMIT 1
                """,
                (sym,),
            ).fetchone()
            if target is None:
                return False

            if (
                target["listing_status"] in ("DELISTED", "DELISTED_PRE_2006")
                and target["latest_delisting_date"]
                and observed_date
                and observed_date[:10] > target["latest_delisting_date"][:10]
            ):
                return False

            self._connection.execute(
                """
                UPDATE nse_entities SET isin = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (code, target["id"]),
            )
            self._connection.execute(
                """
                UPDATE delisting_events SET isin = ?
                WHERE entity_id = ? AND (isin IS NULL OR isin = '')
                """,
                (code, target["id"]),
            )
        return True

    def get_company_profile_and_timeline(
        self,
        identifier: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a company's lifecycle status, latest quant snapshot, fundamentals, and qualitative timeline."""
        ident = _clean_str(identifier)
        if not ident:
            return None
        ident_upper = ident.upper()
        norm_name = _normalize_nse_entity_name(ident)

        entity_row = self._connection.execute(
            "SELECT * FROM nse_entities WHERE isin = ? LIMIT 1", (ident_upper,)
        ).fetchone()
        if entity_row is None:
            entity_row = self._connection.execute(
                "SELECT * FROM nse_entities WHERE nse_symbol = ? "
                "ORDER BY (listing_status = 'ACTIVE') DESC, id ASC LIMIT 1",
                (ident_upper,),
            ).fetchone()
        if entity_row is None and norm_name:
            entity_row = self._connection.execute(
                "SELECT * FROM nse_entities WHERE normalized_name = ? ORDER BY id LIMIT 1",
                (norm_name,),
            ).fetchone()
        if entity_row is None:
            return None

        entity = dict(entity_row)
        entity_id = int(entity["id"])
        nse_symbol = entity.get("nse_symbol")
        d_cut = as_of_date[:10] if as_of_date else None
        ts_cut = (
            f"{as_of_date.strip()}T23:59:59"
            if as_of_date and len(as_of_date.strip()) == 10
            else (as_of_date.strip() if as_of_date else None)
        )

        all_events = [
            dict(r)
            for r in self._connection.execute(
                """
                SELECT * FROM delisting_events
                WHERE entity_id = ?
                ORDER BY COALESCE(effective_delisting_date, order_date, ''), id
                """,
                (entity_id,),
            ).fetchall()
        ]
        if d_cut is not None:
            delisting_events = [
                ev
                for ev in all_events
                if (ev.get("effective_delisting_date") or ev.get("order_date"))
                and (ev.get("effective_delisting_date") or ev.get("order_date")) <= d_cut
            ]
        else:
            delisting_events = all_events

        if d_cut is None:
            status_as_of = entity["listing_status"]
        else:
            scope_events = [
                ev
                for ev in all_events
                if ev.get("scope") in ("NSE_2006_2026", "PRE_2006")
                and (ev.get("effective_delisting_date") or ev.get("order_date"))
            ]
            past_delistings = sorted(
                (ev.get("effective_delisting_date") or ev.get("order_date"))
                for ev in scope_events
                if (ev.get("effective_delisting_date") or ev.get("order_date")) <= d_cut
            )
            future_delistings = sorted(
                (ev.get("effective_delisting_date") or ev.get("order_date"))
                for ev in scope_events
                if (ev.get("effective_delisting_date") or ev.get("order_date")) > d_cut
            )
            ld = entity.get("listing_date")
            sd = entity.get("suspension_date")
            if past_delistings:
                last_delist = past_delistings[-1]
                if (
                    entity["listing_status"] == "ACTIVE"
                    and ld
                    and ld > last_delist
                    and d_cut >= ld
                ):
                    status_as_of = "ACTIVE"
                else:
                    status_as_of = "DELISTED"
            elif ld and d_cut < ld and not future_delistings:
                status_as_of = "NOT_YET_LISTED"
            elif sd and sd <= d_cut and not (
                entity["listing_status"] == "ACTIVE" and ld and sd < ld <= d_cut
            ):
                status_as_of = "SUSPENDED"
            elif entity["listing_status"] == "PROPOSED_DELISTING":
                status_as_of = "PROPOSED_DELISTING"
            else:
                status_as_of = "ACTIVE"

        if d_cut is not None:
            mkt_row = self._connection.execute(
                """
                SELECT * FROM daily_market_data
                WHERE (entity_id = ? OR (entity_id IS NULL AND ? IS NOT NULL AND nse_symbol = ?))
                  AND trade_date <= ?
                ORDER BY trade_date DESC, id DESC LIMIT 1
                """,
                (entity_id, nse_symbol, nse_symbol, d_cut),
            ).fetchone()
        else:
            mkt_row = self._connection.execute(
                """
                SELECT * FROM daily_market_data
                WHERE (entity_id = ? OR (entity_id IS NULL AND ? IS NOT NULL AND nse_symbol = ?))
                ORDER BY trade_date DESC, id DESC LIMIT 1
                """,
                (entity_id, nse_symbol, nse_symbol),
            ).fetchone()
        latest_market_snapshot = (
            {k: v for k, v in dict(mkt_row).items() if k != "raw_json"}
            if mkt_row is not None
            else None
        )

        if ts_cut is not None:
            fund_row = self._connection.execute(
                """
                SELECT * FROM quarterly_fundamentals
                WHERE (entity_id = ? OR (entity_id IS NULL AND ? IS NOT NULL AND nse_symbol = ?))
                  AND broadcast_timestamp <= ?
                ORDER BY broadcast_timestamp DESC, quarter_ending DESC, id DESC LIMIT 1
                """,
                (entity_id, nse_symbol, nse_symbol, ts_cut),
            ).fetchone()
        else:
            fund_row = self._connection.execute(
                """
                SELECT * FROM quarterly_fundamentals
                WHERE (entity_id = ? OR (entity_id IS NULL AND ? IS NOT NULL AND nse_symbol = ?))
                ORDER BY broadcast_timestamp DESC, quarter_ending DESC, id DESC LIMIT 1
                """,
                (entity_id, nse_symbol, nse_symbol),
            ).fetchone()
        latest_fundamentals = dict(fund_row) if fund_row is not None else None

        if ts_cut is not None:
            filings_rows = self._connection.execute(
                """
                SELECT * FROM qualitative_filings
                WHERE (entity_id = ? OR (entity_id IS NULL AND ? IS NOT NULL AND nse_symbol = ?))
                  AND broadcast_timestamp IS NOT NULL
                  AND broadcast_timestamp <= ?
                ORDER BY broadcast_timestamp, id
                """,
                (entity_id, nse_symbol, nse_symbol, ts_cut),
            ).fetchall()
        else:
            filings_rows = self._connection.execute(
                """
                SELECT * FROM qualitative_filings
                WHERE (entity_id = ? OR (entity_id IS NULL AND ? IS NOT NULL AND nse_symbol = ?))
                ORDER BY COALESCE(broadcast_timestamp, ''), id
                """,
                (entity_id, nse_symbol, nse_symbol),
            ).fetchall()
        qualitative_filings = [dict(r) for r in filings_rows]

        timeline: list[dict[str, Any]] = []
        if entity.get("listing_date") and (d_cut is None or entity["listing_date"] <= d_cut):
            timeline.append(
                {
                    "date": entity["listing_date"],
                    "event_type": "LISTING",
                    "summary": f"Listed on NSE ({entity.get('series') or 'EQ'})",
                    "source_url": entity.get("source_url"),
                    "local_artifact_path": None,
                }
            )
        if entity.get("suspension_date") and (
            d_cut is None or entity["suspension_date"] <= d_cut
        ):
            reason = entity.get("suspension_reason") or "Trading suspended"
            timeline.append(
                {
                    "date": entity["suspension_date"],
                    "event_type": "SUSPENSION",
                    "summary": reason,
                    "source_url": entity.get("source_url"),
                    "local_artifact_path": None,
                }
            )
        for filing in qualitative_filings:
            ts = filing.get("broadcast_timestamp")
            if not ts:
                continue
            headline = (
                filing.get("announcement_headline")
                or filing.get("announcement_category")
                or "Qualitative Filing"
            )
            timeline.append(
                {
                    "date": ts,
                    "event_type": filing.get("announcement_category") or "FILING",
                    "summary": headline,
                    "source_url": filing.get("source_url"),
                    "local_artifact_path": filing.get("local_artifact_path"),
                }
            )
        for ev in delisting_events:
            ev_dt = ev.get("effective_delisting_date") or ev.get("order_date")
            if not ev_dt:
                continue
            timeline.append(
                {
                    "date": ev_dt,
                    "event_type": f"DELISTING_{ev.get('delisting_type') or 'EVENT'}",
                    "summary": f"{ev.get('scope')}: {ev.get('delisting_type') or 'OTHER'}",
                    "source_url": ev.get("source_url"),
                    "local_artifact_path": ev.get("local_artifact_path"),
                }
            )
        timeline.sort(key=lambda item: (item["date"], item["event_type"]))

        return {
            "as_of_date": as_of_date,
            "status_as_of": status_as_of,
            "entity": entity,
            "latest_market_snapshot": latest_market_snapshot,
            "latest_fundamentals": latest_fundamentals,
            "delisting_events": delisting_events,
            "qualitative_filings": qualitative_filings,
            "timeline": timeline,
        }

    # ------------------------------------------------------------------
    # Typed quantitative, fundamental & qualitative tables
    # ------------------------------------------------------------------

    def _resolve_entity_id(
        self,
        isin: str | None,
        nse_symbol: str | None,
        observed_date: str | None = None,
    ) -> int | None:
        if isin:
            row = self._connection.execute(
                "SELECT id FROM nse_entities WHERE isin = ? LIMIT 1", (isin,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
        if nse_symbol:
            rows = self._connection.execute(
                "SELECT * FROM nse_entities WHERE nse_symbol = ? ORDER BY id",
                (nse_symbol,),
            ).fetchall()
            for row in rows:
                if isin and row["isin"] and row["isin"] != isin:
                    continue
                if (
                    isin
                    and observed_date
                    and row["listing_status"] in ("DELISTED", "DELISTED_PRE_2006")
                    and row["latest_delisting_date"]
                    and observed_date[:10] > row["latest_delisting_date"][:10]
                ):
                    continue
                return int(row["id"])
        return None

    def upsert_daily_market_row(self, record: Mapping[str, Any]) -> bool:
        """Insert or enrich a daily market row keyed by (nse_symbol, trade_date)."""
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        trade_date = _clean_str(record.get("trade_date"))
        if not nse_symbol or not trade_date:
            raise ValueError("Daily market row requires 'nse_symbol' and 'trade_date'.")

        isin = _clean_upper(record.get("isin"))
        shares_in = _to_int(record.get("shares_outstanding"))
        close_val = _to_float(record.get("close"))
        mcap_val = _to_float(record.get("market_cap_cr"))
        raw_json = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)

        with self._write_txn():
            if isin and nse_symbol:
                self.backfill_entity_isin(nse_symbol, isin, observed_date=trade_date)

            entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
                isin, nse_symbol, observed_date=trade_date
            )

            if shares_in is not None and shares_in > 0 and entity_id is not None:
                self._connection.execute(
                    """
                    UPDATE nse_entities
                    SET shares_outstanding = COALESCE(?, shares_outstanding),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (shares_in, entity_id),
                )

            if mcap_val is None and close_val is not None and close_val > 0:
                shares = shares_in
                if (shares is None or shares <= 0) and entity_id is not None:
                    ent_row = self._connection.execute(
                        "SELECT shares_outstanding FROM nse_entities WHERE id = ?",
                        (entity_id,),
                    ).fetchone()
                    if ent_row is not None:
                        shares = ent_row["shares_outstanding"]
                if shares is not None and shares > 0:
                    mcap_val = round(close_val * shares / 1e7, 4)

            existing = self._connection.execute(
                "SELECT id FROM daily_market_data WHERE nse_symbol = ? AND trade_date = ?",
                (nse_symbol, trade_date),
            ).fetchone()

            values = (
                entity_id,
                isin,
                nse_symbol,
                _clean_upper(record.get("series")),
                _clean_str(record.get("sector")),
                _clean_str(record.get("industry")),
                trade_date,
                _to_float(record.get("open")),
                _to_float(record.get("high")),
                _to_float(record.get("low")),
                close_val,
                _to_int(record.get("total_traded_qty")),
                _to_float(record.get("turnover_cr")),
                _to_int(record.get("deliverable_qty")),
                _to_float(record.get("delivery_pct")),
                _to_int(record.get("no_of_trades")),
                mcap_val,
                _to_float(record.get("free_float_pct")),
                _to_sqlite_bool(record.get("is_fno")),
                _to_sqlite_bool(record.get("bulk_block_deal_flag")),
                _to_float(record.get("futures_oi")),
                _to_float(record.get("oi_change_pct")),
                _to_float(record.get("pe_ratio")),
                _to_float(record.get("sector_median_pe")),
                _to_float(record.get("ev_ebitda")),
                _to_float(record.get("peg_ratio")),
                _to_float(record.get("z_factor_mcap")),
                _to_float(record.get("mcap_percentile")),
                _to_int(record.get("mcap_rank")),
                _clean_upper(record.get("size_bracket")),
                _to_float(record.get("deflated_mcap_cr")),
                _to_float(record.get("equivalent_mcap_cr")),
                _to_sqlite_bool(record.get("in_target_bracket")),
                _to_float(record.get("target_bracket_distance_z")),
                _to_float(record.get("implied_bracket_min_cr")),
                _to_float(record.get("implied_bracket_max_cr")),
                raw_json,
            )

            self._connection.execute(
                """
                INSERT INTO daily_market_data (
                    entity_id, isin, nse_symbol, series, sector, industry, trade_date,
                    open, high, low, close, total_traded_qty, turnover_cr,
                    deliverable_qty, delivery_pct, no_of_trades, market_cap_cr,
                    free_float_pct, is_fno, bulk_block_deal_flag, futures_oi,
                    oi_change_pct, pe_ratio, sector_median_pe, ev_ebitda, peg_ratio,
                    z_factor_mcap, mcap_percentile, mcap_rank, size_bracket,
                    deflated_mcap_cr, equivalent_mcap_cr, in_target_bracket,
                    target_bracket_distance_z, implied_bracket_min_cr,
                    implied_bracket_max_cr, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nse_symbol, trade_date) DO UPDATE SET
                    entity_id = COALESCE(excluded.entity_id, daily_market_data.entity_id),
                    isin = COALESCE(excluded.isin, daily_market_data.isin),
                    series = COALESCE(excluded.series, daily_market_data.series),
                    sector = COALESCE(excluded.sector, daily_market_data.sector),
                    industry = COALESCE(excluded.industry, daily_market_data.industry),
                    open = COALESCE(excluded.open, daily_market_data.open),
                    high = COALESCE(excluded.high, daily_market_data.high),
                    low = COALESCE(excluded.low, daily_market_data.low),
                    close = COALESCE(excluded.close, daily_market_data.close),
                    total_traded_qty = COALESCE(excluded.total_traded_qty, daily_market_data.total_traded_qty),
                    turnover_cr = COALESCE(excluded.turnover_cr, daily_market_data.turnover_cr),
                    deliverable_qty = COALESCE(excluded.deliverable_qty, daily_market_data.deliverable_qty),
                    delivery_pct = COALESCE(excluded.delivery_pct, daily_market_data.delivery_pct),
                    no_of_trades = COALESCE(excluded.no_of_trades, daily_market_data.no_of_trades),
                    market_cap_cr = COALESCE(excluded.market_cap_cr, daily_market_data.market_cap_cr),
                    free_float_pct = COALESCE(excluded.free_float_pct, daily_market_data.free_float_pct),
                    is_fno = COALESCE(excluded.is_fno, daily_market_data.is_fno),
                    bulk_block_deal_flag = COALESCE(excluded.bulk_block_deal_flag, daily_market_data.bulk_block_deal_flag),
                    futures_oi = COALESCE(excluded.futures_oi, daily_market_data.futures_oi),
                    oi_change_pct = COALESCE(excluded.oi_change_pct, daily_market_data.oi_change_pct),
                    pe_ratio = COALESCE(excluded.pe_ratio, daily_market_data.pe_ratio),
                    sector_median_pe = COALESCE(excluded.sector_median_pe, daily_market_data.sector_median_pe),
                    ev_ebitda = COALESCE(excluded.ev_ebitda, daily_market_data.ev_ebitda),
                    peg_ratio = COALESCE(excluded.peg_ratio, daily_market_data.peg_ratio),
                    z_factor_mcap = COALESCE(excluded.z_factor_mcap, daily_market_data.z_factor_mcap),
                    mcap_percentile = COALESCE(excluded.mcap_percentile, daily_market_data.mcap_percentile),
                    mcap_rank = COALESCE(excluded.mcap_rank, daily_market_data.mcap_rank),
                    size_bracket = COALESCE(excluded.size_bracket, daily_market_data.size_bracket),
                    deflated_mcap_cr = COALESCE(excluded.deflated_mcap_cr, daily_market_data.deflated_mcap_cr),
                    equivalent_mcap_cr = COALESCE(excluded.equivalent_mcap_cr, daily_market_data.equivalent_mcap_cr),
                    in_target_bracket = COALESCE(excluded.in_target_bracket, daily_market_data.in_target_bracket),
                    target_bracket_distance_z = COALESCE(excluded.target_bracket_distance_z, daily_market_data.target_bracket_distance_z),
                    implied_bracket_min_cr = COALESCE(excluded.implied_bracket_min_cr, daily_market_data.implied_bracket_min_cr),
                    implied_bracket_max_cr = COALESCE(excluded.implied_bracket_max_cr, daily_market_data.implied_bracket_max_cr),
                    raw_json = COALESCE(excluded.raw_json, daily_market_data.raw_json)
                """,
                values,
            )
        return existing is None

    def count_daily_market_rows(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS cnt FROM daily_market_data"
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def list_daily_market_rows(
        self, trade_date: str | None = None, nse_symbol: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_date is not None:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if nse_symbol is not None:
            clauses.append("nse_symbol = ?")
            params.append(nse_symbol.strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM daily_market_data {where} ORDER BY trade_date, nse_symbol, id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_daily_market_rows_for_z(
        self, pit_universe_only: bool = True
    ) -> list[dict[str, Any]]:
        """Fetch daily market rows enriched with entity series, optionally excluding post-delisting ghost rows."""
        rows = self._connection.execute(
            """
            SELECT
                d.*,
                COALESCE(d.series, e.series) AS effective_series,
                e.listing_status AS entity_listing_status,
                e.listing_date AS entity_listing_date,
                e.latest_delisting_date AS entity_delisting_date
            FROM daily_market_data d
            LEFT JOIN nse_entities e ON d.entity_id = e.id
            ORDER BY d.trade_date, d.nse_symbol, d.id
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if pit_universe_only and not _is_row_active_at_trade_date(item):
                continue
            item["series"] = item.get("effective_series") or item.get("series")
            out.append(item)
        return out

    def save_z_calibration(self, cal: Mapping[str, Any]) -> int:
        """Persist a target-bracket Z-factor calibration record."""
        with self._write_txn():
            cursor = self._connection.execute(
                """
                INSERT INTO z_factor_calibrations (
                    target_min_cr, target_max_cr, ref_log_center, ref_log_scale,
                    z_min, z_max, source, reference_start, reference_end,
                    reference_dates_count, robust, mainboard_only_stats,
                    trailing_days, z_buffer, value_field
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(cal["target_min_cr"]),
                    float(cal["target_max_cr"]),
                    float(cal["ref_log_center"]),
                    float(cal["ref_log_scale"]),
                    float(cal["z_min"]),
                    float(cal["z_max"]),
                    str(cal["source"]),
                    _clean_str(cal.get("reference_start")),
                    _clean_str(cal.get("reference_end")),
                    int(cal.get("reference_dates_count") or 0),
                    1 if cal.get("robust") else 0,
                    1 if cal.get("mainboard_only_stats", True) else 0,
                    int(cal.get("trailing_days") or 1),
                    float(cal.get("z_buffer") or 0.0),
                    _clean_str(cal.get("value_field")) or "market_cap_cr",
                ),
            )
        return int(cursor.lastrowid)

    def latest_z_calibration(self) -> dict[str, Any] | None:
        """Return the most recently persisted target-bracket Z-factor calibration, if any."""
        row = self._connection.execute(
            "SELECT * FROM z_factor_calibrations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["robust"] = bool(out["robust"])
        out["mainboard_only_stats"] = bool(out["mainboard_only_stats"])
        out["trailing_days"] = int(out.get("trailing_days") or 1)
        out["z_buffer"] = float(out.get("z_buffer") or 0.0)
        out["value_field"] = out.get("value_field") or "market_cap_cr"
        return out

    def list_target_bracket_stocks(
        self,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        survivorship_free: bool = True,
    ) -> list[dict[str, Any]]:
        """Query standardized target-bracket stocks with entity lifecycle context."""
        clauses: list[str] = ["d.in_target_bracket = 1"]
        params: list[Any] = []
        if trade_date is not None:
            clauses.append("d.trade_date = ?")
            params.append(trade_date)
        if start_date is not None:
            clauses.append("d.trade_date >= ?")
            params.append(start_date)
        if end_date is not None:
            clauses.append("d.trade_date <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}"
        rows = self._connection.execute(
            f"""
            SELECT
                d.*,
                e.company_name AS company_name,
                e.listing_status AS entity_listing_status,
                e.listing_date AS entity_listing_date,
                e.latest_delisting_date AS entity_delisting_date,
                e.latest_delisting_type AS latest_delisting_type
            FROM daily_market_data d
            LEFT JOIN nse_entities e ON d.entity_id = e.id
            {where}
            ORDER BY d.trade_date, d.mcap_rank, d.nse_symbol
            """,
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if survivorship_free and not _is_row_active_at_trade_date(item):
                continue
            item["listing_status"] = item.pop("entity_listing_status", None)
            item["listing_date"] = item.pop("entity_listing_date", None)
            item["latest_delisting_date"] = item.pop("entity_delisting_date", None)
            out.append(item)
        return out

    def upsert_quarterly_fundamental(self, record: Mapping[str, Any]) -> bool:
        """Insert or update point-in-time quarterly fundamentals."""
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        quarter_ending = _clean_str(record.get("quarter_ending"))
        broadcast_timestamp = _clean_str(record.get("broadcast_timestamp"))
        if not nse_symbol or not quarter_ending or not broadcast_timestamp:
            raise ValueError(
                "Quarterly fundamental requires 'nse_symbol', 'quarter_ending', and 'broadcast_timestamp'."
            )

        isin = _clean_upper(record.get("isin"))
        with self._write_txn():
            entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
                isin, nse_symbol, observed_date=(broadcast_timestamp or quarter_ending)
            )

            existing = self._connection.execute(
                """
                SELECT id FROM quarterly_fundamentals
                WHERE nse_symbol = ? AND quarter_ending = ? AND broadcast_timestamp = ?
                """,
                (nse_symbol, quarter_ending, broadcast_timestamp),
            ).fetchone()

            values = (
                entity_id,
                isin,
                nse_symbol,
                quarter_ending,
                broadcast_timestamp,
                _to_float(record.get("sales_growth_yoy")),
                _to_float(record.get("sales_growth_qoq")),
                _to_float(record.get("ebitda_margin_pct")),
                _to_float(record.get("pat_growth_yoy")),
                _to_float(record.get("eps_ttm")),
                _to_float(record.get("roce_pct")),
                _to_float(record.get("roe_pct")),
                _to_float(record.get("debt_to_equity")),
                _to_float(record.get("interest_coverage")),
                _to_float(record.get("cash_flow_from_ops")),
                _to_float(record.get("promoter_holding_pct")),
                _to_float(record.get("promoter_pledge_pct")),
                _to_float(record.get("fii_holding_chg_qoq")),
                _to_float(record.get("dii_holding_chg_qoq")),
                _to_float(record.get("retail_holding_chg_qoq")),
            )

            self._connection.execute(
                """
                INSERT INTO quarterly_fundamentals (
                    entity_id, isin, nse_symbol, quarter_ending, broadcast_timestamp,
                    sales_growth_yoy, sales_growth_qoq, ebitda_margin_pct,
                    pat_growth_yoy, eps_ttm, roce_pct, roe_pct, debt_to_equity,
                    interest_coverage, cash_flow_from_ops, promoter_holding_pct,
                    promoter_pledge_pct, fii_holding_chg_qoq, dii_holding_chg_qoq,
                    retail_holding_chg_qoq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nse_symbol, quarter_ending, broadcast_timestamp) DO UPDATE SET
                    entity_id = COALESCE(excluded.entity_id, quarterly_fundamentals.entity_id),
                    isin = COALESCE(excluded.isin, quarterly_fundamentals.isin),
                    sales_growth_yoy = COALESCE(excluded.sales_growth_yoy, quarterly_fundamentals.sales_growth_yoy),
                    sales_growth_qoq = COALESCE(excluded.sales_growth_qoq, quarterly_fundamentals.sales_growth_qoq),
                    ebitda_margin_pct = COALESCE(excluded.ebitda_margin_pct, quarterly_fundamentals.ebitda_margin_pct),
                    pat_growth_yoy = COALESCE(excluded.pat_growth_yoy, quarterly_fundamentals.pat_growth_yoy),
                    eps_ttm = COALESCE(excluded.eps_ttm, quarterly_fundamentals.eps_ttm),
                    roce_pct = COALESCE(excluded.roce_pct, quarterly_fundamentals.roce_pct),
                    roe_pct = COALESCE(excluded.roe_pct, quarterly_fundamentals.roe_pct),
                    debt_to_equity = COALESCE(excluded.debt_to_equity, quarterly_fundamentals.debt_to_equity),
                    interest_coverage = COALESCE(excluded.interest_coverage, quarterly_fundamentals.interest_coverage),
                    cash_flow_from_ops = COALESCE(excluded.cash_flow_from_ops, quarterly_fundamentals.cash_flow_from_ops),
                    promoter_holding_pct = COALESCE(excluded.promoter_holding_pct, quarterly_fundamentals.promoter_holding_pct),
                    promoter_pledge_pct = COALESCE(excluded.promoter_pledge_pct, quarterly_fundamentals.promoter_pledge_pct),
                    fii_holding_chg_qoq = COALESCE(excluded.fii_holding_chg_qoq, quarterly_fundamentals.fii_holding_chg_qoq),
                    dii_holding_chg_qoq = COALESCE(excluded.dii_holding_chg_qoq, quarterly_fundamentals.dii_holding_chg_qoq),
                    retail_holding_chg_qoq = COALESCE(excluded.retail_holding_chg_qoq, quarterly_fundamentals.retail_holding_chg_qoq)
                """,
                values,
            )
        return existing is None

    def list_fundamentals_as_of(
        self, nse_symbol: str, as_of_timestamp: str
    ) -> list[dict[str, Any]]:
        """Return fundamentals released on or before as_of_timestamp (zero lookahead)."""
        rows = self._connection.execute(
            """
            SELECT * FROM quarterly_fundamentals
            WHERE nse_symbol = ? AND broadcast_timestamp <= ?
            ORDER BY broadcast_timestamp, quarter_ending, id
            """,
            (nse_symbol.strip().upper(), as_of_timestamp),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_qualitative_filing(self, record: Mapping[str, Any]) -> bool:
        """Insert a qualitative filing deterministically by content hash."""
        isin = _clean_upper(record.get("isin"))
        nse_symbol = _clean_symbol(record.get("nse_symbol"))
        obs_date = _clean_str(record.get("broadcast_timestamp")) or _clean_str(
            record.get("trade_date")
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
            entity_id = _to_int(record.get("entity_id")) or self._resolve_entity_id(
                isin, nse_symbol, observed_date=obs_date
            )
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

    def ingest_unified_market_record(self, record: Mapping[str, Any]) -> dict[str, bool]:
        """Persist a canonical 50-column market record into typed tables atomically."""
        normalized = normalize_market_record(record)
        with self._write_txn():
            daily_upserted = False
            if _clean_symbol(normalized.get("nse_symbol")) and _clean_str(
                normalized.get("trade_date")
            ):
                daily_upserted = self.upsert_daily_market_row(normalized)

            fundamental_upserted = False
            bcast_ts = _clean_str(normalized.get("broadcast_timestamp")) or _clean_str(
                normalized.get("trade_date")
            )
            if (
                _clean_symbol(normalized.get("nse_symbol"))
                and _clean_str(normalized.get("quarter_ending"))
                and bcast_ts
            ):
                fundamental_payload = dict(normalized)
                fundamental_payload["broadcast_timestamp"] = bcast_ts
                fundamental_upserted = self.upsert_quarterly_fundamental(
                    fundamental_payload
                )

            qualitative_fields = (
                "surveillance_stage",
                "announcement_category",
                "announcement_headline",
                "announcement_body_md",
                "concall_commentary_md",
                "concall_guidance_md",
                "sast_insider_activity",
                "bulk_block_deal_participants",
            )
            qualitative_inserted = False
            if any(normalized.get(field) is not None for field in qualitative_fields):
                qualitative_inserted = self.upsert_qualitative_filing(normalized)

        return {
            "daily_upserted": daily_upserted,
            "fundamental_upserted": fundamental_upserted,
            "qualitative_inserted": qualitative_inserted,
        }

    # ------------------------------------------------------------------
    # Internal matchers & schema initialization
    # ------------------------------------------------------------------

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

            CREATE TABLE IF NOT EXISTS daily_market_data (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER REFERENCES nse_entities(id),
                isin TEXT,
                nse_symbol TEXT NOT NULL,
                series TEXT,
                sector TEXT,
                industry TEXT,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                total_traded_qty INTEGER,
                turnover_cr REAL,
                deliverable_qty INTEGER,
                delivery_pct REAL,
                no_of_trades INTEGER,
                market_cap_cr REAL,
                free_float_pct REAL,
                is_fno INTEGER,
                bulk_block_deal_flag INTEGER,
                futures_oi REAL,
                oi_change_pct REAL,
                pe_ratio REAL,
                sector_median_pe REAL,
                ev_ebitda REAL,
                peg_ratio REAL,
                z_factor_mcap REAL,
                mcap_percentile REAL,
                mcap_rank INTEGER,
                size_bracket TEXT,
                deflated_mcap_cr REAL,
                equivalent_mcap_cr REAL,
                in_target_bracket INTEGER,
                target_bracket_distance_z REAL,
                implied_bracket_min_cr REAL,
                implied_bracket_max_cr REAL,
                raw_json TEXT,
                UNIQUE(nse_symbol, trade_date)
            );
            CREATE INDEX IF NOT EXISTS daily_market_trade_date_idx
                ON daily_market_data(trade_date);
            CREATE INDEX IF NOT EXISTS daily_market_isin_date_idx
                ON daily_market_data(isin, trade_date);
            CREATE INDEX IF NOT EXISTS daily_market_entity_date_idx
                ON daily_market_data(entity_id, trade_date);

            CREATE TABLE IF NOT EXISTS z_factor_calibrations (
                id INTEGER PRIMARY KEY,
                target_min_cr REAL NOT NULL,
                target_max_cr REAL NOT NULL,
                ref_log_center REAL NOT NULL,
                ref_log_scale REAL NOT NULL,
                z_min REAL NOT NULL,
                z_max REAL NOT NULL,
                source TEXT NOT NULL,
                reference_start TEXT,
                reference_end TEXT,
                reference_dates_count INTEGER NOT NULL DEFAULT 0,
                robust INTEGER NOT NULL DEFAULT 0,
                mainboard_only_stats INTEGER NOT NULL DEFAULT 1,
                trailing_days INTEGER NOT NULL DEFAULT 1,
                z_buffer REAL NOT NULL DEFAULT 0.0,
                value_field TEXT NOT NULL DEFAULT 'market_cap_cr',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quarterly_fundamentals (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER REFERENCES nse_entities(id),
                isin TEXT,
                nse_symbol TEXT NOT NULL,
                quarter_ending TEXT NOT NULL,
                broadcast_timestamp TEXT NOT NULL,
                sales_growth_yoy REAL,
                sales_growth_qoq REAL,
                ebitda_margin_pct REAL,
                pat_growth_yoy REAL,
                eps_ttm REAL,
                roce_pct REAL,
                roe_pct REAL,
                debt_to_equity REAL,
                interest_coverage REAL,
                cash_flow_from_ops REAL,
                promoter_holding_pct REAL,
                promoter_pledge_pct REAL,
                fii_holding_chg_qoq REAL,
                dii_holding_chg_qoq REAL,
                retail_holding_chg_qoq REAL,
                UNIQUE(nse_symbol, quarter_ending, broadcast_timestamp)
            );
            CREATE INDEX IF NOT EXISTS quarterly_fundamentals_sym_ts_idx
                ON quarterly_fundamentals(nse_symbol, broadcast_timestamp);

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
                if stmt.strip():
                    self._connection.execute(stmt)

    def _migrate_schema(self) -> None:
        new_entity_cols = (
            ("suspension_date", "TEXT"),
            ("suspension_reason", "TEXT"),
            ("surveillance_stage", "TEXT"),
            ("shares_outstanding", "INTEGER"),
        )
        new_cols = (
            ("mcap_rank", "INTEGER"),
            ("equivalent_mcap_cr", "REAL"),
            ("in_target_bracket", "INTEGER"),
            ("target_bracket_distance_z", "REAL"),
            ("implied_bracket_min_cr", "REAL"),
            ("implied_bracket_max_cr", "REAL"),
        )
        new_cal_cols = (
            ("trailing_days", "INTEGER NOT NULL DEFAULT 1"),
            ("z_buffer", "REAL NOT NULL DEFAULT 0.0"),
            ("value_field", "TEXT NOT NULL DEFAULT 'market_cap_cr'"),
        )
        with self._write_txn():
            entity_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(nse_entities)"
                ).fetchall()
            }
            existing_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(daily_market_data)"
                ).fetchall()
            }
            cal_cols = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(z_factor_calibrations)"
                ).fetchall()
            }
            for col_name, col_type in new_entity_cols:
                if col_name not in entity_cols:
                    self._connection.execute(
                        f"ALTER TABLE nse_entities ADD COLUMN {col_name} {col_type}"
                    )
            for col_name, col_type in new_cols:
                if col_name not in existing_cols:
                    self._connection.execute(
                        f"ALTER TABLE daily_market_data ADD COLUMN {col_name} {col_type}"
                    )
            for col_name, col_type in new_cal_cols:
                if col_name not in cal_cols:
                    self._connection.execute(
                        f"ALTER TABLE z_factor_calibrations ADD COLUMN {col_name} {col_type}"
                    )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS daily_market_target_bracket_idx
                    ON daily_market_data(trade_date, in_target_bracket)
                """
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


def _is_valid_equity_isin(code: str) -> bool:
    if not re.fullmatch(r"IN[E9][A-Z0-9]{8}[0-9]", code or ""):
        return False
    if code[7:9] in {"07", "08", "14", "16"}:
        return False
    digits = "".join(str(int(c, 36)) for c in code)
    total = 0
    for idx, digit in enumerate(reversed(digits)):
        num = int(digit) * (2 if idx % 2 else 1)
        total += num - 9 if num > 9 else num
    return total % 10 == 0


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


def _is_row_active_at_trade_date(row: Mapping[str, Any]) -> bool:
    """Exclude post-delisting ghost prints and delisted gaps on re-listed stocks."""
    if row.get("entity_id") is None:
        return True
    trade_date = row.get("trade_date")
    status = row.get("entity_listing_status")
    listing_date = row.get("entity_listing_date")
    delisting_date = row.get("entity_delisting_date")
    if not trade_date:
        return True

    if status in ("DELISTED", "DELISTED_PRE_2006"):
        if delisting_date and trade_date >= delisting_date:
            return False
        if listing_date and trade_date < listing_date:
            return False
        return True

    if status == "ACTIVE":
        if delisting_date and listing_date and delisting_date < listing_date:
            if delisting_date <= trade_date < listing_date:
                return False
        elif listing_date and trade_date < listing_date:
            return False
        return True

    return True
