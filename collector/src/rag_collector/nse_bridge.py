"""Sync NSE active listings, delisting archives, and leads into SQLite."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from rag_collector.universe import UniverseStore

_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y")


def _parse_date(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _read_clean_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            cleaned: dict[str, str] = {}
            for key, val in raw_row.items():
                if key is None:
                    continue
                clean_key = key.strip()
                if not clean_key:
                    continue
                cleaned[clean_key] = (val or "").strip()
            rows.append(cleaned)
    return rows


def sync_archive_to_store(
    repo_root: str | Path,
    store: UniverseStore,
    include_bse_only: bool = False,
) -> dict[str, int]:
    """Import NSE active listings, delistings, pre-2006 exits, and leads into SQLite."""
    root = Path(repo_root)
    counts = {
        "active_equity_rows": 0,
        "active_sme_rows": 0,
        "delisted_2006_2026_rows": 0,
        "delisted_pre_2006_rows": 0,
        "proposed_lead_rows": 0,
        "suspended_rows": 0,
        "surveillance_rows": 0,
        "bse_only_rows": 0,
    }

    with store._write_txn():
        # 1. Active mainboard NSE equity listings
        equity_rows = _read_clean_csv(root / "data" / "reference" / "nse_equity_listed.csv")
        for row in equity_rows:
            company_name = row.get("NAME OF COMPANY", "")
            if not company_name:
                continue
            store.upsert_nse_entity(
                {
                    "isin": row.get("ISIN NUMBER"),
                    "nse_symbol": row.get("SYMBOL"),
                    "company_name": company_name,
                    "series": row.get("SERIES"),
                    "listing_date": _parse_date(row.get("DATE OF LISTING")),
                    "listing_status": "ACTIVE",
                    "paid_up_value": _parse_float(row.get("PAID UP VALUE")),
                    "face_value": _parse_float(row.get("FACE VALUE")),
                }
            )
            counts["active_equity_rows"] += 1

        # 2. Active NSE SME listings
        sme_rows = _read_clean_csv(root / "data" / "reference" / "nse_sme_listed.csv")
        for row in sme_rows:
            company_name = row.get("NAME_OF_COMPANY", "")
            if not company_name:
                continue
            store.upsert_nse_entity(
                {
                    "isin": row.get("ISIN_NUMBER"),
                    "nse_symbol": row.get("SYMBOL"),
                    "company_name": company_name,
                    "series": row.get("SERIES"),
                    "listing_date": _parse_date(row.get("DATE_OF_LISTING")),
                    "listing_status": "ACTIVE",
                    "paid_up_value": _parse_float(row.get("PAID_UP_VALUE")),
                    "face_value": _parse_float(row.get("FACE_VALUE")),
                }
            )
            counts["active_sme_rows"] += 1

        # 3. Verified NSE delistings (2006-2026)
        delisted_path = root / "data" / "processed" / "nse_delisted_master.csv"
        delisted_rows = _read_clean_csv(delisted_path)
        seen_master_ids: set[str] = set()
        for row in delisted_rows:
            rid = _ingest_delisting_row(
                store, row, listing_status="DELISTED", scope="NSE_2006_2026"
            )
            if rid:
                seen_master_ids.add(rid)
            counts["delisted_2006_2026_rows"] += 1
        if delisted_path.exists():
            store.prune_delisting_events("NSE_2006_2026", seen_master_ids)

        # 4. Pre-2006 out-of-scope NSE delistings
        pre_2006_path = root / "data" / "processed" / "out_of_scope_delistings.csv"
        pre_2006_rows = _read_clean_csv(pre_2006_path)
        seen_pre_ids: set[str] = set()
        for row in pre_2006_rows:
            rid = _ingest_delisting_row(
                store, row, listing_status="DELISTED_PRE_2006", scope="PRE_2006"
            )
            if rid:
                seen_pre_ids.add(rid)
            counts["delisted_pre_2006_rows"] += 1
        if pre_2006_path.exists():
            store.prune_delisting_events("PRE_2006", seen_pre_ids)

        # 5. Proposed delisting leads
        lead_path = root / "data" / "processed" / "proposed_delisting_leads.csv"
        lead_rows = _read_clean_csv(lead_path)
        seen_lead_ids: set[str] = set()
        for row in lead_rows:
            company_name = row.get("company_name", "")
            if not company_name:
                continue
            nse_symbol = row.get("nse_symbol", "")
            isin = row.get("isin", "")
            source_url = row.get("source_url", "")
            local_artifact_path = row.get("local_artifact_path", "")

            entity_id, _ = store.upsert_nse_entity(
                {
                    "isin": isin or None,
                    "nse_symbol": nse_symbol or None,
                    "company_name": company_name,
                    "listing_status": "PROPOSED_DELISTING",
                    "source_url": source_url or None,
                }
            )
            lead_digest = hashlib.sha256(
                f"{company_name}|{nse_symbol}|{source_url}".encode("utf-8")
            ).hexdigest()[:12]
            lead_rid = f"lead-{lead_digest}"
            seen_lead_ids.add(lead_rid)
            store.upsert_delisting_event(
                entity_id,
                {
                    "record_id": lead_rid,
                    "scope": "PROPOSED_LEAD",
                    "company_name": company_name,
                    "nse_symbol": nse_symbol or None,
                    "isin": isin or None,
                    "source_url": source_url or None,
                    "local_artifact_path": local_artifact_path or None,
                    "confidence_status": "VERIFIED_PRIMARY",
                },
            )
            store.upsert_qualitative_filing(
                {
                    "entity_id": entity_id,
                    "isin": isin or None,
                    "nse_symbol": nse_symbol or None,
                    "surveillance_stage": "PROPOSED_DELISTING",
                    "announcement_category": "PROPOSED_DELISTING",
                    "announcement_headline": f"{company_name} (PROPOSED_DELISTING)",
                    "source_url": source_url or None,
                    "local_artifact_path": local_artifact_path or None,
                    "confidence_status": "VERIFIED_PRIMARY",
                }
            )
            counts["proposed_lead_rows"] += 1
        if lead_path.exists():
            store.prune_delisting_events("PROPOSED_LEAD", seen_lead_ids)

        # 6. Suspended & ASM/GSM surveillance reference files (if present)
        susp_path = root / "data" / "reference" / "nse_suspended_equities.csv"
        if susp_path.exists():
            susp_res = ingest_suspended_and_surveillance_csv(susp_path, store)
            counts["suspended_rows"] += susp_res["suspended_rows"]
            counts["surveillance_rows"] += susp_res["surveillance_rows"]

        surv_path = root / "data" / "reference" / "nse_surveillance_asm_gsm.csv"
        if surv_path.exists():
            surv_res = ingest_suspended_and_surveillance_csv(surv_path, store)
            counts["suspended_rows"] += surv_res["suspended_rows"]
            counts["surveillance_rows"] += surv_res["surveillance_rows"]

        # 7. Optional BSE-only delistings
        if include_bse_only:
            bse_path = root / "data" / "processed" / "bse_only_delistings.csv"
            bse_rows = _read_clean_csv(bse_path)
            seen_bse_ids: set[str] = set()
            for row in bse_rows:
                rid = _ingest_delisting_row(
                    store, row, listing_status="BSE_ONLY", scope="BSE_ONLY"
                )
                if rid:
                    seen_bse_ids.add(rid)
                counts["bse_only_rows"] += 1
            if bse_path.exists():
                store.prune_delisting_events("BSE_ONLY", seen_bse_ids)

        entities_total = store.count_nse_entities(include_bse_only=include_bse_only)
        delisting_events_total = store.count_delisting_events()
        qualitative_filings_total = store.count_qualitative_filings()

    return {
        **counts,
        "entities_total": entities_total,
        "delisting_events_total": delisting_events_total,
        "qualitative_filings_total": qualitative_filings_total,
    }


def ingest_suspended_and_surveillance_csv(
    csv_path: str | Path,
    store: UniverseStore,
) -> dict[str, int]:
    """Ingest NSE/BSE suspended equities or ASM/GSM surveillance CSVs into UniverseStore."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Suspended/surveillance CSV not found: {path}")

    raw_rows = _read_clean_csv(path)
    suspended_rows = 0
    surveillance_rows = 0

    for raw in raw_rows:
        norm = {
            k.strip().lower().replace(" ", "_").replace(".", "").replace("-", "_"): v
            for k, v in raw.items()
        }
        symbol = _first_non_empty(
            norm, ("nse_symbol", "symbol", "scrip", "scrip_code", "trading_symbol")
        )
        company_name = _first_non_empty(
            norm,
            (
                "company_name",
                "name_of_company",
                "company",
                "name",
                "security_name",
                "issuer_name",
            ),
        ) or symbol
        if not company_name:
            continue

        isin = _first_non_empty(norm, ("isin", "isin_number", "isin_code"))
        suspension_date = _parse_date(
            _first_non_empty(
                norm,
                (
                    "suspension_date",
                    "date_of_suspension",
                    "suspended_w_e_f",
                    "suspended_wef",
                    "wef",
                ),
            )
        )
        reason = _first_non_empty(
            norm, ("reason", "suspension_reason", "remarks", "category")
        )
        stage = _first_non_empty(
            norm, ("surveillance_stage", "stage", "gsm_stage", "asm_stage", "status")
        )
        event_date = (
            _parse_date(
                _first_non_empty(
                    norm, ("effective_date", "date", "broadcast_date", "circular_date")
                )
            )
            or suspension_date
        )
        source_url = _first_non_empty(norm, ("source_url", "url"))

        is_suspension = bool(
            suspension_date
            or (stage and "suspend" in stage.lower())
            or (reason and "suspend" in reason.lower())
            or ("suspend" in path.name.lower() and "surveillance" not in path.name.lower())
        )

        if is_suspension:
            listing_status = "SUSPENDED"
            eff_susp_date = suspension_date or event_date
            eff_stage = stage or "SUSPENDED"
            category = "SUSPENSION"
            headline = f"{company_name} ({reason or 'Trading Suspended'})"
            suspended_rows += 1
        else:
            listing_status = "ACTIVE"
            eff_susp_date = None
            eff_stage = stage or "SURVEILLANCE"
            category = "SURVEILLANCE"
            headline = f"{company_name} ({eff_stage})"
            surveillance_rows += 1

        entity_id, _ = store.upsert_nse_entity(
            {
                "isin": isin,
                "nse_symbol": symbol,
                "company_name": company_name,
                "listing_status": listing_status,
                "suspension_date": eff_susp_date,
                "suspension_reason": reason,
                "surveillance_stage": eff_stage,
                "source_url": source_url,
            }
        )
        store.upsert_qualitative_filing(
            {
                "entity_id": entity_id,
                "isin": isin,
                "nse_symbol": symbol,
                "broadcast_timestamp": event_date,
                "surveillance_stage": eff_stage,
                "announcement_category": category,
                "announcement_headline": headline,
                "announcement_body_md": reason,
                "source_url": source_url,
                "local_artifact_path": str(path),
                "confidence_status": "VERIFIED_PRIMARY",
            }
        )

    return {
        "suspended_rows": suspended_rows,
        "surveillance_rows": surveillance_rows,
    }


def _first_non_empty(row: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = (row.get(key) or "").strip()
        if val:
            return val
    return None


def _ingest_delisting_row(
    store: UniverseStore,
    row: Mapping[str, str],
    *,
    listing_status: str,
    scope: str,
) -> str | None:
    company_name = row.get("company_name", "")
    record_id = row.get("record_id", "")
    if not company_name or not record_id:
        return None

    order_date = _parse_date(row.get("order_date"))
    effective_date = _parse_date(row.get("effective_delisting_date"))
    delisting_type = (row.get("delisting_type") or "").strip().upper() or "OTHER"
    isin = row.get("isin") or None
    nse_symbol = row.get("nse_symbol") or None
    source_url = row.get("source_url") or None
    local_artifact_path = row.get("local_artifact_path") or None
    confidence_status = row.get("confidence_status") or None

    entity_id, _ = store.upsert_nse_entity(
        {
            "isin": isin,
            "nse_symbol": nse_symbol,
            "company_name": company_name,
            "listing_status": listing_status,
            "latest_delisting_date": effective_date or order_date,
            "latest_delisting_type": delisting_type,
            "source_url": source_url,
        }
    )
    store.upsert_delisting_event(
        entity_id,
        {
            "record_id": record_id,
            "scope": scope,
            "company_name": company_name,
            "nse_symbol": nse_symbol,
            "isin": isin,
            "delisting_type": delisting_type,
            "order_date": order_date,
            "effective_delisting_date": effective_date,
            "calendar_year": _parse_int(row.get("calendar_year")),
            "financial_year": row.get("financial_year") or None,
            "is_priority_year": _parse_bool(row.get("is_priority_year")),
            "regulatory_regime": row.get("regulatory_regime") or None,
            "exit_or_fair_value_inr": _parse_float(row.get("exit_or_fair_value_inr")),
            "source_url": source_url,
            "local_artifact_path": local_artifact_path,
            "confidence_status": confidence_status,
            "source_count": _parse_int(row.get("source_count")),
            "all_source_urls": row.get("all_source_urls") or None,
            "all_artifact_paths": row.get("all_artifact_paths") or None,
            "source_conflicts": row.get("source_conflicts") or None,
            "review_flags": row.get("review_flags") or None,
            "date_source": row.get("date_source") or None,
        },
    )
    store.upsert_qualitative_filing(
        {
            "entity_id": entity_id,
            "isin": isin,
            "nse_symbol": nse_symbol,
            "broadcast_timestamp": order_date or effective_date,
            "announcement_category": f"DELISTING_{delisting_type}",
            "announcement_headline": f"{company_name} ({delisting_type})",
            "source_url": source_url,
            "local_artifact_path": local_artifact_path,
            "confidence_status": confidence_status,
        }
    )
    return record_id
