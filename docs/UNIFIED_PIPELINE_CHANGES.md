# Unified_Pipeline: what Sanidhya changed, and where it went

`/Users/arnav/Projects/Software/Unified_Pipeline` (a plain folder, not a git repo) is a copy of
**`nse-delisted-comp` at `remediation/qa-phase1` (0713c59)** plus the changes below. It was imported verbatim as
commit `bab190f` on branch `unified-pipeline` of rag-collector, then split between rag-collector and rag-processor.

Inventory method: `rsync` of the folder over a checkout of `remediation/qa-phase1` (excluding `.git .venv
node_modules vendor` and git-ignored runtime data), `git diff --name-status -M`, and `git check-ignore --no-index`
for every file in the folder.

## 1. Files that will never reach GitHub (git-ignored)

These exist in Unified_Pipeline but are excluded by `.gitignore`, so the pushed repos do **not** contain them:

| File in Unified_Pipeline | Ignored by | What it is | Where it comes from now |
|---|---|---|---|
| `data/nse_unified.sqlite3` (1.5 MB) | `data/nse_unified.sqlite3` | the SQLite DB built by `sync-nse-archive` | rebuilt by `rag-collector-step universe-sync` (3,654 entities, 457 events, 457 filings) |
| `logs/.issues.lock` | `logs/.issues.lock` | lock file of `report_issue.py` | created at run time |
| `data/companies.sqlite3` (listed in his `.gitignore`, not present) | `data/companies.sqlite3` | DB of the generic company directory | dropped with the directory (see §3) |
| `*.egg-info/` (listed, not present) | `*.egg-info/` | `pip install -e .` metadata | created by the installer |

Everything else in the folder is tracked. Also not in Unified_Pipeline (so not "his" changes, but runtime state on
this machine): `data/docket.sqlite`, `data/raw/**`, `data/processed/parsed/*`, `data/search_cache/*` — git-ignored and
kept untouched in the local checkout.

## 2. What changed relative to `remediation/qa-phase1`

### Code

| Path | Change | Lines (+/−) | Destination |
|---|---|---|---|
| `src/company_pipeline/nse_bridge.py` | **new**: active/delisted/pre-2006/leads CSVs → SQLite universe | +297 | **rag-collector** `src/rag_collector/nse_bridge.py` (unchanged apart from the store class name) |
| `src/company_pipeline/storage.py` | **new**: one SQLite store for companies, universe, daily market data, fundamentals, filings, Z calibrations | +1442 | split: entity/delisting/filing tables + their methods → **rag-collector** `universe.py` (extracted unchanged by AST); daily market, quarterly fundamentals, Z calibrations → **rag-processor** quant stage |
| `src/company_pipeline/z_factor.py` | **new**: point-in-time cross-sectional Z-factor, size brackets, target bracket calibration | +739 | **rag-processor** `pipeline/quant/` |
| `src/company_pipeline/market_data.py` | **new**: 50-column market schema, CSV export | +125 | **dropped**: identical to rag-processor's `pipeline/schema.py` extended fields (same 50 names, same order); rag-processor's schema stays the single source |
| `src/company_pipeline/pdf.py`, `markdown.py` | **new**: PyMuPDF text + image-region OCR, page Markdown | +95, +8 | **dropped**: rag-processor parses PDFs with docling (on-demand OCR) |
| `src/company_pipeline/api.py` | **new**: adapter protocols (market data API, document → market records) | +103 | **dropped**: interfaces with no implementation; rag-processor's tabular record mode fills the same columns from files |
| `src/company_pipeline/models.py`, `filtering.py` | **new**: generic company directory (name/website/country) | +57, +27 | **dropped**: not used by either stage |
| `src/company_pipeline/cli.py` | **new**: `company-pipeline` CLI (9 commands) | +384 | split: `universe-as-of` → `rag-collector` CLI; `sync-nse-archive` → step `universe-sync`; Z-factor / bracket commands → rag-processor `steps quant-*`; the rest dropped |
| `pyproject.toml` | **new**: package `nse-unified-pipeline` 0.2.0 | +36 | rewritten as `rag-collector` 0.3.0 (console scripts `rag-collector`, `rag-collector-step`, `rag-collector-loop`) |
| `requirements.txt` | `+PyMuPDF>=1.24` | +1 | removed again (only `pdf.py` used it) |
| `scripts/check.sh` | pyflakes also over `src/` | ±1 | kept |
| `scripts/agent_sandbox.py`, `credentials.py`, `setup.py`, `web_search.py`, `search_providers/base.py`, `run_agent.sh`, `verify_records.py`, `install.sh`, `.env.example`, `config/query_plan.json` | rename `nse-delisted-comp` → `nse-unified-pipeline` (keys path `~/.config/nse-unified-pipeline`), one pandas import re-indented | ±1–6 each | renamed again to **rag-collector** (`~/.config/rag-collector/keys.env`) |
| `.gitignore` | `*.py[cod]`, `*.egg-info/`, the two SQLite DBs | +4/−1 | kept; outbox, locks, STOP flag and loop log added |

### Tests

| Path | Change | Destination |
|---|---|---|
| `tests/test_rc_agent_sandbox.py` → `test_agent_sandbox.py` | renamed (94 % similar), keys path | rag-collector |
| `tests/test_rc_agent_supervisor.py` → `test_agent_supervisor.py` | renamed | rag-collector |
| `tests/test_rc_agy_spotchecks.py` → `test_date_and_isin_edge_cases.py` | renamed (85 % similar) — the AGY spot checks are **not** lost | rag-collector |
| `tests/test_rc_false_entities.py`, `test_rc_header_dates.py`, `test_rc_network_outage.py` → without `rc_` | renamed | rag-collector |
| `tests/conftest.py` | `src/` on `sys.path` | rag-collector |
| `tests/test_data_integrity.py`, `test_execution_reliability.py`, `test_operational_resilience.py`, `test_security_regressions.py`, `test_tool_guard.py` | comments no longer cite remediation ids; keys path | rag-collector |
| `tests/test_unified_pipeline.py` | **new** (5 tests) | first 2 (archive sync, survivorship) → rag-collector `test_universe.py`, with counts read from the CSVs instead of hard-coded 409/34/14; the 3 market/Z tests → rag-processor |
| `tests/test_z_factor_brackets.py` | **new** (7 tests) | rag-processor |
| `tests/test_pipeline.py` | **new** (company directory, market CSV, PyMuPDF OCR) | dropped with those modules |

### Docs

| Path | Change | Now |
|---|---|---|
| `README.md` | rewritten 35 KB → 5 KB (Z-factor quickstart; most operating docs removed) | the full manual is restored and updated for rag-collector |
| `instructions.md` | mission widened to quantitative data; `src/company_pipeline` in the layout | updated for rag-collector |
| `AUDIT.md`, `qa/` (9 files + 7 evidence), `remediation/` (reports, reconciliation, evidence, `staging_rc/`, `tools/reconcile_master.py`) | **removed** | removed from the tree, still in history: `git show 0713c59:<path>` |

### Data

| Path | Change |
|---|---|
| `data/processed/nse_delisted_master.csv` / `.jsonl` | 491 → **409** records: byte-identical to `remediation/staging_rc/nse_delisted_master.csv` (the regenerated release-candidate dataset) |
| `data/processed/out_of_scope_delistings.csv` (34), `extraction_review.csv` (26) | **new** in `data/processed/` (moved from `remediation/staging_rc/`) |
| `data/processed/bse_only_delistings.csv`, `priority_years_audit.csv`, `verification_report.csv`, `data/metadata/*.json` | regenerated with the RC code |
| `data/reference/nse_equity_listed.csv`, `nse_sme_listed.csv` | unchanged (already tracked) |
| `logs/runs/*.json` (24), `logs/issues.jsonl` | **missing** in Unified_Pipeline; **kept** in rag-collector (tracked provenance) |

## 3. Why the split

`company_pipeline` did three different jobs:

1. **Point-in-time universe** (who was listed on a date): built from the collector's own CSVs → stays in
   **rag-collector**, run automatically after every collection segment (`universe-sync`) and shipped to rag-processor
   as a `sqlite3.backup()` snapshot inside each outbox batch.
2. **Document/market-record processing** (PDF → Markdown, OCR, 50-column schema): duplicated rag-processor → kept
   only in **rag-processor** (docling parser, `pipeline/schema.py`).
3. **Quant / Z-factor**: needs daily prices and market caps, which the collector never gathers → **rag-processor's
   quant stage**, fed by the price/fundamentals files rag-processor ingests in record mode, filtered with the
   universe snapshot. Until price files are ingested it reports `noop` (`no_price_rows`).

Nothing of his code was rewritten in the move: the universe store was extracted method by method (AST), the bridge
and the Z-factor engine were moved as they are; adapters were added around them.
