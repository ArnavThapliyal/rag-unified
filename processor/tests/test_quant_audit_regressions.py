"""Regression tests for hardened QuantStore, Z-factor, sync, quantitative, and qualitative modules."""

from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import zipfile

from pipeline.quant import quantitative
from pipeline.quant.qualitative import (
    DeterministicFilingExtractor,
    extract_document_markdown,
    ingest_qualitative_paths,
    render_company_status_markdown,
)
from pipeline.quant.quantitative import (
    MAX_ZIP_UNCOMPRESSED_BYTES,
    ingest_quantitative_paths,
    parse_quantitative_file,
)
from pipeline.quant.store import QuantStore
from pipeline.quant.sync import calibration_from_row
from pipeline.quant.z_factor import (
    TargetBracketCalibration,
    _safe_exp,
    compute_cross_sectional_z_factors,
    recompute_store_z_factors,
)


class QuantStoreHardeningTests(unittest.TestCase):
    def test_wal_mode_and_nested_savepoint_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "quant.sqlite3"
            with QuantStore(db_path) as store:
                journal_mode = store._connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
                self.assertEqual(str(journal_mode).lower(), "wal")

                # Outer transaction commits; failing inner savepoint rolls back only inner work.
                with store.transaction():
                    store.upsert_nse_entity(
                        {
                            "isin": "INE001A01036",
                            "nse_symbol": "ALPHA",
                            "company_name": "Alpha Industries Limited",
                            "listing_status": "ACTIVE",
                        }
                    )
                    with self.assertRaises(RuntimeError):
                        with store.transaction():
                            store.upsert_nse_entity(
                                {
                                    "isin": "INE002A01018",
                                    "nse_symbol": "BETA",
                                    "company_name": "Beta Industries Limited",
                                    "listing_status": "ACTIVE",
                                }
                            )
                            raise RuntimeError("abort inner savepoint")

                self.assertIsNotNone(store.get_nse_entity(isin="INE001A01036"))
                self.assertIsNone(store.get_nse_entity(isin="INE002A01018"))

                # Failing outer transaction rolls back completely.
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.upsert_nse_entity(
                            {
                                "isin": "INE009A01021",
                                "nse_symbol": "GAMMA",
                                "company_name": "Gamma Tech Limited",
                                "listing_status": "ACTIVE",
                            }
                        )
                        raise RuntimeError("abort outer transaction")

                self.assertIsNone(store.get_nse_entity(isin="INE009A01021"))

    def test_isin_unique_index_and_entity_metadata_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                idx_row = store._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='nse_entities_isin_unique'"
                ).fetchone()
                self.assertIsNotNone(idx_row)

                store.upsert_nse_entity(
                    {
                        "isin": "INE001A01036",
                        "nse_symbol": "SUSPCO",
                        "company_name": "Suspended Corp Limited",
                        "listing_status": "SUSPENDED",
                        "suspension_date": "2023-04-10",
                        "suspension_reason": "Procedural non-compliance",
                        "surveillance_stage": "ASM STAGE IV",
                        "shares_outstanding": 12_500_000,
                    }
                )
                ent = store.get_nse_entity(isin="INE001A01036")
                self.assertIsNotNone(ent)
                assert ent is not None
                self.assertEqual(ent["suspension_date"], "2023-04-10")
                self.assertEqual(ent["suspension_reason"], "Procedural non-compliance")
                self.assertEqual(ent["surveillance_stage"], "ASM STAGE IV")
                self.assertEqual(ent["shares_outstanding"], 12_500_000)

                # Direct duplicate ISIN insert must be rejected by nse_entities_isin_unique.
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction():
                        store._connection.execute(
                            """
                            INSERT INTO nse_entities (isin, nse_symbol, company_name, normalized_name, listing_status)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            ("INE001A01036", "DUPCO", "Dup Corp", "DUP", "ACTIVE"),
                        )

                # Multiple ISIN-less entities remain permitted.
                id1, ins1 = store.upsert_nse_entity(
                    {"nse_symbol": "NOISIN1", "company_name": "No ISIN One Ltd", "listing_status": "DELISTED"}
                )
                id2, ins2 = store.upsert_nse_entity(
                    {"nse_symbol": "NOISIN2", "company_name": "No ISIN Two Ltd", "listing_status": "DELISTED"}
                )
                self.assertTrue(ins1 and ins2)
                self.assertNotEqual(id1, id2)


class PointInTimeHardeningTests(unittest.TestCase):
    def test_proposed_delisting_preserved_when_active_arrives_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                store.upsert_nse_entity(
                    {
                        "isin": "INE002A01018",
                        "nse_symbol": "EXITCO",
                        "company_name": "Exit Corp Limited",
                        "listing_status": "PROPOSED_DELISTING",
                        "listing_date": "2010-01-01",
                    }
                )
                store.upsert_nse_entity(
                    {
                        "isin": "INE002A01018",
                        "nse_symbol": "EXITCO",
                        "company_name": "Exit Corp Limited",
                        "listing_status": "ACTIVE",
                        "listing_date": "2010-01-01",
                    }
                )
                ent = store.get_nse_entity(isin="INE002A01018")
                self.assertIsNotNone(ent)
                assert ent is not None
                self.assertEqual(ent["listing_status"], "PROPOSED_DELISTING")

    def test_pre_listing_ghost_row_excluded_from_pit_z_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                store.upsert_nse_entity(
                    {
                        "isin": "INE009A01021",
                        "nse_symbol": "NEWIPO",
                        "company_name": "New IPO Limited",
                        "listing_status": "ACTIVE",
                        "listing_date": "2020-06-01",
                        "series": "EQ",
                    }
                )
                store.upsert_daily_market_row(
                    {
                        "isin": "INE009A01021",
                        "nse_symbol": "NEWIPO",
                        "trade_date": "2020-05-01",
                        "close": 100.0,
                        "market_cap_cr": 1000.0,
                    }
                )
                store.upsert_daily_market_row(
                    {
                        "isin": "INE009A01021",
                        "nse_symbol": "NEWIPO",
                        "trade_date": "2020-06-15",
                        "close": 120.0,
                        "market_cap_cr": 1200.0,
                    }
                )

                pit_rows = store.list_daily_market_rows_for_z(pit_universe_only=True)
                self.assertEqual([r["trade_date"] for r in pit_rows], ["2020-06-15"])

                all_rows = store.list_daily_market_rows_for_z(pit_universe_only=False)
                self.assertEqual(
                    [r["trade_date"] for r in all_rows], ["2020-05-01", "2020-06-15"]
                )

    def test_recycled_symbol_isolation_and_zero_lookahead_broadcast_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                old_id, _ = store.upsert_nse_entity(
                    {
                        "isin": "INE001A01036",
                        "nse_symbol": "RECYC",
                        "company_name": "Old Recycled Limited",
                        "listing_status": "DELISTED",
                        "listing_date": "2008-01-01",
                        "latest_delisting_date": "2015-05-01",
                    }
                )
                new_id, _ = store.upsert_nse_entity(
                    {
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "company_name": "New Recycled Limited",
                        "listing_status": "ACTIVE",
                        "listing_date": "2020-01-10",
                    }
                )
                self.assertNotEqual(old_id, new_id)

                store.upsert_daily_market_row(
                    {
                        "entity_id": old_id,
                        "isin": "INE001A01036",
                        "nse_symbol": "RECYC",
                        "trade_date": "2014-06-01",
                        "close": 50.0,
                        "market_cap_cr": 250.0,
                    }
                )
                store.upsert_daily_market_row(
                    {
                        "entity_id": new_id,
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "trade_date": "2022-05-05",
                        "close": 500.0,
                        "market_cap_cr": 5000.0,
                    }
                )

                # Fundamentals and filings for old vs new entity
                store.upsert_quarterly_fundamental(
                    {
                        "entity_id": old_id,
                        "isin": "INE001A01036",
                        "nse_symbol": "RECYC",
                        "quarter_ending": "2014-03-31",
                        "broadcast_timestamp": "2014-05-15T17:00:00",
                        "eps_ttm": 4.5,
                    }
                )
                store.upsert_quarterly_fundamental(
                    {
                        "entity_id": new_id,
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "quarter_ending": "2022-03-31",
                        "broadcast_timestamp": "2022-05-10T18:00:00",
                        "eps_ttm": 25.0,
                    }
                )
                store.upsert_quarterly_fundamental(
                    {
                        "entity_id": new_id,
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "quarter_ending": "2022-06-30",
                        "broadcast_timestamp": "2022-08-15T18:00:00",
                        "eps_ttm": 30.0,
                    }
                )

                store.upsert_qualitative_filing(
                    {
                        "entity_id": old_id,
                        "isin": "INE001A01036",
                        "nse_symbol": "RECYC",
                        "broadcast_timestamp": "2014-05-15T17:00:00",
                        "announcement_category": "FINANCIAL_RESULTS",
                        "announcement_headline": "Old Co FY14 Results",
                    }
                )
                store.upsert_qualitative_filing(
                    {
                        "entity_id": new_id,
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "broadcast_timestamp": "2022-05-10T18:00:00",
                        "announcement_category": "FINANCIAL_RESULTS",
                        "announcement_headline": "New Co Q4 FY22 Results",
                    }
                )
                store.upsert_qualitative_filing(
                    {
                        "entity_id": new_id,
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "broadcast_timestamp": "2022-08-15T18:00:00",
                        "announcement_category": "CONCALL_TRANSCRIPT",
                        "announcement_headline": "New Co Q1 FY23 Concall",
                    }
                )
                # Undated filing must not leak into as_of_date queries
                store.upsert_qualitative_filing(
                    {
                        "entity_id": new_id,
                        "isin": "INE002A01018",
                        "nse_symbol": "RECYC",
                        "broadcast_timestamp": None,
                        "announcement_category": "CORPORATE_ANNOUNCEMENT",
                        "announcement_headline": "Undated Notice",
                    }
                )

                old_profile = store.get_company_profile_and_timeline("INE001A01036")
                self.assertIsNotNone(old_profile)
                assert old_profile is not None
                self.assertEqual(old_profile["latest_market_snapshot"]["close"], 50.0)
                self.assertEqual(old_profile["latest_fundamentals"]["eps_ttm"], 4.5)
                self.assertEqual(
                    [f["announcement_headline"] for f in old_profile["qualitative_filings"]],
                    ["Old Co FY14 Results"],
                )

                new_june = store.get_company_profile_and_timeline(
                    "INE002A01018", as_of_date="2022-06-01"
                )
                self.assertIsNotNone(new_june)
                assert new_june is not None
                self.assertEqual(new_june["latest_fundamentals"]["eps_ttm"], 25.0)
                self.assertEqual(
                    [f["announcement_headline"] for f in new_june["qualitative_filings"]],
                    ["New Co Q4 FY22 Results"],
                )

                new_sept = store.get_company_profile_and_timeline(
                    "INE002A01018", as_of_date="2022-09-01"
                )
                self.assertIsNotNone(new_sept)
                assert new_sept is not None
                self.assertEqual(new_sept["latest_fundamentals"]["eps_ttm"], 30.0)
                self.assertEqual(
                    [f["announcement_headline"] for f in new_sept["qualitative_filings"]],
                    ["New Co Q4 FY22 Results", "New Co Q1 FY23 Concall"],
                )


class BackfillEntityIsinTests(unittest.TestCase):
    def test_backfill_attaches_valid_isin_and_rejects_post_delisting_and_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                ent_id, _ = store.upsert_nse_entity(
                    {
                        "nse_symbol": "NOISIN",
                        "company_name": "Missing ISIN Limited",
                        "listing_status": "DELISTED",
                        "latest_delisting_date": "2018-03-01",
                    }
                )
                store.upsert_delisting_event(
                    ent_id,
                    {
                        "record_id": "DEL-NOISIN-1",
                        "scope": "NSE_2006_2026",
                        "company_name": "Missing ISIN Limited",
                        "nse_symbol": "NOISIN",
                        "effective_delisting_date": "2018-03-01",
                        "delisting_type": "VOLUNTARY",
                    },
                )
                store.upsert_nse_entity(
                    {
                        "isin": "INE001A01036",
                        "nse_symbol": "OWNER",
                        "company_name": "Existing Owner Limited",
                        "listing_status": "ACTIVE",
                    }
                )

                # Rejects duplicate ISIN already owned by another entity
                self.assertFalse(
                    store.backfill_entity_isin(
                        "NOISIN", "INE001A01036", observed_date="2017-01-01"
                    )
                )

                # Rejects observed_date strictly after latest_delisting_date
                self.assertFalse(
                    store.backfill_entity_isin(
                        "NOISIN", "INE002A01018", observed_date="2019-06-01"
                    )
                )
                self.assertIsNone(store.get_nse_entity(nse_symbol="NOISIN")["isin"])

                # Rejects invalid / non-equity ISIN
                self.assertFalse(
                    store.backfill_entity_isin(
                        "NOISIN", "INE002A07018", observed_date="2017-06-01"
                    )
                )

                # Attaches valid equity ISIN when observed on or before delisting date
                self.assertTrue(
                    store.backfill_entity_isin(
                        "NOISIN", "INE002A01018", observed_date="2017-06-01"
                    )
                )
                updated = store.get_nse_entity(nse_symbol="NOISIN")
                self.assertEqual(updated["isin"], "INE002A01018")
                events = store.list_delisting_events(entity_id=ent_id)
                self.assertEqual(events[0]["isin"], "INE002A01018")


class ZFactorAndCalibrationRegressionTests(unittest.TestCase):
    def test_safe_exp_overflow_guard_and_extreme_z_computation(self) -> None:
        self.assertEqual(_safe_exp(0.0), 1.0)
        self.assertIsNone(_safe_exp(701.0))
        self.assertIsNone(_safe_exp(1e9))
        self.assertIsNone(_safe_exp(math.inf))
        self.assertIsNone(_safe_exp(math.nan))

        extreme_cal = TargetBracketCalibration(
            target_min_cr=3500.0,
            target_max_cr=12500.0,
            ref_log_center=500.0,
            ref_log_scale=500.0,
            z_min=0.0,
            z_max=2.0,
            source="fixed_z",
        )
        records = [
            {"nse_symbol": "A", "trade_date": "2024-01-01", "market_cap_cr": 100.0},
            {"nse_symbol": "B", "trade_date": "2024-01-01", "market_cap_cr": 10000.0},
        ]
        scored = compute_cross_sectional_z_factors(records, calibration=extreme_cal)
        self.assertEqual(len(scored), 2)
        # Positive Z row has ref_log_center + ref_log_scale * (+1.0) = 1000 > 700 -> None, no OverflowError
        self.assertIsNone(scored[1]["equivalent_mcap_cr"])

    def test_empty_pit_rows_clears_stale_z_factors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                ent_id, _ = store.upsert_nse_entity(
                    {
                        "isin": "INE040A01034",
                        "nse_symbol": "GHOST",
                        "company_name": "Ghost Corp Limited",
                        "listing_status": "ACTIVE",
                        "listing_date": "2010-01-01",
                    }
                )
                store.upsert_daily_market_row(
                    {
                        "entity_id": ent_id,
                        "isin": "INE040A01034",
                        "nse_symbol": "GHOST",
                        "trade_date": "2020-05-01",
                        "market_cap_cr": 5000.0,
                    }
                )
                first = recompute_store_z_factors(store, pit_universe_only=True)
                self.assertEqual(first["rows_updated"], 1)
                row_before = store.list_daily_market_rows(nse_symbol="GHOST")[0]
                self.assertIsNotNone(row_before["z_factor_mcap"])

                # Mark entity delisted before trade_date so PIT query returns zero rows.
                store.upsert_nse_entity(
                    {
                        "isin": "INE040A01034",
                        "nse_symbol": "GHOST",
                        "company_name": "Ghost Corp Limited",
                        "listing_status": "DELISTED",
                        "latest_delisting_date": "2019-01-01",
                    }
                )
                second = recompute_store_z_factors(store, pit_universe_only=True)
                self.assertEqual(second["rows_updated"], 0)
                self.assertEqual(second["dates_processed"], 0)
                row_after = store.list_daily_market_rows(nse_symbol="GHOST")[0]
                self.assertIsNone(row_after["z_factor_mcap"])
                self.assertIsNone(row_after["in_target_bracket"])

    def test_z_calibration_persisted_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                base = TargetBracketCalibration.default_nse(
                    target_bracket_cr=(3500.0, 12500.0), robust=True
                )
                store.save_z_calibration(
                    {
                        **base.to_dict(),
                        "trailing_days": 5,
                        "z_buffer": 0.15,
                        "value_field": "close",
                    }
                )
                saved = store.latest_z_calibration()
                self.assertIsNotNone(saved)
                assert saved is not None
                self.assertEqual(saved["trailing_days"], 5)
                self.assertAlmostEqual(saved["z_buffer"], 0.15)
                self.assertEqual(saved["value_field"], "close")

                cal = calibration_from_row(saved)
                self.assertEqual(cal.target_min_cr, 3500.0)
                self.assertEqual(cal.target_max_cr, 12500.0)
                self.assertTrue(cal.robust)
                self.assertEqual(cal.trailing_days, 5)
                self.assertAlmostEqual(cal.z_buffer, 0.15)
                self.assertEqual(cal.value_field, "close")


class QuantitativeIngestionTests(unittest.TestCase):
    def test_nse_cm_bhavcopy_csv_ingestion_into_quant_store(self) -> None:
        csv_content = (
            "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n"
            "INFY,EQ,1400.0,1450.0,1390.0,1440.0,1440.0,1410.0,2500000,3600000000.0,15-MAY-2024,42000,INE009A01021\n"
            "BONDX,N1,1000.0,1005.0,995.0,1002.0,1002.0,1000.0,100,100200.0,15-MAY-2024,5,INE009A07010\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "cm15MAY2024bhav.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                store.upsert_nse_entity(
                    {
                        "nse_symbol": "INFY",
                        "company_name": "Infosys Limited",
                        "listing_status": "ACTIVE",
                        "shares_outstanding": 4_150_000_000,
                    }
                )
                summary = ingest_quantitative_paths([csv_path], store, recompute_z=False)
                self.assertEqual(summary["files_processed"], 1)
                self.assertEqual(summary["records_parsed"], 1)
                self.assertEqual(summary["daily_rows_inserted"], 1)
                self.assertEqual(summary["isins_backfilled"], 1)

                rows = store.list_daily_market_rows(
                    trade_date="2024-05-15", nse_symbol="INFY"
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["close"], 1440.0)
                self.assertEqual(rows[0]["total_traded_qty"], 2_500_000)
                self.assertAlmostEqual(rows[0]["turnover_cr"], 360.0, places=4)
                self.assertEqual(rows[0]["isin"], "INE009A01021")
                self.assertIsNotNone(rows[0]["entity_id"])

    def test_zip_bomb_uncompressed_cap_rejected(self) -> None:
        self.assertEqual(MAX_ZIP_UNCOMPRESSED_BYTES, 200 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "oversized.zip"
            payload = "SYMBOL,DATE,CLOSE\n" + ("TEST,2024-05-15,100.0\n" * 100)
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("bhav.csv", payload.encode("utf-8"))

            with mock.patch.object(quantitative, "MAX_ZIP_UNCOMPRESSED_BYTES", 512):
                with self.assertRaisesRegex(ValueError, "uncompressed limit"):
                    parse_quantitative_file(zip_path)


class QualitativeExtractionAndRenderingTests(unittest.TestCase):
    def test_deterministic_extractor_markdown_and_html_ingest_and_render(self) -> None:
        md_filing = """# Audited Financial Results for Q4 FY24
Symbol: INFY
ISIN: INE009A01021
Broadcast Date: 2024-04-18 18:30:00
Quarter ended: March 31, 2024

- ROCE: 31.5%
- ROE: 29.2%
- EBITDA Margin: 24.1%
- Debt to Equity: 0.08
- EPS (TTM): Rs. 63.4
"""
        html_filing = """<html>
<head><style>.hide { display:none; }</style><script>var ignored = 1;</script></head>
<body>
  <h1>Annual Earnings Call Transcript</h1>
  <p>Symbol: INFY</p>
  <p>ISIN: INE009A01021</p>
  <p>Filing Date: 2024-04-19 10:00:00</p>
  <table>
    <tr><th>Metric</th><th>Value</th></tr>
    <tr><td>Sales Growth (YoY)</td><td>12.5%</td></tr>
  </table>
  <p>Management Commentary: We expect to grow revenue strongly in FY25 with steady margin expansion.</p>
</body>
</html>
"""
        extractor = DeterministicFilingExtractor()
        md_records = extractor.extract_market_records(md_filing, source_name="infy_q4.md")
        self.assertEqual(len(md_records), 1)
        rec = md_records[0]
        self.assertEqual(rec["isin"], "INE009A01021")
        self.assertEqual(rec["nse_symbol"], "INFY")
        self.assertEqual(rec["broadcast_timestamp"], "2024-04-18T18:30:00")
        self.assertEqual(rec["quarter_ending"], "2024-03-31")
        self.assertEqual(rec["announcement_category"], "FINANCIAL_RESULTS")
        self.assertAlmostEqual(rec["roce_pct"], 31.5)
        self.assertAlmostEqual(rec["roe_pct"], 29.2)
        self.assertAlmostEqual(rec["ebitda_margin_pct"], 24.1)
        self.assertAlmostEqual(rec["debt_to_equity"], 0.08)
        self.assertAlmostEqual(rec["eps_ttm"], 63.4)

        with tempfile.TemporaryDirectory() as tmp:
            filings_dir = Path(tmp) / "filings"
            filings_dir.mkdir()
            (filings_dir / "01_results.md").write_text(md_filing, encoding="utf-8")
            html_path = filings_dir / "02_concall.html"
            html_path.write_text(html_filing, encoding="utf-8")

            converted_md = extract_document_markdown(html_path)
            self.assertNotIn("var ignored", converted_md)
            self.assertIn("Annual Earnings Call Transcript", converted_md)
            html_records = extractor.extract_market_records(
                converted_md, source_name=html_path.name
            )
            self.assertEqual(len(html_records), 1)
            self.assertEqual(html_records[0]["announcement_category"], "CONCALL_TRANSCRIPT")
            self.assertIsNotNone(html_records[0]["concall_guidance_md"])

            with QuantStore(Path(tmp) / "quant.sqlite3") as store:
                store.upsert_nse_entity(
                    {
                        "isin": "INE009A01021",
                        "nse_symbol": "INFY",
                        "company_name": "Infosys Limited",
                        "listing_status": "ACTIVE",
                        "listing_date": "1995-02-08",
                        "series": "EQ",
                    }
                )
                ingest_summary = ingest_qualitative_paths([filings_dir], store)
                self.assertEqual(ingest_summary["files_processed"], 2)
                self.assertEqual(ingest_summary["filings_inserted"], 2)
                self.assertEqual(ingest_summary["fundamentals_upserted"], 1)
                self.assertEqual(ingest_summary["entities_matched"], 2)
                self.assertEqual(store.count_qualitative_filings(), 2)

                profile = store.get_company_profile_and_timeline("INE009A01021")
                self.assertIsNotNone(profile)
                assert profile is not None
                rendered = render_company_status_markdown(profile)
                self.assertIn("# Infosys Limited (INFY)", rendered)
                self.assertIn("INE009A01021", rendered)
                self.assertIn("## Latest Fundamentals", rendered)
                self.assertIn("63.4", rendered)
                self.assertIn("## Qualitative & Lifecycle Timeline", rendered)
                self.assertIn("Audited Financial Results for Q4 FY24", rendered)
                self.assertIn("Annual Earnings Call Transcript", rendered)


if __name__ == "__main__":
    unittest.main()
