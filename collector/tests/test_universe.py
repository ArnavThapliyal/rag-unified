"""Universe DB: archive sync, idempotency, survivorship-bias-free universe-as-of (from Unified_Pipeline)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rag_collector.nse_bridge import _read_clean_csv, sync_archive_to_store
from rag_collector.universe import UniverseStore

REPO_ROOT = Path(__file__).resolve().parent.parent


class UniverseTests(unittest.TestCase):
    def test_real_archive_sync_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with UniverseStore(Path(tmp) / "unified.sqlite3") as store:
                first = sync_archive_to_store(REPO_ROOT, store)
                second = sync_archive_to_store(REPO_ROOT, store)

                self.assertGreaterEqual(first["active_equity_rows"] + first["active_sme_rows"], 3200)
                # counts follow the collector's current outputs (they grow as collection runs)
                processed = REPO_ROOT / "data" / "processed"
                self.assertEqual(first["delisted_2006_2026_rows"],
                                 len(_read_clean_csv(processed / "nse_delisted_master.csv")))
                self.assertEqual(first["delisted_pre_2006_rows"],
                                 len(_read_clean_csv(processed / "out_of_scope_delistings.csv")))
                leads = _read_clean_csv(processed / "proposed_delisting_leads.csv")
                self.assertEqual(first["proposed_lead_rows"], sum(1 for r in leads if r.get("company_name")))

                # Idempotency check
                self.assertEqual(first["entities_total"], second["entities_total"])
                self.assertEqual(first["delisting_events_total"], second["delisting_events_total"])
                self.assertEqual(
                    first["qualitative_filings_total"], second["qualitative_filings_total"]
                )

                entities_by_sym = {
                    e["nse_symbol"]: e
                    for e in store.list_nse_entities()
                    if e["nse_symbol"]
                }
                for sym in ("BATLIBOI", "SICAGEN", "ARIHANT"):
                    self.assertIn(sym, entities_by_sym)
                    ent = entities_by_sym[sym]
                    self.assertEqual(ent["listing_status"], "ACTIVE")
                    self.assertIsNotNone(ent["latest_delisting_date"])
                    self.assertIsNotNone(ent["isin"])
                    events = store.list_delisting_events(entity_id=ent["id"])
                    self.assertGreaterEqual(len(events), 1)

    def test_survivorship_bias_free_universe_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with UniverseStore(Path(tmp) / "unified.sqlite3") as store:
                sync_archive_to_store(REPO_ROOT, store)

                u_2007 = store.list_nse_entities(as_of_date="2007-06-01")
                u_2008 = store.list_nse_entities(as_of_date="2008-06-01")
                u_2015 = store.list_nse_entities(as_of_date="2015-06-01")
                u_2020 = store.list_nse_entities(as_of_date="2020-06-01")
                u_2025 = store.list_nse_entities(as_of_date="2025-06-01")
                u_2026 = store.list_nse_entities(as_of_date="2026-06-01")

                syms_2007 = {e["nse_symbol"] for e in u_2007}
                syms_2008 = {e["nse_symbol"] for e in u_2008}
                syms_2015 = {e["nse_symbol"] for e in u_2015}
                syms_2020 = {e["nse_symbol"] for e in u_2020}
                syms_2025 = {e["nse_symbol"] for e in u_2025}
                syms_2026 = {e["nse_symbol"] for e in u_2026}

                # BATLIBOI delisted 2012-06-28, re-listed 2026-04-20
                self.assertIn("BATLIBOI", syms_2007)
                self.assertNotIn("BATLIBOI", syms_2015)
                self.assertIn("BATLIBOI", syms_2026)

                # Essar Steel (ESTL) delisted 2007-12-24
                self.assertIn("ESTL", syms_2007)
                self.assertNotIn("ESTL", syms_2008)

                # Aadhar Housing Finance (AADHARHFC) listed 2024-05-15
                self.assertNotIn("AADHARHFC", syms_2020)
                self.assertIn("AADHARHFC", syms_2025)


if __name__ == "__main__":
    unittest.main()
