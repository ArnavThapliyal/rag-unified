"""Quantitative NSE Bhavcopy (CM/UDiFF/Delivery/F&O) and broker feed ingestors."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
import csv
from datetime import datetime
import io
import json
from pathlib import Path
from typing import Any
import zipfile

from pipeline.quant.store import QuantStore, normalize_market_record
from pipeline.quant.z_factor import recompute_store_z_factors
from pipeline.schema import EXTENDED_FIELDS as MARKET_DATA_COLUMNS

EQUITY_SERIES: frozenset[str] = frozenset(
    {"EQ", "BE", "BZ", "BL", "BT", "SM", "ST", "SZ", ""}
)

_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")
_QUANT_EXTENSIONS = frozenset({".csv", ".json", ".jsonl", ".zip"})
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB zip-bomb safety cap

_ALIAS_MAP: dict[str, str] = {
    "isin": "isin",
    "isin_number": "isin",
    "isin_code": "isin",
    "nse_symbol": "nse_symbol",
    "symbol": "nse_symbol",
    "ticker": "nse_symbol",
    "tradingsymbol": "nse_symbol",
    "trading_symbol": "nse_symbol",
    "tckrsymb": "nse_symbol",
    "scrip_symbol": "nse_symbol",
    "series": "series",
    "sctysrs": "series",
    "sector": "sector",
    "industry": "industry",
    "market_cap_cr": "market_cap_cr",
    "mcap_cr": "market_cap_cr",
    "mktcap_cr": "market_cap_cr",
    "market_cap": "market_cap_cr",
    "shares_outstanding": "_shares_outstanding",
    "listed_shares": "_shares_outstanding",
    "total_shares": "_shares_outstanding",
    "free_float_pct": "free_float_pct",
    "free_float": "free_float_pct",
    "is_fno": "is_fno",
    "trade_date": "trade_date",
    "date": "trade_date",
    "traddt": "trade_date",
    "timestamp": "trade_date",
    "open": "open",
    "open_price": "open",
    "opnpric": "open",
    "high": "high",
    "high_price": "high",
    "hghpric": "high",
    "low": "low",
    "low_price": "low",
    "lwpric": "low",
    "close": "close",
    "close_price": "close",
    "clspric": "close",
    "ltp": "close",
    "last_price": "close",
    "total_traded_qty": "total_traded_qty",
    "volume": "total_traded_qty",
    "tottrdqty": "total_traded_qty",
    "ttl_trd_qnty": "total_traded_qty",
    "ttltradgvol": "total_traded_qty",
    "turnover_cr": "turnover_cr",
    "deliverable_qty": "deliverable_qty",
    "deliv_qty": "deliverable_qty",
    "delivery_pct": "delivery_pct",
    "deliv_per": "delivery_pct",
    "no_of_trades": "no_of_trades",
    "totaltrades": "no_of_trades",
    "bulk_block_deal_flag": "bulk_block_deal_flag",
    "futures_oi": "futures_oi",
    "open_int": "futures_oi",
    "oi_change_pct": "oi_change_pct",
    "quarter_ending": "quarter_ending",
    "sales_growth_yoy": "sales_growth_yoy",
    "sales_growth_qoq": "sales_growth_qoq",
    "ebitda_margin_pct": "ebitda_margin_pct",
    "pat_growth_yoy": "pat_growth_yoy",
    "eps_ttm": "eps_ttm",
    "eps": "eps_ttm",
    "roce_pct": "roce_pct",
    "roce": "roce_pct",
    "roe_pct": "roe_pct",
    "roe": "roe_pct",
    "debt_to_equity": "debt_to_equity",
    "interest_coverage": "interest_coverage",
    "cash_flow_from_ops": "cash_flow_from_ops",
    "pe_ratio": "pe_ratio",
    "pe": "pe_ratio",
    "p_e": "pe_ratio",
    "sector_median_pe": "sector_median_pe",
    "ev_ebitda": "ev_ebitda",
    "peg_ratio": "peg_ratio",
    "promoter_holding_pct": "promoter_holding_pct",
    "promoter_pledge_pct": "promoter_pledge_pct",
    "fii_holding_chg_qoq": "fii_holding_chg_qoq",
    "dii_holding_chg_qoq": "dii_holding_chg_qoq",
    "retail_holding_chg_qoq": "retail_holding_chg_qoq",
    "surveillance_stage": "surveillance_stage",
    "broadcast_timestamp": "broadcast_timestamp",
    "filing_date": "broadcast_timestamp",
    "result_date": "broadcast_timestamp",
    "announcement_category": "announcement_category",
    "announcement_headline": "announcement_headline",
    "announcement_body_md": "announcement_body_md",
    "concall_commentary_md": "concall_commentary_md",
    "concall_guidance_md": "concall_guidance_md",
    "sast_insider_activity": "sast_insider_activity",
    "bulk_block_deal_participants": "bulk_block_deal_participants",
}

_INT_FIELDS = frozenset(
    {"total_traded_qty", "deliverable_qty", "no_of_trades", "_shares_outstanding"}
)
_BOOL_FIELDS = frozenset({"is_fno", "bulk_block_deal_flag"})
_FLOAT_FIELDS = frozenset(
    {
        "market_cap_cr",
        "free_float_pct",
        "open",
        "high",
        "low",
        "close",
        "turnover_cr",
        "delivery_pct",
        "futures_oi",
        "oi_change_pct",
        "sales_growth_yoy",
        "sales_growth_qoq",
        "ebitda_margin_pct",
        "pat_growth_yoy",
        "eps_ttm",
        "roce_pct",
        "roe_pct",
        "debt_to_equity",
        "interest_coverage",
        "cash_flow_from_ops",
        "pe_ratio",
        "sector_median_pe",
        "ev_ebitda",
        "peg_ratio",
        "promoter_holding_pct",
        "promoter_pledge_pct",
        "fii_holding_chg_qoq",
        "dii_holding_chg_qoq",
        "retail_holding_chg_qoq",
    }
)


def parse_quantitative_file(path: str | Path) -> list[dict[str, Any]]:
    """Parse NSE Bhavcopy (CM/UDiFF/Delivery/F&O) or broker CSV/JSON/ZIP into normalized rows."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Quantitative input not found: {p}")
    suffix = p.suffix.lower()

    if suffix == ".zip":
        rows: list[dict[str, Any]] = []
        total_uncompressed = 0
        with zipfile.ZipFile(p, "r") as zf:
            for member in sorted(zf.namelist()):
                if member.endswith("/") or "__MACOSX" in member:
                    continue
                ext = Path(member).suffix.lower()
                if ext not in (".csv", ".json", ".jsonl"):
                    continue
                info = zf.getinfo(member)
                total_uncompressed += info.file_size
                if info.file_size > MAX_ZIP_UNCOMPRESSED_BYTES or total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"ZIP archive {p.name} exceeds {MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)} MB uncompressed limit"
                    )
                with zf.open(info, "r") as member_fp:
                    raw_bytes = member_fp.read(MAX_ZIP_UNCOMPRESSED_BYTES + 1)
                if len(raw_bytes) > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"ZIP member {member} exceeds {MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)} MB uncompressed limit"
                    )
                raw_text = raw_bytes.decode("utf-8-sig", errors="replace")
                rows.extend(_parse_text_payload(raw_text, ext))
        return rows

    if suffix not in (".csv", ".json", ".jsonl"):
        raise ValueError(f"Unsupported quantitative file extension: {p.suffix}")

    text = p.read_text(encoding="utf-8-sig", errors="replace")
    return _parse_text_payload(text, suffix)


def _parse_text_payload(text: str, ext: str) -> list[dict[str, Any]]:
    if ext == ".json":
        payload = json.loads(text)
        items = payload if isinstance(payload, list) else payload.get("records") or payload.get("data") or []
        if not isinstance(items, list):
            raise ValueError("JSON quantitative payload must be an array of objects.")
        return [_normalize_generic_mapping(item) for item in items if isinstance(item, Mapping)]

    if ext == ".jsonl":
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            item = json.loads(stripped)
            if isinstance(item, Mapping):
                out.append(_normalize_generic_mapping(item))
        return out

    # CSV parsing
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    raw_rows: list[dict[str, str]] = []
    for row in reader:
        cleaned = {
            (k.strip() if k is not None else ""): (v.strip() if v is not None else "")
            for k, v in row.items()
            if k is not None and k.strip()
        }
        if cleaned:
            raw_rows.append(cleaned)
    if not raw_rows:
        return []

    headers_upper = {k.upper() for k in raw_rows[0]}

    # 1. NSE UDiFF CM Bhavcopy
    if {
        "TRADDT",
        "TCKRSYMB",
        "OPNPRIC",
        "HGHPRIC",
        "LWPRIC",
        "CLSPRIC",
        "TTLTRADGVOL",
        "TTLTRFVAL",
    } <= headers_upper:
        return _parse_udiff_cm(raw_rows)

    # 2. NSE F&O Bhavcopy (Legacy or UDiFF)
    if {
        "INSTRUMENT",
        "SYMBOL",
        "OPEN_INT",
        "CHG_IN_OI",
        "TIMESTAMP",
    } <= headers_upper or {
        "FININSTRMTP",
        "TCKRSYMB",
        "OPNINTRST",
        "CHNGINOPNINTRST",
        "TRADDT",
    } <= headers_upper:
        return _parse_fno_bhavcopy(raw_rows, headers_upper)

    # 3. NSE Full Delivery Bhavdata
    if {
        "SYMBOL",
        "SERIES",
        "DATE1",
        "OPEN_PRICE",
        "HIGH_PRICE",
        "LOW_PRICE",
        "CLOSE_PRICE",
        "TTL_TRD_QNTY",
        "DELIV_QTY",
        "DELIV_PER",
    } <= headers_upper:
        return _parse_delivery_bhavdata(raw_rows)

    # 4. NSE Legacy CM Bhavcopy
    if {
        "SYMBOL",
        "SERIES",
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "TOTTRDQTY",
        "TOTTRDVAL",
        "TIMESTAMP",
    } <= headers_upper:
        return _parse_legacy_cm(raw_rows)

    # 5. Generic broker/vendor CSV
    return [_normalize_generic_mapping(r) for r in raw_rows]


def _parse_legacy_cm(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        up = {k.upper(): v for k, v in raw.items()}
        series = up.get("SERIES", "").strip().upper()
        if series not in EQUITY_SERIES:
            continue
        sym = _clean_sym(up.get("SYMBOL"))
        td = _parse_date(up.get("TIMESTAMP"))
        if not sym or not td:
            continue
        tottrdval = _to_float(up.get("TOTTRDVAL"))
        row: dict[str, Any] = {
            "nse_symbol": sym,
            "series": series or None,
            "trade_date": td,
            "open": _to_float(up.get("OPEN")),
            "high": _to_float(up.get("HIGH")),
            "low": _to_float(up.get("LOW")),
            "close": _to_float(up.get("CLOSE")),
            "total_traded_qty": _to_int(up.get("TOTTRDQTY")),
            "turnover_cr": round(tottrdval / 1e7, 6) if tottrdval is not None else None,
            "no_of_trades": _to_int(up.get("TOTALTRADES")),
            "isin": _clean_upper(up.get("ISIN")),
        }
        out.append(_drop_none_keys(row))
    return out


def _parse_udiff_cm(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        up = {k.upper(): v for k, v in raw.items()}
        inst_tp = up.get("FININSTRMTP", "").strip().upper()
        if inst_tp and inst_tp != "STK":
            continue
        series = up.get("SCTYSRS", "").strip().upper()
        if series not in EQUITY_SERIES:
            continue
        sym = _clean_sym(up.get("TCKRSYMB"))
        td = _parse_date(up.get("TRADDT"))
        if not sym or not td:
            continue
        ttltrfval = _to_float(up.get("TTLTRFVAL"))
        row: dict[str, Any] = {
            "nse_symbol": sym,
            "series": series or None,
            "trade_date": td,
            "open": _to_float(up.get("OPNPRIC")),
            "high": _to_float(up.get("HGHPRIC")),
            "low": _to_float(up.get("LWPRIC")),
            "close": _to_float(up.get("CLSPRIC")),
            "total_traded_qty": _to_int(up.get("TTLTRADGVOL")),
            "turnover_cr": round(ttltrfval / 1e7, 6) if ttltrfval is not None else None,
            "no_of_trades": _to_int(up.get("TTLNBOFTXSEXCTD")),
            "isin": _clean_upper(up.get("ISIN")),
        }
        out.append(_drop_none_keys(row))
    return out


def _parse_delivery_bhavdata(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        up = {k.upper(): v for k, v in raw.items()}
        series = up.get("SERIES", "").strip().upper()
        if series not in EQUITY_SERIES:
            continue
        sym = _clean_sym(up.get("SYMBOL"))
        td = _parse_date(up.get("DATE1"))
        if not sym or not td:
            continue
        turnover_lacs = _to_float(up.get("TURNOVER_LACS"))
        row: dict[str, Any] = {
            "nse_symbol": sym,
            "series": series or None,
            "trade_date": td,
            "open": _to_float(up.get("OPEN_PRICE")),
            "high": _to_float(up.get("HIGH_PRICE")),
            "low": _to_float(up.get("LOW_PRICE")),
            "close": _to_float(up.get("CLOSE_PRICE")),
            "total_traded_qty": _to_int(up.get("TTL_TRD_QNTY")),
            "turnover_cr": round(turnover_lacs / 100.0, 6) if turnover_lacs is not None else None,
            "no_of_trades": _to_int(up.get("NO_OF_TRADES")),
            "deliverable_qty": _to_int(up.get("DELIV_QTY")),
            "delivery_pct": _to_float(up.get("DELIV_PER")),
            "isin": _clean_upper(up.get("ISIN")),
        }
        out.append(_drop_none_keys(row))
    return out


def _parse_fno_bhavcopy(
    raw_rows: list[dict[str, str]], headers_upper: set[str]
) -> list[dict[str, Any]]:
    is_udiff = "FININSTRMTP" in headers_upper
    agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])

    for raw in raw_rows:
        up = {k.upper(): v for k, v in raw.items()}
        if is_udiff:
            if up.get("FININSTRMTP", "").strip().upper() != "STF":
                continue
            sym = _clean_sym(up.get("TCKRSYMB"))
            td = _parse_date(up.get("TRADDT"))
            oi = _to_float(up.get("OPNINTRST"))
            chg = _to_float(up.get("CHNGINOPNINTRST"))
        else:
            if up.get("INSTRUMENT", "").strip().upper() != "FUTSTK":
                continue
            sym = _clean_sym(up.get("SYMBOL"))
            td = _parse_date(up.get("TIMESTAMP"))
            oi = _to_float(up.get("OPEN_INT"))
            chg = _to_float(up.get("CHG_IN_OI"))

        if not sym or not td or oi is None:
            continue
        agg[(sym, td)][0] += oi
        agg[(sym, td)][1] += chg or 0.0

    out: list[dict[str, Any]] = []
    for (sym, td) in sorted(agg):
        total_oi, total_chg = agg[(sym, td)]
        prev_oi = total_oi - total_chg
        oi_chg_pct = round(100.0 * total_chg / prev_oi, 4) if prev_oi > 0 else None
        row: dict[str, Any] = {
            "nse_symbol": sym,
            "trade_date": td,
            "is_fno": True,
            "futures_oi": round(total_oi, 4),
            "oi_change_pct": oi_chg_pct,
        }
        out.append(_drop_none_keys(row))
    return out


def _normalize_generic_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for k, val in raw.items():
        norm_key = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        target = _ALIAS_MAP.get(norm_key)
        if target is None:
            continue
        if target == "trade_date":
            parsed_d = _parse_date(val)
            if parsed_d is not None:
                mapped[target] = parsed_d
        elif target == "quarter_ending":
            parsed_d = _parse_date(val)
            if parsed_d is not None:
                mapped[target] = parsed_d
        elif target == "nse_symbol":
            sym = _clean_sym(val)
            if sym is not None:
                mapped[target] = sym
        elif target in ("isin", "series"):
            cleaned = _clean_upper(val)
            if cleaned is not None:
                mapped[target] = cleaned
        elif target in _INT_FIELDS:
            num = _to_int(val)
            if num is not None:
                mapped[target] = num
        elif target in _FLOAT_FIELDS:
            num = _to_float(val)
            if num is not None:
                mapped[target] = num
        elif target in _BOOL_FIELDS:
            b = _to_bool(val)
            if b is not None:
                mapped[target] = b
        else:
            s = _clean_str(val)
            if s is not None:
                mapped[target] = s

    if (
        mapped.get("market_cap_cr") is None
        and mapped.get("close") is not None
        and mapped["close"] > 0
        and mapped.get("_shares_outstanding") is not None
        and mapped["_shares_outstanding"] > 0
    ):
        mapped["market_cap_cr"] = round(
            mapped["close"] * mapped["_shares_outstanding"] / 1e7, 4
        )
    return mapped


class QuantitativeFileMarketClient:
    """Concrete MarketDataApiClient reading NSE Bhavcopy & broker CSV/JSON/ZIP files."""

    def __init__(self, paths: Sequence[str | Path]) -> None:
        self.paths = [Path(p) for p in paths]

    def fetch_market_records(self) -> Iterable[Mapping[str, Any]]:
        merged_rows, _ = _collect_and_merge_paths(self.paths)
        for row in merged_rows:
            clean = {k: v for k, v in row.items() if k in MARKET_DATA_COLUMNS}
            yield normalize_market_record(clean)


def ingest_quantitative_paths(
    paths: Sequence[str | Path],
    store: QuantStore,
    recompute_z: bool = True,
    **z_kwargs: Any,
) -> dict[str, Any]:
    """Ingest NSE Bhavcopy & broker files into QuantStore with ISIN backfill and Z-factors."""
    merged_rows, file_count = _collect_and_merge_paths([Path(p) for p in paths])

    parsed_count = len(merged_rows)
    daily_inserted = 0
    daily_enriched = 0
    fundamentals_upserted = 0
    qualitative_inserted = 0
    isins_backfilled = 0

    for row in merged_rows:
        sym = row.get("nse_symbol")
        isin = row.get("isin")
        shares_out = row.get("_shares_outstanding")
        obs_date = row.get("trade_date") or row.get("broadcast_timestamp") or row.get("quarter_ending")

        if sym and isin:
            if store.backfill_entity_isin(sym, isin, observed_date=obs_date):
                isins_backfilled += 1

        if shares_out is not None and shares_out > 0 and sym:
            entity_id = store._resolve_entity_id(isin, sym, observed_date=obs_date)
            if entity_id is not None:
                with store._write_txn():
                    store._connection.execute(
                        """
                        UPDATE nse_entities
                        SET shares_outstanding = COALESCE(?, shares_outstanding),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (shares_out, entity_id),
                    )

        if (
            row.get("market_cap_cr") is None
            and row.get("close") is not None
            and row["close"] > 0
            and shares_out is not None
            and shares_out > 0
        ):
            row["market_cap_cr"] = round(row["close"] * shares_out / 1e7, 4)

        clean_payload = {k: v for k, v in row.items() if k in MARKET_DATA_COLUMNS}
        if not clean_payload:
            continue

        res = store.ingest_unified_market_record(clean_payload)
        if clean_payload.get("nse_symbol") and clean_payload.get("trade_date"):
            if res["daily_upserted"]:
                daily_inserted += 1
            else:
                daily_enriched += 1
        if res["fundamental_upserted"]:
            fundamentals_upserted += 1
        if res["qualitative_inserted"]:
            qualitative_inserted += 1

    z_summary: dict[str, Any] | None = None
    if recompute_z:
        z_summary = recompute_store_z_factors(store, **z_kwargs)

    return {
        "files_processed": file_count,
        "records_parsed": parsed_count,
        "daily_rows_inserted": daily_inserted,
        "daily_rows_enriched": daily_enriched,
        "fundamentals_upserted": fundamentals_upserted,
        "qualitative_inserted": qualitative_inserted,
        "isins_backfilled": isins_backfilled,
        "z_factor_summary": z_summary,
    }


def _collect_and_merge_paths(paths: Sequence[Path]) -> tuple[list[dict[str, Any]], int]:
    files: list[Path] = []
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(f"Quantitative input not found: {p}")
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in _QUANT_EXTENSIONS:
                    files.append(child)
        elif p.is_file():
            files.append(p)

    daily_merged: dict[tuple[str, str], dict[str, Any]] = {}
    other_rows: list[dict[str, Any]] = []

    for f in files:
        for row in parse_quantitative_file(f):
            sym = row.get("nse_symbol")
            td = row.get("trade_date")
            if sym and td:
                key = (sym, td)
                if key not in daily_merged:
                    daily_merged[key] = dict(row)
                else:
                    target = daily_merged[key]
                    for rk, rv in row.items():
                        if rv is not None and (target.get(rk) is None or rk in ("is_fno", "futures_oi", "oi_change_pct")):
                            target[rk] = rv
            elif row:
                other_rows.append(row)

    ordered_daily = [daily_merged[k] for k in sorted(daily_merged)]
    return ordered_daily + other_rows, len(files)


def _clean_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s in {"-", "NA", "N/A", "null", "None", "nan", "NaN"}:
        return None
    return s


def _clean_upper(val: Any) -> str | None:
    s = _clean_str(val)
    return s.upper() if s else None


def _clean_sym(val: Any) -> str | None:
    s = _clean_upper(val)
    if not s or s == "UNKNOWN":
        return None
    return s


def _parse_date(val: Any) -> str | None:
    s = _clean_str(val)
    if not s:
        return None
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        try:
            datetime.strptime(s[:10], "%Y-%m-%d")
            return s[:10]
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _to_float(val: Any) -> float | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = _clean_str(val)
    if not s:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(val: Any) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = _clean_str(val)
    if not s:
        return None
    s = s.replace(",", "")
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_bool(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = _clean_str(val)
    if not s:
        return None
    low = s.lower()
    if low in {"true", "1", "yes", "y"}:
        return True
    if low in {"false", "0", "no", "n"}:
        return False
    return None


def _drop_none_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if v is not None}
