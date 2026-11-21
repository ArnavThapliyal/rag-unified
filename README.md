# NSE Delisted Companies Archive

A local-first research pipeline for building a verified, source-cited dataset of companies delisted from the **National Stock Exchange of India (NSE)**, 2006–2026. Four years get deep coverage: **2007, 2017, 2020, 2024**.

The **Cline** agent runs unattended on a local model (LM Studio, `qwen/qwen3.8-27b`). Python scripts do all the searching, fetching, parsing and merging, and they log every step. The agent reads only short JSON summaries, decides what to do next, and keeps going without asking for approval.

Full specification: [`instructions.md`](instructions.md). Cleanup audit: [`AUDIT.md`](AUDIT.md).

---

## How it works

```text
config/query_plan.json  (phases: master → 2017 → 2020 → 2024 → 2007 → archive)
          │
          ▼  scripts/collect.py  (one resumable run; Cline calls it in a loop)
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ 1. search    SearXNG localhost:8082        → data/search_cache/          │
  │ 2. fetch     official/trusted domains only → data/raw/<bucket>/          │
  │ 3. harvest   document links on official    → data/raw/<bucket>/          │
  │              pages (e.g. NSE "orders of delisting" → ~140 order PDFs)    │
  │ 4. parse     tables, ISIN lines, single-company delisting orders         │
  │                                            → data/processed/parsed/      │
  │ 5. merge     dedupe, FY, SEBI regime, confidence                         │
  │                                            → data/processed/*.csv        │
  │ 6. metadata  everything done so far        → data/metadata/              │
  └──────────────────────────────────────────────────────────────────────────┘
          │  every step → logs/events.jsonl, logs/agent_run.log, logs/runs/<run_id>.json
          ▼
  Cline reads the summary → harvests backlog / runs more queries / fixes parser / adds gap queries
```

**Safeguards built in:**
- **Polite fetching.** Waits a random 2–5 s between requests, backs off on 429/5xx, and moves failed downloads to `data/quarantine/`.
- **Fetch once.** A URL that downloaded successfully, or failed with a definitive 4xx error, is never requested again.
- **Blocked domains.** A domain that fails 3 times in a row is blocked and the block is saved. There's no WAF bypass.
- **Resumable.** Completed queries are tracked in `data/metadata/collection_state.json`, so you can stop at any time (Ctrl-C) and re-run.
- **Retries after throttling.** Empty search results are not cached, so a query can be retried with `--retry-failed` if SearXNG's upstream engines were throttling it.
- **Proposed delistings kept out of the dataset.** NSE's "companies proposed to be delisted" lists go to `proposed_delisting_leads.csv` as companies to verify later.
- **Off-topic documents rejected.** Only documents about delisting produce rows. Pages about trading members, brokers and defaulters, and procedural files (checklists, SOPs, forms), are skipped.
- **Disk guard.** Warns when `data/` passes 5 GB and again every +5 GB (terminal line, log entry and macOS notification). The run stops cleanly if free space drops below 20 GB. Adjust under `disk` in `config/query_plan.json`.

---

## Setup (one-time)

```bash
cd /Users/arnav/Projects/Software/nse-delisted-comp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python pipeline
.venv/bin/pip install -r requirements-dev.txt                         # pytest + pyflakes
npm install                                                           # Cline CLI (pinned 3.0.75)

# Optional reference copy of Cline's source (git-ignored)
git clone --depth 1 https://github.com/cline/cline vendor/cline
```

Requirements:
- **SearXNG** (Docker) at `http://localhost:8082` with JSON output enabled (`search.formats: [html, json]` in its `settings.yml`).
- **LM Studio** serving at `http://localhost:1234` with `qwen/qwen3.8-27b` loaded. Give it a large context length, since the agent session gets long.

Check everything (code, tests, configs, data audit, disk, SearXNG, LM Studio, Cline) in one go:
```bash
scripts/check.sh            # add --offline to skip the service checks
```

```text
pyflakes                    ok
bash syntax                 ok
json configs                ok
pytest                      ok  (24 passed in 0.16s)
dataset audit               ok  ({"records": 0, "errors": {}, …})
disk                        ok  (ok 0.0 GB data, 374.4 GB free)
no stale references         ok
SearXNG json                ok
LM Studio                   ok
cline cli                   ok  (3.0.75)
```

---

## Running a long collection

### Terminal 1: the agent

```bash
cd /Users/arnav/Projects/Software/nse-delisted-comp
scripts/run_agent.sh
```

This launches Cline in unattended mode (`--json`, auto-approve) with [`prompts/autonomous_collection.md`](prompts/autonomous_collection.md). It cycles through:
- **Harvest:** fetch any backlog of linked documents.
- **Query:** run the next 8 pending queries.
- **Check:** look at the indicators.
- **Repair:** fix parsing for files that gave zero rows, and add searches for coverage gaps and proposed-delisting leads.
- **Log:** write a milestone line.

It stops when the plan is finished and nothing new is found, when SearXNG is down, or after 25 cycles.

The terminal shows one line per action:

```text
■ agent-20261121-160000 — raw log: logs/agent_sessions/agent-20261121-160000.jsonl
  💬 Harvest backlog is 142; draining it first.
  ▶ [3] run_commands: .venv/bin/python scripts/collect.py --harvest-only --max-harvest 40
  ▶ [4] run_commands: .venv/bin/python scripts/status_report.py --json
■ session finished: completed after 61 iterations, 88 tool calls, …
```

Options:
```bash
CLINE_TIMEOUT=14400 scripts/run_agent.sh                       # stop after 4 h
CLINE_MODEL=google/gemma-4-26b-a4b scripts/run_agent.sh        # different LM Studio model
CLINE_EXTRA_ARGS="--thinking low" scripts/run_agent.sh         # extra Cline flags
scripts/run_agent.sh prompts/my_task.md                        # different task prompt
```

**Without the agent** (deterministic, no LLM), `collect.py` runs the plan by itself:
```bash
.venv/bin/python scripts/collect.py --dry-run          # what's pending + harvest backlog
.venv/bin/python scripts/collect.py --harvest-only --max-harvest 60
.venv/bin/python scripts/collect.py                    # whole plan, in order (hours)
.venv/bin/python scripts/collect.py --phase priority_2017 --max-queries 5
.venv/bin/python scripts/collect.py --retry-failed     # re-run failed / empty queries
```

### Terminal 2: live dashboard

```bash
.venv/bin/python scripts/status_report.py --watch 30
```

```text
 Query plan         ████░░░░░░░░░░░░░░░░  21.6%  21/97 queries
 Priority grid      ██████░░░░░░░░░░░░░░  30.0%  year × type cells with ≥1 record
 Fetch success      ██████████████████░░  91.2%  163 files downloaded
 Verified primary   ███████████████████░  96.0%  4 rows NEEDS_REVIEW
 ISIN completeness  ██░░░░░░░░░░░░░░░░░░  10.0%  rows with an ISIN
 Records: 140 (priority years: 37) | files with rows: 120, zero-row: 43 | blocked domains: 1 | proposed leads: 14
```

### Disk-space warnings

You'll see `!! DISK WARNING: data/ has grown to 5.1 GB …` in the terminal and get a macOS notification. When that happens, free up space, or raise `disk.warn_data_gb` in `config/query_plan.json` to keep going. If free space drops below `disk.min_free_gb` (20 GB), the run stops with `stop_reason: low_disk_space`, the agent finishes, and `run_agent.sh` won't start again until there's room. The dashboard always shows a `Disk:` line. The biggest folder is `data/raw/`, which you can delete once records are extracted (the dataset keeps `source_url` for every row).

### Other things to watch

```bash
tail -f logs/agent_run.log                                   # every script action, human-readable
tail -f logs/events.jsonl                                    # same, structured (JSON per line)
~/.lmstudio/bin/lms log stream --source model --stats        # LM Studio: model input/output + tokens/s
tail -f ~/.lmstudio/server-logs/$(date +%Y-%m)/$(date +%F).1.log   # LM Studio server log
```

---

## Where everything is saved

| Location | Contents | In git? |
|---|---|---|
| `data/processed/nse_delisted_master.csv` / `.jsonl` | **The dataset**, all years | Yes |
| `data/processed/priority_years_audit.csv` | Priority-year rows, with `source_count` and `all_source_urls` | Yes |
| `data/processed/proposed_delisting_leads.csv` | Companies NSE has *proposed* for delisting, to be verified | Yes |
| `data/metadata/collection_metadata.json` | **Metadata of everything done**: indicators, queries per phase, fetches per domain/status/type, artifacts, records by year/type/confidence/regime, field completeness, run history, agent-session stats | Yes |
| `data/metadata/artifacts.csv` | One row per downloaded file: source URL, domain, type, size, sha256, rows extracted | Yes |
| `data/metadata/dataset_audit.json` | Data-quality checks on the dataset (errors and warnings, with examples) | Yes |
| `data/metadata/collection_state.json` | Status of each query (done, no_results or failed) and the blocked domains. This is what makes runs resumable. | Yes |
| `logs/runs/<run_id>.json` | Per-run summary: duration, queries, URLs, fetched/quarantined/harvested, records before → after | Yes |
| `logs/fetch_manifest.jsonl` | Every download attempt: timestamp, url, http_status, sha256, local_path, year_tag | Yes |
| `logs/events.jsonl` | Every action as JSON (search, fetch, harvest, parse, merge, run_start/end, agent tool calls), tagged with `run_id` and `agent_session` | No (large) |
| `logs/agent_run.log` | The same events as readable lines | No |
| `logs/agent_sessions/<session>.jsonl` | Cline's full raw event stream (its reasoning, tool calls, outputs, token usage) | No (large) |
| `data/raw/master_lists/`, `priority_<year>/`, `archive_2006_2026/` | Downloaded documents | No (can be regenerated) |
| `data/search_cache/`, `data/quarantine/`, `data/processed/parsed/` | Search results, failed downloads, per-file candidate rows | No |

To trace any record back to its source: `source_url` → `local_artifact_path` → the manifest entry (with its sha256) → `artifacts.csv`.

---

## Configuration

| Item | Where | Notes |
|---|---|---|
| Query plan | `config/query_plan.json` | Phases, queries, pages per query, `fetch_domains`, `fetch_limit_per_query`, `harvest_limit_per_page`, `block_domain_after_failures`. The agent adds queries to an `agent_added` phase. |
| Agent task | `prompts/autonomous_collection.md` | The loop, stop conditions and hard rules for unattended runs |
| Agent rules | `.cline/rules/01-nse-research.md` | Loaded automatically by Cline for every task |
| Agent persona | `soul.md` | Accuracy over volume, never invent fields, every row cites a source |
| Skills | `skills/` (linked as `.cline/skills`) | `searxng-discovery`, `nse-document-parser`, `dataset-auditor` |
| Hidden from agent | `.clineignore` | `vendor/`, `.venv/`, raw data, caches, quarantine, parsed rows |
| Trusted domains | `scripts/common.py` | `PRIMARY_DOMAINS` (NSE, SEBI, BSE → `VERIFIED_PRIMARY`), `SECONDARY_DOMAINS` (news and legal sites → `CORROBORATED_SECONDARY`) |
| Parser rules | `scripts/parse_nse_records.py` | `HEADER_MAP`, `TYPE_KEYWORDS`, `rows_from_order`, `PROPOSED_RE` |
| LM Studio | `scripts/run_agent.sh` | Cline `lmstudio` provider, `localhost:1234/v1`, model from `CLINE_MODEL` |
| Cline CLI | `package.json` | Pinned to `3.0.75`, with npm `overrides` for vulnerable dependencies (`npm audit`: 0 critical). **Never run `npm audit fix --force`**, because it downgrades Cline to 3.0.3. |
| SearXNG | Docker container `searxng` | `search.formats: [html, json]` added. The original config is backed up as `settings.yml.bak`. |

---

## Scripts

| Script | Purpose |
|---|---|
| `scripts/run_agent.sh` | Launch Cline unattended. Checks that SearXNG and LM Studio are up, logs the session, prints the dashboard at the end. |
| `scripts/collect.py` | The resumable pipeline run (search → fetch → harvest → parse → merge → metadata) |
| `scripts/status_report.py` | Dashboard: indicator bars, year × type grid, gaps. Supports `--watch N`, `--json`, `--year`, `--needs-review`. |
| `scripts/build_metadata.py` | Rebuilds `data/metadata/` (runs automatically) |
| `scripts/searxng_client.py` | One search, cached. Falls back to HTML if JSON is disabled. |
| `scripts/polite_fetcher.py` | Download URLs, a search cache file, or links harvested from a page (`--harvest`) |
| `scripts/parse_nse_records.py` | Extract candidate rows from HTML, PDF, XLSX and CSV files |
| `scripts/normalize_and_merge.py` | Normalize, dedupe, write the master, priority-audit and leads CSVs |
| `scripts/inspect_artifact.py` | Bounded excerpt of a downloaded file, or `--zero-rows` to list files that gave no rows |
| `scripts/agent_progress.py` | Turns Cline's JSON stream into the readable terminal lines |
| `scripts/audit_dataset.py` | Data-quality audit (duplicate ids, bad ISINs, off-topic sources, missing artifacts, no-date rows, suspicious names) |
| `scripts/check.sh` | Repo health check: pyflakes, pytest, configs, audit, disk, stale references, services |
| `scripts/common.py` | Paths, logging and events, domain rules. `python scripts/common.py log "msg"` adds a milestone. |

---

## Output schema

Column definitions are in the root [`nse_delisted_master.csv`](nse_delisted_master.csv).

| Column | Notes |
|---|---|
| `record_id` | sha256(ISIN or normalized name \| date \| type), first 16 hex characters. Stable across re-runs. |
| `company_name`, `nse_symbol`, `isin` | Symbol is uppercased, `UNKNOWN` if absent. ISIN matches `IN[A-Z0-9]{9}[0-9]`. |
| `delisting_type` | `COMPULSORY`, `VOLUNTARY_EXIT_OFFER`, `VOLUNTARY_NO_EXIT`, `AMALGAMATION_MERGER`, `IBC_NCLT` or `OTHER` |
| `order_date`, `effective_delisting_date` | ISO `YYYY-MM-DD`. Indian sources are read day-first. |
| `calendar_year`, `financial_year` | Taken from the effective date, falling back to the order date. FY is written like `FY2017-18`. |
| `is_priority_year` | `true` when the calendar year or the order year is 2007, 2017, 2020 or 2024 |
| `regulatory_regime` | Set by date: `SEBI_2003_Guidelines` before 2009-06-10, `SEBI_2009_Regs` before 2021-06-10, `SEBI_2021_Regs` before 2024-09-25, `SEBI_2024_Amendment` after that |
| `exit_or_fair_value_inr` | Exit price or fair value, if the document states one |
| `source_url`, `local_artifact_path` | Where the row came from |
| `confidence_status` | `VERIFIED_PRIMARY` (an official NSE, SEBI or BSE document), `CORROBORATED_SECONDARY` (a reputable source, or two or more independent domains), or `NEEDS_REVIEW` |

Don't edit the processed files or the metadata by hand. Fix the inputs or the parsing rules and re-run the scripts.

---

## Proposed uses for this dataset

None of these is built yet. They are possible next steps.

**Research**
1. **Survivorship-bias-free backtesting.** Join delisted ISINs and symbols back into historical NSE universes so returns aren't overstated. Each `effective_delisting_date` marks where a stock's price series ends.
2. **Regulatory event studies.**
   - *2007*: the last years of the SEBI 2003 Guidelines.
   - *2017*: the size of the shell-company crackdown.
   - *2020*: IBC/NCLT-driven delistings and the COVID relaxations.
   - *2024*: whether the fixed-price route and counter-offer changes moved voluntary delistings.
3. **Regime comparison.** Group by `regulatory_regime` × `delisting_type`.
4. **Investor recovery.** Compare `exit_or_fair_value_inr` with the last traded price (from bhavcopy data).
5. **IBC outcome tracking.** Link `IBC_NCLT` rows to IBBI and NCLT orders.
6. **Early-warning signals.** Test whether suspension or non-compliance predicts compulsory delisting. `proposed_delisting_leads.csv` is a ready-made list of companies to watch.

**Engineering:** DuckDB/Parquet export, a bhavcopy price join, a BSE cross-listing pass, analysis notebooks, a Streamlit dashboard, OCR for scanned PDFs, a scheduled `launchd` refresh, and `pytest` checks on the processed CSV.

---

## Layout

```text
.
├── instructions.md, soul.md, README.md, AUDIT.md
├── nse_delisted_master.csv          # schema reference
├── requirements.txt, requirements-dev.txt, package.json   # Python deps; pytest/pyflakes; Cline CLI
├── tests/                           # pytest suite (offline)
├── config/query_plan.json           # what to search, in order
├── prompts/autonomous_collection.md # unattended agent task
├── .cline/rules/, .cline/skills → skills/, .clineignore
├── skills/                          # agent skill guides
├── scripts/                         # pipeline + agent launcher
├── data/
│   ├── search_cache/  raw/  quarantine/
│   ├── processed/                   # dataset, priority audit, leads, parsed/
│   └── metadata/                    # collection metadata, artifact inventory, dataset audit, resumable state
├── logs/                            # fetch_manifest, events, agent_run.log, runs/, agent_sessions/
└── vendor/cline/                    # reference clone (git-ignored)
```

## Known limitations

- Many `nseindia.com` pages load their content with JavaScript, so their HTML holds no table data. Harvesting gets around this by following the document links they contain (PDF/XLSX on `nsearchives.nseindia.com`).
- Single-company order PDFs usually have no ISIN, so `isin` stays empty until another source (a master list or a BSE notice) supplies it.
- Scanned (image-only) PDFs are flagged as `no_text` and need OCR or manual review.
- Search results sometimes point to `nseindia.in`, which is not the official domain. Rows from it stay `NEEDS_REVIEW`.
- The parser's heuristics are tuned on the formats seen so far. The agent extends them during runs, so check a sample of rows from each new format.
