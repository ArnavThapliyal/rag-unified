"""
quant-sync and quant-zfactor.

quant-sync
  1. Universe: every entity and delisting event of the latest collector snapshot (UNIVERSE_DB, installed by
     pipeline/batches.py) is upserted into QUANT_DB by its natural keys (ISIN, symbol, normalized name; record_id),
     so the quant DB's own ids stay stable while the collector rebuilds its DB. Skipped when the snapshot is the one
     already synced.
  2. Market rows: LanceDB `documents` rows that carry a trade_date or a quarter_ending (record-mode tabular chunks
     with typed columns) and were ingested after the last sync's watermark are routed into daily_market_data /
     quarterly_fundamentals / qualitative_filings by QuantStore.ingest_unified_market_record.

quant-zfactor
  recompute_store_z_factors over QUANT_DB (value field QUANT_VALUE_FIELD, default market_cap_cr), then the rows that
  got a Z-factor are written to the LanceDB table QUANT_TABLE (overwrite: it is derived data). With no usable
  rows the step is a noop with reason no_price_rows / no_<field>_values.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import config.settings as settings
from pipeline.batches import read_universe_meta
from pipeline.quant.store import QuantStore
from pipeline.quant.z_factor import recompute_store_z_factors
from pipeline.schema import EXTENDED_FIELDS
from pipeline.stepkit import now_iso, result

FACTOR_COLUMNS = ("isin", "nse_symbol", "series", "trade_date", "market_cap_cr", "z_factor_mcap", "mcap_percentile",
                  "mcap_rank", "size_bracket", "deflated_mcap_cr", "equivalent_mcap_cr", "in_target_bracket",
                  "target_bracket_distance_z", "implied_bracket_min_cr", "implied_bracket_max_cr")


def state_path(quant_db: Optional[Path] = None) -> Path:
    return Path(quant_db or settings.QUANT_DB).with_suffix(".state.json")


def read_state(quant_db: Optional[Path] = None) -> Dict[str, Any]:
    try:
        return json.loads(state_path(quant_db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: Dict[str, Any], quant_db: Optional[Path] = None) -> None:
    target = state_path(quant_db)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def sync_universe(store: QuantStore, universe_db: Path) -> Dict[str, int]:
    src = sqlite3.connect(f"file:{Path(universe_db).resolve()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        entities = [dict(r) for r in src.execute("SELECT * FROM nse_entities ORDER BY id")]
        events = [dict(r) for r in src.execute("SELECT * FROM delisting_events ORDER BY id")]
    finally:
        src.close()
    id_map: Dict[int, int] = {}
    for e in entities:
        snap_id = e.pop("id")
        for k in ("normalized_name", "created_at", "updated_at"):
            e.pop(k, None)
        id_map[snap_id], _ = store.upsert_nse_entity(e)
    n_events = 0
    for ev in events:
        entity = id_map.get(ev.pop("entity_id"))
        ev.pop("id", None)
        if entity is not None:
            store.upsert_delisting_event(entity, ev)
            n_events += 1
    return {"universe_entities": len(entities), "universe_events": n_events}


def table_names(db: Any) -> list:
    """LanceDB table names across versions (list_tables() returns a paged response in 0.34+)."""
    listed = db.list_tables()
    names = getattr(listed, "tables", listed)
    return list(names or [])


def _lance_table(lancedb_path: Path, table: str):
    import lancedb  # heavy import only when the step runs

    db = lancedb.connect(str(lancedb_path))
    return db, (db.open_table(table) if table in table_names(db) else None)


def sync_market_rows(store: QuantStore, lancedb_path: Path, table: str, watermark: Optional[str]) -> Dict[str, Any]:
    _, tbl = _lance_table(lancedb_path, table)
    if tbl is None:
        return {"market_rows_read": 0, "daily_inserted": 0, "fundamentals_inserted": 0, "watermark": watermark}
    where = "(trade_date IS NOT NULL OR quarter_ending IS NOT NULL)"
    if watermark:
        # >= : every row of one indexer run shares its run's ingested_at, and a run may still be writing when we
        # read; re-reading the boundary run is harmless (upserts are keyed by symbol + date / quarter)
        where += f" AND ingested_at >= '{watermark.replace(chr(39), '')}'"
    cols = ["symbol", "ingested_at", *EXTENDED_FIELDS]
    rows = tbl.search().where(where).select(cols).limit(None).to_arrow().to_pylist()
    counts = {"market_rows_read": len(rows), "daily_inserted": 0, "fundamentals_inserted": 0, "skipped_no_symbol": 0}
    newest = watermark
    for r in rows:
        ingested = r.pop("ingested_at")
        symbol = r.pop("symbol")
        if newest is None or (ingested and ingested > newest):
            newest = ingested
        if not r.get("nse_symbol"):
            r["nse_symbol"] = symbol
        if not r.get("nse_symbol"):
            counts["skipped_no_symbol"] += 1
            continue
        res = store.ingest_unified_market_record({k: v for k, v in r.items() if k in EXTENDED_FIELDS})
        counts["daily_inserted"] += bool(res.get("daily_upserted"))
        counts["fundamentals_inserted"] += bool(res.get("fundamental_upserted"))
    counts["watermark"] = newest
    return counts


def quant_sync(universe_db: Optional[Path] = None, quant_db: Optional[Path] = None,
               lancedb_path: Optional[Path] = None, table: Optional[str] = None, full: bool = False) -> Dict[str, Any]:
    universe_db = Path(universe_db or settings.UNIVERSE_DB)
    quant_db = Path(quant_db or settings.QUANT_DB)
    lancedb_path = Path(lancedb_path or settings.LANCEDB_PATH)
    table = table or settings.LANCEDB_TABLE
    state = {} if full else read_state(quant_db)
    uni_meta = read_universe_meta(universe_db)
    counts: Dict[str, Any] = {}
    Path(quant_db).parent.mkdir(parents=True, exist_ok=True)
    with QuantStore(quant_db) as store:
        if Path(universe_db).exists() and uni_meta.get("batch_id") != state.get("universe_batch_id"):
            counts.update(sync_universe(store, universe_db))
            state["universe_batch_id"] = uni_meta.get("batch_id")
        m = sync_market_rows(store, lancedb_path, table, state.get("lance_watermark"))
        state["lance_watermark"] = m.pop("watermark")
        counts.update(m)
    state["synced_at"] = now_iso()
    write_state(state, quant_db)
    # rows re-read at the watermark boundary upsert again; only new ones count as work
    did = counts.get("universe_entities") or counts.get("daily_inserted") or counts.get("fundamentals_inserted")
    nxt = ["quant-zfactor"] if counts.get("daily_inserted") or counts.get("universe_entities") else []
    if not Path(universe_db).exists():
        reason = "no universe snapshot yet (ingest a collector batch first); market rows synced without it"
    else:
        reason = "" if did else "nothing new since the last sync"
    return result("quant-sync", "ok" if did else "noop", reason, counts=counts, next_suggested=nxt,
                  universe_batch_id=state.get("universe_batch_id"), quant_db=str(quant_db))


def quant_zfactor(quant_db: Optional[Path] = None, lancedb_path: Optional[Path] = None,
                  value_field: Optional[str] = None, write_lance: bool = True, **kwargs: Any) -> Dict[str, Any]:
    quant_db = Path(quant_db or settings.QUANT_DB)
    lancedb_path = Path(lancedb_path or settings.LANCEDB_PATH)
    value_field = value_field or settings.QUANT_VALUE_FIELD
    if not Path(quant_db).exists():
        return result("quant-zfactor", "blocked", "no quant DB yet: run quant-sync first", next_suggested=["quant-sync"])
    with QuantStore(quant_db) as store:
        rows = store.list_daily_market_rows_for_z(pit_universe_only=False)
        if not rows:
            return result("quant-zfactor", "noop", "no_price_rows", counts={"daily_rows": 0})
        usable = sum(1 for r in rows if r.get(value_field) not in (None, ""))
        if not usable:
            return result("quant-zfactor", "noop", f"no_{value_field}_values",
                          counts={"daily_rows": len(rows), "usable_rows": 0})
        out = recompute_store_z_factors(store, value_field=value_field, **kwargs)
        factors = [r for r in store.list_daily_market_rows() if r.get("z_factor_mcap") is not None]
    counts = {"daily_rows": len(rows), "usable_rows": usable, "rows_updated": out.get("rows_updated", 0),
              "dates_processed": out.get("dates_processed", 0),
              "target_bracket_matches": out.get("target_bracket_matches", 0), "factor_rows": len(factors)}
    if write_lance and factors:
        import lancedb
        import pyarrow as pa

        data = pa.Table.from_pylist([{c: f.get(c) for c in FACTOR_COLUMNS} for f in factors])
        lancedb.connect(str(lancedb_path)).create_table(settings.QUANT_TABLE, data=data, mode="overwrite")
        counts["lance_rows"] = len(factors)
    return result("quant-zfactor", "ok" if counts["rows_updated"] else "noop",
                  "" if counts["rows_updated"] else "no rows in the point-in-time universe",
                  counts=counts, calibration=out.get("calibration"), anchor_date=out.get("anchor_date_used"))


def calibration_from_row(row: Dict[str, Any]):
    """A saved z_factor_calibrations row -> TargetBracketCalibration (from Unified_Pipeline's cli.py)."""
    from pipeline.quant.z_factor import TargetBracketCalibration

    return TargetBracketCalibration(
        target_min_cr=float(row["target_min_cr"]), target_max_cr=float(row["target_max_cr"]),
        ref_log_center=float(row["ref_log_center"]), ref_log_scale=float(row["ref_log_scale"]),
        z_min=float(row["z_min"]), z_max=float(row["z_max"]), source=str(row["source"]),
        reference_start=row.get("reference_start"), reference_end=row.get("reference_end"),
        reference_dates_count=int(row.get("reference_dates_count") or 0), robust=bool(row.get("robust")),
        mainboard_only_stats=bool(row.get("mainboard_only_stats", True)),
        trailing_days=int(row.get("trailing_days") or 1),
        z_buffer=float(row.get("z_buffer") or 0.0),
        value_field=str(row.get("value_field") or "market_cap_cr"))


def quant_bracket(quant_db: Optional[Path] = None, schedule: bool = False, date: Optional[str] = None,
                  start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """The target bracket over time (schedule: equivalent rupee bounds per date) or its constituents
    (survivorship-free stocks inside the bracket on a date / range). Read-only."""
    from pipeline.quant.z_factor import TargetBracketCalibration, summarize_historical_brackets

    quant_db = Path(quant_db or settings.QUANT_DB)
    if not quant_db.exists():
        return result("quant-bracket", "blocked", "no quant DB yet: run quant-sync first", next_suggested=["quant-sync"])
    with QuantStore(quant_db) as store:
        saved = store.latest_z_calibration()
        cal = calibration_from_row(saved) if saved is not None else TargetBracketCalibration.default_nse()
        if schedule:
            rows = summarize_historical_brackets(
                store.list_daily_market_rows_for_z(pit_universe_only=True),
                value_field=cal.value_field,
                robust=cal.robust,
                target_bracket_cr=(cal.target_min_cr, cal.target_max_cr),
                calibration=cal,
                mainboard_only_stats=cal.mainboard_only_stats,
                trailing_days=cal.trailing_days,
                z_buffer=cal.z_buffer)
            rows = [r for r in rows if (date is None or r["trade_date"] == date)
                    and (start_date is None or r["trade_date"] >= start_date)
                    and (end_date is None or r["trade_date"] <= end_date)]
            return result("quant-bracket", "ok" if rows else "noop", "" if rows else "no dates with Z-factors",
                          counts={"dates": len(rows)}, calibration=cal.to_dict(), schedule=rows[: max(0, limit)])
        matches = store.list_target_bracket_stocks(trade_date=date, start_date=start_date, end_date=end_date,
                                                   survivorship_free=True)
        sample = [{k: m.get(k) for k in ("trade_date", "nse_symbol", "isin", "company_name", "listing_status",
                                         "latest_delisting_date", "market_cap_cr", "z_factor_mcap", "mcap_percentile",
                                         "mcap_rank", "equivalent_mcap_cr", "implied_bracket_min_cr",
                                         "implied_bracket_max_cr")} for m in matches[: max(0, limit)]]
        return result("quant-bracket", "ok" if matches else "noop", "" if matches else "no stocks in the bracket",
                      counts={"matches": len(matches)}, calibration=saved or cal.to_dict(), sample=sample)
