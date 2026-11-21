# Repository Audit — 2026-11-21

A pre-long-run review of everything built so far: removals, bugs fixed, and items kept on purpose. Every script in `scripts/` was read in full and statically checked (`pyflakes` reports 0 issues, `bash -n` passes). The pipeline was then re-tested end to end on an isolated copy of the repo, so the real repo starts the long run with no data.

## 1. Removed

| Item | Why it was unneeded |
|---|---|
| The earlier IDE-extension agent config (its rules folder, ignore file and a 476 MB vendor clone) | Cline is now the only agent. Rules and ignore files live in `.cline/rules/` and `.clineignore`. |
| `projectstructure.txt` | A stale `tree` snapshot that still listed the removed vendor clone, test PDFs and `__pycache__`. Superseded by the README layout section. |
| `scripts/__pycache__/` | Python bytecode (and git-ignored) |
| `SCHEMA_CSV` constant in `scripts/common.py` | Defined but never used |
| **All test-run data** | Reset so the long run starts clean. Removed: 13 raw downloads, 6 search-cache files, 13 parsed files, 13 manifest lines, 63 events, 2 test run summaries, 1 smoke-test agent session, plus the derived CSVs and metadata. |
| Smoke-test Cline config | Kept in the session scratchpad only, never in the repo or `~/.cline` |

## 2. Bugs found and fixed before the long run

| # | Problem | Impact on a long run | Fix |
|---|---|---|---|
| 1 | **False positives from non-listing pages.** NSE's "defaulter / expelled members" page lists *stockbrokers* (e.g. Angel One, YES Securities). 29 of 32 test records came from it, all marked `VERIFIED_PRIMARY`. | The dataset would fill with brokers that were never delisted | Relevance gate in `parse_nse_records.py`: a document produces rows only if its URL, the page it was harvested from, its HTML title or its PDF/XLSX text mentions delisting. Member, broker, defaulter, arbitration and complaint pages are always excluded. Decisions are recorded in `data/processed/parsed/_relevance.json` and `artifacts.csv`. |
| 2 | **Proposed delistings counted as delistings.** NSE's "companies proposed to be delisted" spreadsheet added 14 companies that aren't delisted. | Wrong records | Routed to `data/processed/proposed_delisting_leads.csv` as companies to verify later |
| 3 | **Procedural documents harvested.** Checklists, the SOP and annexures linked from delisting pages were fetched and parsed. | Wasted fetches, and the agent would chase zero-row files | Skipped at harvest time and marked `procedural_document` if found another way (`PROCEDURAL_RE` in `common.py`) |
| 4 | **Empty search results cached forever.** When SearXNG's upstream engines throttle, a query returns 0 results. That page was cached and the query marked done. | Coverage silently lost | Empty pages aren't trusted from cache. `unresponsive_engines` is recorded and logged. `--retry-failed` also re-runs `no_results` queries. |
| 5 | **Failed URLs re-fetched every run.** Only 200 responses counted as done. | Breaks the "fetch once" rule; repeated 404/403 hits | Definitive 4xx errors (except 429) are never requested again. Timeouts, 429 and 5xx stay retryable. |
| 6 | **Misleading harvest backlog.** Run summaries counted only the current page's pending links. | Wrong indicator | Global `harvest_backlog()`, computed at the end of every run |
| 7 | **Run ids collided inside an agent session.** Every `collect.py` call in one session shared a run id and overwrote one summary file. | Lost run history | Each `collect.py` call gets its own `run_id`. The session is tagged separately as `agent_session`. |
| 8 | **Wrong delisting type in order PDFs.** The extractor matched the warning sentence ("failing which … shall be delisted") instead of the decision. | `OTHER` instead of `COMPULSORY` | Prefers the "compulsorily delisted" decision sentence |
| 9 | **`NaT` read as a company name.** | Junk row | Empty-value strings (`nan`, `nat`, `none`) are rejected as names |
| 10 | **Wrong Cline LM Studio auth command in the README.** | Setup failed | Uses `-k lm-studio -m …`, which the `lmstudio` provider requires. `run_agent.sh` now passes the provider explicitly. |

## 3. Kept on purpose (flagged, not removed)

| Item | Why it stays |
|---|---|
| `vendor/cline` (125 MB, git-ignored) | A reference clone you asked for. It was used to confirm the rules/skills paths, auto-approve behaviour and provider ids. Safe to delete. |
| `node_modules/` (371 MB, git-ignored) | Needed for `npx cline` (pinned 3.0.75) |
| `npm audit`: 12 remaining findings (0 critical) | All upstream inside Cline (`zod` 3 in the unused `dify-ai-provider`, and a beta ai-sdk in Langfuse telemetry). The critical ones are fixed via `overrides`. |
| `nse_delisted_master.csv` (root) | Schema reference from your spec. The data lives in `data/processed/`. |
| SearXNG `settings.yml.bak` (inside the container) | Backup of the original config, kept so the change can be rolled back |

## 4. Verified before handover

- `pyflakes scripts/*.py` gives 0 issues, and `bash -n scripts/run_agent.sh` passes.
- **Isolated end-to-end run** (on a copy, so the real repo stays clean): 2 queries, 16 downloads (10 harvested), 0 quarantined, 4 off-topic files rejected. Result: **10 records, all real**:
  - 7 compulsory orders: Jindal Cotex, Penta Gold, S.S. Infrastructure, Poddar Housing, SecUR Credentials, Varanium Cloud and Suumaya Industries. Suumaya is `NEEDS_REVIEW` because no order date was found.
  - 3 voluntary exits: Bhagyanagar Properties, TCI Developers, DFM Foods.

  Regimes match the dates.
- **Cline unattended smoke test** on `qwen/qwen3.8-27b`: it ran the scripts, logged a milestone and finished with `completed`. The JSON stream was captured and summarized.

## 5. Ready state

The real repo has an empty `data/` and `logs/` (only `.gitkeep` files plus an empty `logs/fetch_manifest.jsonl`). The plan has 97 queries in 6 phases. Start the long run with `scripts/run_agent.sh`.

## 6. Second pass (tests, checks, disk guard)

| Change | Notes |
|---|---|
| `tests/` (pytest, 24 tests, offline) | Cover the parser (dates, types, order extractor, tables, ISIN lines, relevance gate, proposed lists), merging (FY, regime boundaries, dedupe, confidence upgrade, stable ids), the fetcher (harvest filtering, fetch-once rules, extensions), domain rules, the disk guard, the dataset audit, and query-plan validity. |
| `scripts/check.sh` | One command for everything: pyflakes, bash syntax, JSON configs, pytest, dataset audit, disk, stale-reference scan, SearXNG, LM Studio, the Cline CLI. `--offline` skips the services. |
| `scripts/audit_dataset.py` | Data-quality audit of the master CSV, written to `data/metadata/dataset_audit.json` and summarized in the dashboard. |
| Disk guard (`common.check_disk`) | Warns when `data/` passes 5 GB and again every +5 GB (terminal, log and macOS notification). Stops the run when free space drops below 20 GB. Thresholds are in `config/query_plan.json` → `disk`. |
| **Bug caught by the new tests** | `check_disk()` passed a dict with a `level` key into `log(level=…)`. The first disk warning would have crashed the run. Fixed. |
| `instructions.md` | The agent references now name Cline (`.cline/rules/`, Cline CLI in LM Studio) |
| `status_report.py` trimmed | No longer recounts the manifest and search cache itself; it reads the metadata, plus disk and audit lines |
| `requirements-dev.txt` | `pytest`, `pyflakes` |
