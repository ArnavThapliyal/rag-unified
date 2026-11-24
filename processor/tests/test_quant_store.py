"""Quant store and Z-factor tests ported from Unified_Pipeline's test_unified_pipeline.py (Sanidhya)."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from pipeline.quant.store import QuantStore
from pipeline.quant.z_factor import (
    classify_size_bracket,
    compute_cross_sectional_z_factors,
    recompute_store_z_factors,
)


class QuantStoreTests(unittest.TestCase):
    def test_daily_market_upsert_and_pit_fundamentals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "unified.sqlite3") as store:
                store.upsert_nse_entity(
                    {
                        "isin": "INE001A01036",
                        "nse_symbol": "RELIANCE",
                        "company_name": "Reliance Industries Limited",
                        "listing_status": "ACTIVE",
                    }
                )

                # First insert OHLCV, then enrich with valuation fields on same symbol + date
                inserted = store.ingest_unified_market_record(
                    {
                        "isin": "INE001A01036",
                        "nse_symbol": "RELIANCE",
                        "trade_date": "2020-05-15",
                        "close": 1450.0,
                        "market_cap_cr": 920000.0,
                    }
                )
                self.assertTrue(inserted["daily_upserted"])

                updated = store.ingest_unified_market_record(
                    {
                        "nse_symbol": "RELIANCE",
                        "trade_date": "2020-05-15",
                        "pe_ratio": 22.5,
                        "sector": "Energy",
                    }
                )
                self.assertFalse(updated["daily_upserted"])

                rows = store.list_daily_market_rows(
                    trade_date="2020-05-15", nse_symbol="RELIANCE"
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["close"], 1450.0)
                self.assertEqual(rows[0]["market_cap_cr"], 920000.0)
                self.assertEqual(rows[0]["pe_ratio"], 22.5)
                self.assertEqual(rows[0]["sector"], "Energy")
                self.assertIsNotNone(rows[0]["entity_id"])

                # Point-in-time fundamentals: Q4 FY20 ended 2020-03-31, announced 2020-04-30
                store.ingest_unified_market_record(
                    {
                        "nse_symbol": "RELIANCE",
                        "quarter_ending": "2020-03-31",
                        "broadcast_timestamp": "2020-04-30T18:00:00",
                        "eps_ttm": 62.5,
                    }
                )
                self.assertEqual(
                    store.list_fundamentals_as_of("RELIANCE", "2020-04-15T09:15:00"),
                    [],
                )
                as_of_may = store.list_fundamentals_as_of(
                    "RELIANCE", "2020-05-01T09:15:00"
                )
                self.assertEqual(len(as_of_may), 1)
                self.assertEqual(as_of_may[0]["eps_ttm"], 62.5)

    def test_z_factor_bracket_invariance_and_zero_lookahead(self) -> None:
        # 2011 cross-section
        records_2011 = [
            {"nse_symbol": "A", "trade_date": "2011-06-01", "market_cap_cr": 100.0},
            {"nse_symbol": "B", "trade_date": "2011-06-01", "market_cap_cr": 1000.0},
            {"nse_symbol": "C", "trade_date": "2011-06-01", "market_cap_cr": 10000.0},
            {"nse_symbol": "D", "trade_date": "2011-06-01", "market_cap_cr": 100000.0},
        ]
        # 2026 cross-section scaled exactly 10x in nominal INR
        records_2026 = [
            {"nse_symbol": "A", "trade_date": "2026-06-01", "market_cap_cr": 1000.0},
            {"nse_symbol": "B", "trade_date": "2026-06-01", "market_cap_cr": 10000.0},
            {"nse_symbol": "C", "trade_date": "2026-06-01", "market_cap_cr": 100000.0},
            {"nse_symbol": "D", "trade_date": "2026-06-01", "market_cap_cr": 1000000.0},
        ]

        only_2011 = compute_cross_sectional_z_factors(records_2011)
        combined = compute_cross_sectional_z_factors(records_2011 + records_2026)

        # Zero lookahead: adding 2026 data must not change 2011 outputs
        for r1, r2 in zip(only_2011, combined[:4]):
            self.assertEqual(r1["z_factor"], r2["z_factor"])
            self.assertEqual(r1["percentile"], r2["percentile"])
            self.assertEqual(r1["size_bracket"], r2["size_bracket"])
            self.assertEqual(r1["deflated_value"], r2["deflated_value"])

        # Bracket invariance across 15 years (10x nominal inflation)
        for r_11, r_26 in zip(combined[:4], combined[4:]):
            self.assertAlmostEqual(r_11["z_factor"], r_26["z_factor"], places=6)
            self.assertEqual(r_11["percentile"], r_26["percentile"])
            self.assertEqual(r_11["size_bracket"], r_26["size_bracket"])
            self.assertAlmostEqual(r_11["deflated_value"], r_26["deflated_value"], places=4)

        # Anchor to 2026 maps 2011 stocks into 2026 nominal rupee equivalents
        anchored_2026 = compute_cross_sectional_z_factors(
            records_2011 + records_2026, anchor_date="2026-06-01"
        )
        self.assertAlmostEqual(anchored_2026[0]["deflated_value"], 1000.0, places=4)
        self.assertAlmostEqual(anchored_2026[3]["deflated_value"], 1000000.0, places=4)

    def test_z_factor_robust_edge_cases_and_db_persistence(self) -> None:
        self.assertIsNone(classify_size_bracket(None))
        self.assertEqual(classify_size_bracket(1.5), "MEGA_CAP")
        self.assertEqual(classify_size_bracket(0.5), "LARGE_CAP")
        self.assertEqual(classify_size_bracket(0.0), "MID_CAP")
        self.assertEqual(classify_size_bracket(-1.0), "SMALL_CAP")
        self.assertEqual(classify_size_bracket(-2.0), "MICRO_CAP")

        edge_records = [
            {"nse_symbol": "SOLO", "trade_date": "2010-01-01", "market_cap_cr": 500.0},
            {"nse_symbol": "ZERO", "trade_date": "2010-01-02", "market_cap_cr": 0.0},
            {"nse_symbol": "NULL", "trade_date": "2010-01-02", "market_cap_cr": None},
            {"nse_symbol": "NAN", "trade_date": "2010-01-02", "market_cap_cr": math.nan},
            {"nse_symbol": "VALID1", "trade_date": "2010-01-02", "market_cap_cr": 100.0},
            {"nse_symbol": "VALID2", "trade_date": "2010-01-02", "market_cap_cr": 400.0},
            {"nse_symbol": "VALID3", "trade_date": "2010-01-02", "market_cap_cr": 1600.0},
        ]
        res = compute_cross_sectional_z_factors(edge_records, robust=True)
        self.assertEqual(res[0]["z_factor"], 0.0)
        self.assertEqual(res[0]["percentile"], 50.0)
        self.assertIsNone(res[1]["z_factor"])
        self.assertIsNone(res[2]["z_factor"])
        self.assertIsNone(res[3]["z_factor"])
        self.assertLess(res[4]["z_factor"], 0)
        self.assertEqual(res[5]["z_factor"], 0.0)
        self.assertGreater(res[6]["z_factor"], 0)

        with tempfile.TemporaryDirectory() as tmp:
            with QuantStore(Path(tmp) / "unified.sqlite3") as store:
                for row in edge_records:
                    if row["market_cap_cr"] is None or (
                        isinstance(row["market_cap_cr"], float)
                        and math.isnan(row["market_cap_cr"])
                    ):
                        continue
                    store.upsert_daily_market_row(row)
                summary = recompute_store_z_factors(store, robust=True)
                self.assertEqual(summary["rows_updated"], 4)
                self.assertEqual(summary["dates_processed"], 2)
                self.assertEqual(summary["anchor_date_used"], "2010-01-01")
                stored = store.list_daily_market_rows(trade_date="2010-01-02")
                by_sym = {r["nse_symbol"]: r for r in stored}
                self.assertEqual(by_sym["VALID2"]["size_bracket"], "MID_CAP")
                self.assertIsNone(by_sym["ZERO"]["z_factor_mcap"])



if __name__ == "__main__":
    unittest.main()
