# Staging regeneration (release candidate)

Built from an isolated copy of the production raw archive (568 data files + 6 .gitkeep) with the code at commit
`fede71d`. Commands, run under a localhost-only network sandbox:

    scripts/parse_nse_records.py --force
    scripts/normalize_and_merge.py
    scripts/audit_dataset.py --out audit.json

The OCR cache came from the phase-1 preview (scripts/ocr.py unchanged since). Production (`../nse-delisted-comp`,
`main` at 35df260) was not modified: `production_hashes_before.txt` and `production_hashes_after.txt` (sha256 of every
raw file, the master CSV/JSONL, the fetch manifest and the metadata) are identical.

These files are a snapshot for review, **not** the production dataset. Record-level comparison with production:
`../reconciliation/record_reconciliation.csv` (+ `_summary.json`), produced by `../tools/reconcile_master.py`.
