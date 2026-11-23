# NSE Delisted Companies Archive

A local-first research pipeline for building a verified, source-cited dataset of companies delisted from the **National Stock Exchange of India (NSE)**, 2006–2026. Four years get deep coverage: **2007, 2017, 2020, 2024**.

The **Cline** agent runs unattended on a local model (LM Studio, `qwen/qwen3.8-27b`). Python scripts do all the searching, fetching, parsing and merging, and they log every step. The agent reads only short JSON summaries, decides what to do next, and keeps going without asking for approval.

Full specification: [`instructions.md`](instructions.md). Cleanup audit: [`AUDIT.md`](AUDIT.md).

---

## How it works

```text
config/query_plan.json  (phases: master → 2017 → 2020 → 2024 → 2007 → archive)
          │
          ▼  scripts/collect.py  (one resumable run; Cline calls it in a loop) — three stages side by side
  ┌─ Discovery (collect.py) ─────┐   ┌─ Fetching (fetch_worker.py) ──┐   ┌─ Processing (process_worker.py) ─┐
  │ search providers             │   │ best items first, each domain │   │ parse tables / ISIN lines /      │
  │ → data/search_cache/         │   │ at its own pace (NSE: slow)   │   │ orders, OCR scanned PDFs         │
  │ file candidate URLs +        │──▶│ → data/raw/<bucket>/          │──▶│ → data/processed/parsed/         │
  │ evidence (query, rank,       │   │ failures retried w/ backoff;  │   │ merge: dedupe, FY, SEBI regime,  │
  │ snippet) in the docket       │   │ linked docs on pages filed    │   │ confidence → data/processed/*.csv│
  └──────────────────────────────┘   └───────────────────────────────┘   └──────────────────────────────────┘
                 └────────── persistent docket: data/docket.sqlite (scripts/docket.py) ──────────┘
          │  every step → logs/events.jsonl, logs/agent_run.log, logs/runs/<run_id>.json; metadata → data/metadata/
          ▼
  Cline reads the summary → drains the docket / runs more queries / adds gap queries / REPORTS anomalies
```

**The agent collects; you debug.** A 27B local model is good at running the loop but not at repairing the
pipeline, so it is not allowed to edit code (`scripts/`, `tests/`, `skills/`, prompts, rules) or probe the
parser with ad-hoc Python. When something looks wrong (relevant documents with 0 rows, junk names, audit
errors, a blocked domain, a crash) it files a report with `scripts/report_issue.py` and keeps collecting.
It stops only if a script crashes twice in a row. Review the reports with:

```bash
.venv/bin/python scripts/report_issue.py --list      # open issues; full detail in logs/issues.jsonl
```

The only file the agent edits is `config/query_plan.json`, to append searches to an `agent_added` phase.

**Safeguards built in:**

- **Polite fetching.** Waits a random 2–5 s between requests, backs off on 429/5xx, and moves failed downloads to `data/quarantine/`.
- **Fetch once.** A URL that downloaded successfully, or failed with a definitive 4xx error, is never requested again.
- **Blocked domains.** A domain that refuses 3 times in a row (403, 429, 5xx, or a 200 "Access Denied"/HTML page served for a document) is blocked for 6 h, then 24 h, then 72 h; blocks expire and the domain is tried again. Timeouts and connection errors never block a domain: if the network itself is down (errors on 2+ domains in a row) the run stops with `network_unavailable` and the attempts are given back. 403/429 are retried with long backoff (2 h, 8 h, then daily); 404/410 are dead at once. Downloads over `max_download_mb` (default 40) are refused while streaming. There's no WAF bypass.
- **Resumable.** Searched queries are tracked in `data/metadata/collection_state.json` and every candidate URL in the docket (`data/docket.sqlite`), so you can stop at any time (Ctrl-C) and re-run.
- **Discovery is separate from downloading.** A search files its results in the docket and moves on; the fetch worker downloads them in parallel at each domain's pace, and the process worker parses / OCRs in parallel too. Slow sites or OCR never hold up searching, and a failed download is retried from the docket (30 min, 2 h, 8 h, then `dead`) without searching again. Results beyond the first `fetch_limit_per_query` (8) per query are kept as *parked* evidence (`scripts/docket.py --promote` to fetch them).
- **Retries after throttling.** Empty search results are not cached, so a query can be retried with `--retry-failed` if SearXNG's upstream engines were throttling it.
- **Proposed delistings kept out of the dataset.** NSE's "companies proposed to be delisted" lists go to `proposed_delisting_leads.csv` as companies to verify later.
- **Off-topic documents rejected.** Only documents about delisting produce rows. Pages about trading members, brokers and defaulters, and procedural files (checklists, SOPs, forms), are skipped.
- **Disk guard.** Warns when `data/` passes 5 GB and again every +5 GB (terminal line, log entry and macOS notification). The run stops cleanly if free space drops below 20 GB. Adjust under `disk` in `config/query_plan.json`.

---

## Setup (one-time)

```bash
git clone https://github.com/ArnavThapliyal/nse-delisted-comp.git
cd nse-delisted-comp && ./install.sh
```

`install.sh` checks the prerequisites (Python 3.9+, and warns about missing git, Docker, npm, Homebrew or
LM Studio), then starts a guided setup (`scripts/setup.py`, Python standard library only). Move through
9 screens with the arrow keys; **the first option on every screen is the recommended one**, so Enter all
the way through gives a working setup. The panel under the options shows exactly what the highlighted
option will do and what is already in place.

| Step | Recommended (first option) | Others |
|---|---|---|
| 1 · Python environment | create `.venv`, install `requirements.txt` | reuse `.venv` · skip |
| 2 · Developer tools | install `requirements-dev.txt` (pytest, pyflakes) | skip |
| 3 · SearXNG search | `docker compose up -d`, random secret written to `.env` | already running at :8082 · skip |
| 4 · Search API keys | save to `~/.config/nse-delisted-comp/keys.env` (outside the repo, mode 600) | repo `.env` · skip |
| 5 · OCR | install tesseract | skip |
| 6 · Agent | `npm install` (Cline pinned to 3.0.75) | skip |
| 7 · Agent model | `qwen/qwen3.8-27b` (saved as `CLINE_MODEL` in `.env`) | `google/gemma-4-26b-a4b` · keep current |
| 8 · Run limits | capped: 60 files, 25 companies, 5 docs/company per run | unlimited · ask at the start of each run |
| 9 · Finish | run `scripts/check.sh` | skip |

Nothing changes until you confirm the summary. Steps already done are skipped, `.env` and the key file are
backed up (`.bak`, `.bak1`, …) before they're written, an existing `SEARXNG_SECRET` is never replaced, and
API keys are typed hidden and never printed (keys are refused for a `.env` tracked by git).
**↑ / ↓** move · **⏎ / →** next · **← / b** back · **q** quit. Without a terminal UI it falls back to numbered
prompts (blank = recommended).

```bash
./install.sh --defaults    # accept every recommended option, no questions (no key entry)
./install.sh --dry-run     # show what would happen, change nothing
```

<details>
<summary>Manual setup (what the wizard does)</summary>

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python pipeline
.venv/bin/pip install -r requirements-dev.txt                         # pytest + pyflakes
npm install                                                           # Cline CLI (pinned 3.0.75)
brew install tesseract                                                # OCR for scanned PDFs

# SearXNG search (Docker): settings come from searxng/settings.yml, the secret from .env
cp .env.example .env && sed -i '' "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=$(openssl rand -hex 32)/" .env
docker compose up -d
curl -s "localhost:8082/search?q=test&format=json" | head -c 80          # expect {"query": "test", ...

# Optional reference copy of Cline's source (git-ignored)
git clone --depth 1 https://github.com/cline/cline vendor/cline
```

</details>

Requirements:

- **SearXNG** (Docker) at `http://localhost:8082` with JSON output enabled. `docker compose up -d` starts it from `docker-compose.yml` (image pinned, port bound to 127.0.0.1) with `searxng/settings.yml` mounted read-only: JSON output plus the extra engines the rotation in `config/query_plan.json` uses. Edit that file and run `docker compose restart` to change engines; the secret key stays in the git-ignored `.env`.
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
  ▶ [3] run_commands: .venv/bin/python scripts/job.py start --harvest-only --max-files 40
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

> **Note:** Cline kills any shell command after 30 seconds (its `bashTimeoutMs`, which the CLI can't change). The agent therefore starts collections with `scripts/job.py start …` and polls `scripts/job.py wait 1`, `wait 2`, …, so long runs survive the timeout. The counter is there because Cline's loop detection aborts a session after 5 identical tool calls in a row. `run_agent.sh` also restarts a session whose run didn't finish `completed` (aborted, mistake limit), not just one that exited non-zero.

**Without the agent** (deterministic, no LLM), `collect.py` runs the plan by itself:

```bash
.venv/bin/python scripts/collect.py --dry-run          # what's pending + docket backlog
.venv/bin/python scripts/collect.py --harvest-only --max-files 60   # drain the docket: fetch + parse, no searching
.venv/bin/python scripts/collect.py --stage discover --max-queries 5  # search only: fill the docket
.venv/bin/python scripts/docket.py --stats             # queue: ready / retrying / awaiting parse / parsed / dead / parked
.venv/bin/python scripts/docket.py --list failed       # what failed and why (also: dead, parse_error, parked)
.venv/bin/python scripts/docket.py --retry dead --domain example.com   # requeue after fixing something
.venv/bin/python scripts/docket.py --promote --query "2017" --limit 20  # fetch parked results too
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

### Run limits and modes

Each run has three caps, set in `run_limits` in `config/query_plan.json` (the setup wizard's "Run limits" screen)
or per run with flags. `0`, `null` or `all` means no limit for that cap.

| Cap | Counts | Stop reason |
|---|---|---|
| `max_files` (60) | files downloaded OK in this run | `max_files_reached` |
| `max_companies` (25) | new records in the master in this run | `max_companies_reached` |
| `max_docs_per_company` (5) | documents fetched per company, all runs; extra ones stay *parked* in the docket | — |

`mode` is `capped` (use the caps), `unlimited` (no caps: run until the queries, the docket or the sources are used
up) or `ask` (asks "capped or unlimited?" at the start of a run, but only in a terminal; the agent, `job.py` and
cron runs use the caps, so unattended runs never block).

```bash
.venv/bin/python scripts/collect.py --unlimited                       # no caps this run
.venv/bin/python scripts/collect.py --max-files 200 --max-companies all
.venv/bin/python scripts/collect.py --capped                          # the config's caps, even in ask mode
```

Every run ends with one `stop_reason`: a cap (`max_files_reached`, `max_companies_reached`,
`max_queries_reached`), or how it ran out: `queries_exhausted` (no searches left), `docket_exhausted` (nothing
left to download), `sources_exhausted` (downloads left, but each is over its site's per-run budget, on a blocked
site or waiting to retry); or `search_engines_suspended`, `low_disk_space`, `keyboard_interrupt`. The run summary
also keeps each stage's own reason (`discovery_stop_reason`, `fetch_stop_reason`, `process_stop_reason`).

### Historical focus: delisted 15–20 years ago

`focus.years_ago` ([15, 20]) sets the target: today that is companies delisted in 2006–2011
(`scripts/historical.py`). Those come before everything else in the plan, in two steps:

1. **Proof first.** `historical_proof`: for each window year, NSE / BSE / SEBI delisting orders, notices and
   lists. `historical_company_proof`: for each company in the window whose records have no official or archive
   source yet, `"<name>" delisted <year>` on NSE, BSE, SEBI, and its ISIN.
2. **Then supporting data.** `historical_company_support`, for proven companies: the delisting public
   announcement / exit offer, the reason-specific document (compulsory delisting order, amalgamation scheme,
   liquidation order) and the last annual report before delisting. These documents are tagged with the company
   in the docket and capped by `max_docs_per_company`.

Company queries are generated from the master on every run, so newly found companies get their own proof and
support queries. Search results are re-ranked before the per-query cut, and the docket fetches in this order:
official and archive records, then other sites, news last; documents and window years get a boost. Official URLs
from the window that are gone (404 / 410) are looked up in the Wayback Machine and the archived copy is fetched
instead. The dashboard's "Historical focus" line shows companies in the window, proven / unproven, and how many
have supporting documents.

### Disk-space warnings

You'll see `!! DISK WARNING: data/ has grown to 5.1 GB …` in the terminal and get a macOS notification. When that happens, free up space, or raise `disk.warn_data_gb` in `config/query_plan.json` to keep going. If free space drops below `disk.min_free_gb` (20 GB), the run stops with `stop_reason: low_disk_space`, the agent finishes, and `run_agent.sh` won't start again until there's room. The dashboard always shows a `Disk:` line. The biggest folder is `data/raw/`, which you can delete once records are extracted (the dataset keeps `source_url` for every row).

### Search providers, rotation and bans

Searches go through pluggable providers ([`scripts/search_providers/`](scripts/search_providers/)), used in the order of `search.providers` in `config/query_plan.json`:

| Provider | What it is | Key | Default limit |
|---|---|---|---|
| `searxng` | Local SearXNG (`docker compose up -d`); rotates its upstream engines | none | none |
| `serper` | [Serper.dev](https://serper.dev) Google results API | `SERPER_API_KEY` | 50 queries/day |
| `brave` | [Brave Search API](https://api-dashboard.search.brave.com) | `BRAVE_API_KEY` | 50/day, 1900/month |

The next provider is used only when the previous one is used up: all its units benched, its daily or monthly limit reached (`provider_settings.<name>.daily_limit` / `monthly_limit`), no key, or down. A clean "no results" answer is final, so paid APIs are not spent on it. A provider without a key is skipped, so the repo works with SearXNG alone.

**API keys** are never in code or config. `scripts/credentials.py` looks them up in this order: the environment, the file named by `NSE_KEYS_FILE`, `~/.config/nse-delisted-comp/keys.env` (outside the repo, recommended), then the git-ignored `.env` (template: `.env.example`). A `.env` that is tracked by git is refused. `run_agent.sh` strips `*API_KEY*` variables from the agent's environment, so for agent runs put keys in one of the files: the pipeline reads them, the agent can't. The tool guard (`scripts/tool_guard.py`) is an allowlist grammar, not a blacklist: every command is parsed into a plan of agent scripts with their exact allowed flags plus stdin-only filters, anything bash would expand (`$`, `~`, globs, backslashes) is refused, and every allowed call is rewritten to `scripts/agent_exec.py`, which re-validates it and runs it without a shell (filters are done in Python). The agent can't edit any file; the plan changes only through `scripts/add_queries.py`, and run caps can only be tightened.

```bash
mkdir -p ~/.config/nse-delisted-comp && chmod 700 ~/.config/nse-delisted-comp
printf 'SERPER_API_KEY=...\nBRAVE_API_KEY=...\n' > ~/.config/nse-delisted-comp/keys.env
chmod 600 ~/.config/nse-delisted-comp/keys.env
.venv/bin/python scripts/web_search.py --check    # each provider: ready or why not; where its key was found (never the key)
```

**Rotation.** Each provider has *units* that are benched independently: SearXNG's are its engines, an API is one unit.

- Each SearXNG request asks only `engines_per_query` (2) engines, the ones rested longest; if they return nothing it tries the next pair once.
- A unit that answers *too many requests* is benched 60 min, *CAPTCHA* 6 h, *access denied* (or a rejected API key) 12 h, *quota* exhausted 24 h, other errors 2 h (`search.cooldown_min`).
- A unit whose answers contain no relevant result 3 times in a row (empty, or junk like Bing returning gaming forums) is benched 6 h.
- Queries are 20–40 s apart.
- When every provider is used up, `collect.py` stops with `search_engines_suspended` and the query stays pending; `run_agent.sh` waits until the first one is back (`search_cooldown_min_left` in `collect.py --dry-run`) and starts a fresh session, up to `CLINE_MAX_COOLDOWNS` (12) times. Harvest-only runs keep working.

SearXNG engines are enabled in [`searxng/settings.yml`](searxng/settings.yml) (`engines:` list; `docker compose restart` after editing). Their bans are per IP: if all engines are banned at once, only an API provider or waiting helps.

```bash
.venv/bin/python scripts/web_search.py --engines   # per provider: usable / benched units (+ reason, minutes left), usage today / this month
```

**Adding a provider** is one file in `scripts/search_providers/` that subclasses `SearchProvider`, sets `name` and `key_names`, implements `search()` and is decorated with `@register`. Then add its name to `search.providers` and its key to your key file. The module docstring of [`search_providers/__init__.py`](scripts/search_providers/__init__.py) has a complete example; `SearchProvider.http_failure()` maps 429 / 401 / quota responses to the reasons above.

### Other things to watch

```bash
tail -f logs/agent_run.log                                   # every script action, human-readable
tail -f logs/events.jsonl                                    # same, structured (JSON per line)
tail -f logs/issues.jsonl                                    # anomalies the agent reported
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
| `data/metadata/collection_state.json` | Status of each query (done, no_results or failed). | Yes |
| `data/docket.sqlite` | The docket: every candidate URL with its evidence (query, provider, rank, snippet), status, attempts, errors, local file, rows parsed; blocked domains. | No (rebuildable: `docket.py` imports the manifest and search cache when it is missing) |
| `data/metadata/docket_stats.json` | Docket summary after each run | Yes |
| `logs/runs/<run_id>.json` | Per-run summary: duration, queries, URLs, fetched/quarantined/harvested, records before → after | Yes |
| `logs/fetch_manifest.jsonl` | Every download attempt: timestamp, url, http_status, sha256, local_path, year_tag | Yes |
| `logs/events.jsonl` | Every action as JSON (search, fetch, harvest, parse, merge, run_start/end, agent tool calls), tagged with `run_id` and `agent_session` | No (large) |
| `logs/agent_run.log` | The same events as readable lines | No |
| `data/processed/ocr/` | OCR text of scanned PDFs, one `.txt` per file (cache) | No (regenerable) |
| `data/processed/out_of_scope_delistings.csv` | Delistings dated outside 2006–2026 (kept, not in the master) | Yes |
| `data/processed/record_sources.json` | Which parsed sources back each `record_id` (keeps ids stable) | Yes |
| `data/processed/bse_only_delistings.csv` | Companies delisted from BSE that appear in no NSE source and have no NSE symbol (many were never NSE-listed). Kept out of the master; same columns as the audit file | Yes |
| `logs/issues.jsonl` | Anomalies reported by the agent (`report_issue.py`): kind, summary, evidence paths, times seen, open/closed | Yes |
| `logs/agent_sessions/<session>.jsonl` | Cline's full raw event stream (its reasoning, tool calls, outputs, token usage) | No (large) |
| `data/raw/master_lists/`, `priority_<year>/`, `archive_2006_2026/` | Downloaded documents | No (can be regenerated) |
| `data/search_cache/`, `data/quarantine/`, `data/processed/parsed/` | Search results, failed downloads, per-file candidate rows | No |

To trace any record back to its source: `source_url` → `local_artifact_path` → the manifest entry (with its sha256) → `artifacts.csv`.

---

## Configuration

| Item              | Where                                 | Notes                                                                                                                                                                                  |
| ----------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Query plan        | `config/query_plan.json`              | Phases, queries, pages per query, `fetch_domains`, `fetch_limit_per_query`, `harvest_limit_per_page`, `block_domain_after_failures`. The agent adds queries to an `agent_added` phase. |
| Agent task        | `prompts/autonomous_collection.md`    | The loop, stop conditions and hard rules for unattended runs                                                                                                                           |
| Agent rules       | `.cline/rules/01-nse-research.md`     | Loaded automatically by Cline for every task                                                                                                                                           |
| Agent persona     | `soul.md`                             | Accuracy over volume, never invent fields, every row cites a source                                                                                                                    |
| Skills            | `skills/` (linked as `.cline/skills`) | `searxng-discovery`, `nse-document-parser`, `dataset-auditor`                                                                                                                          |
| Hidden from agent | `.clineignore`                        | `vendor/`, `.venv/`, raw data, caches, quarantine, parsed rows                                                                                                                         |
| Trusted domains   | `scripts/common.py`                   | `PRIMARY_DOMAINS` (NSE, SEBI, BSE → `VERIFIED_PRIMARY`), `SECONDARY_DOMAINS` (news and legal sites → `CORROBORATED_SECONDARY`)                                                         |
| Parser rules      | `scripts/parse_nse_records.py`        | `HEADER_MAP`, `TYPE_KEYWORDS`, `rows_from_order`, `PROPOSED_RE`                                                                                                                        |
| LM Studio         | `scripts/run_agent.sh`                | Cline `lmstudio` provider, `localhost:1234/v1`, model from `CLINE_MODEL`                                                                                                               |
| Cline CLI         | `package.json`                        | Pinned to `3.0.75`, with npm `overrides` for vulnerable dependencies (`npm audit`: 0 critical). **Never run** `npm audit fix --force`, because it downgrades Cline to 3.0.3.           |
| SearXNG           | Docker container `searxng`            | `search.formats: [html, json]` added. The original config is backed up as `settings.yml.bak`.                                                                                          |

---

## Scripts

| Script | Purpose |
|---|---|
| `scripts/run_agent.sh` | Launch Cline unattended. Checks that SearXNG and LM Studio are up, logs the session, prints the dashboard at the end. |
| `install.sh`, `scripts/setup.py` | Guided first-time setup (recommended option first on every screen; `--defaults`, `--dry-run`) |
| `scripts/collect.py` | The resumable run: Discovery here, with the fetch and process workers alongside (`--stage all\|discover\|fetch\|process\|drain`) |
| `scripts/run_limits.py` | Caps and run modes (`run_limits` in the config, collect.py flags), the shared stop file |
| `scripts/historical.py` | Focus window, proof / supporting queries per company, Wayback lookups |
| `scripts/docket.py` | The persistent queue between the stages; `--stats`, `--list`, `--promote`, `--retry`, `--unblock` |
| `scripts/fetch_worker.py` | Downloads docket items, interleaving domains within their budgets and delays; files linked documents |
| `scripts/process_worker.py` | Parses / OCRs downloads in batches, merges records, marks docket items parsed |
| `scripts/ocr.py` | OCR for scanned PDFs (tesseract on pages rendered by pypdfium2, first 12 pages, cached). Used automatically by the parser; `python scripts/ocr.py <pdf>` prints the text. Needs `brew install tesseract`. |
| `scripts/report_issue.py` | Records an anomaly for you to review (`--kind`, `--summary`, `--evidence`; `--list` shows open ones). The agent's only response to problems. |
| `scripts/job.py` | Runs `collect.py` as a detached background job (`start`, `wait`, `status`, `stop`). The agent uses it because Cline kills shell commands after 30 s. |
| `scripts/status_report.py` | Dashboard: indicator bars, year × type grid, gaps. Supports `--watch N`, `--json`, `--year`, `--needs-review`. |
| `scripts/build_metadata.py` | Rebuilds `data/metadata/` (runs automatically) |
| `scripts/web_search.py` | One search through the providers, cached; `--engines` / `--check` show provider state. |
| `scripts/search_providers/` | Provider registry: `searxng`, `serper`, `brave`, plus any module you add. |
| `scripts/credentials.py` | Reads API keys from the environment or a key file (never from the repo). |
| `scripts/polite_fetcher.py` | Download URLs, a search cache file, or links harvested from a page (`--harvest`) |
| `scripts/parse_nse_records.py` | Extract candidate rows from HTML, PDF, XLSX and CSV files |
| `scripts/normalize_and_merge.py` | Normalize, reconcile one record per company event (conflicts recorded), write the master, priority-audit, out-of-scope, BSE-only and leads files (atomically) |
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
| `record_id` | sha256(ISIN or normalized name \| date \| type), first 16 hex characters, for a new record. Kept across re-runs: `data/processed/record_sources.json` remembers which sources backed each id, so a better source arriving for the same event does not change its id. |
| `company_name`, `nse_symbol`, `isin` | Symbol is uppercased, `UNKNOWN` if absent. ISIN: `INE`/`IN9` + 8 characters + a valid check digit (spaces and lower case are cleaned; an invalid ISIN is dropped). Field labels (`ISIN`, `Symbol: X`, CINs) are never company names. |
| `delisting_type` | `COMPULSORY`, `VOLUNTARY_EXIT_OFFER`, `VOLUNTARY_NO_EXIT`, `AMALGAMATION_MERGER`, `IBC_NCLT` or `OTHER` |
| `order_date`, `effective_delisting_date` | ISO `YYYY-MM-DD`. Indian `dd-mm-yyyy` sources are read day-first; ISO `yyyy-mm-dd` is never read day-first. |
| `calendar_year`, `financial_year` | Taken from the effective date, falling back to the order date. FY is written like `FY2017-18`. |
| `is_priority_year` | `true` when the calendar year or the order year is 2007, 2017, 2020 or 2024 |
| `regulatory_regime` | Set by date: `SEBI_2003_Guidelines` before 2009-06-10, `SEBI_2009_Regs` before 2021-06-10, `SEBI_2021_Regs` before 2024-09-25, `SEBI_2024_Amendment` after that |
| `exit_or_fair_value_inr` | Exit price or fair value, if the document states one |
| `source_url`, `local_artifact_path` | Where the row came from |
| `confidence_status` | `VERIFIED_PRIMARY` (an official NSE, SEBI or BSE document), `CORROBORATED_SECONDARY` (a reputable source, or two or more independent domains), or `NEEDS_REVIEW` (incl. every OCR'd source) |
| `source_count`, `all_source_urls`, `all_artifact_paths` | Every source merged into the record (best first) |
| `source_conflicts` | Sources that disagree, e.g. `effective_delisting_date: 2018-08-08 vs 2018-08-09 (archives.nseindia.com)` or `delisting_type: IBC_NCLT vs COMPULSORY`. The record keeps the most confident source's value. |
| `review_flags` | `<field>:needs_review_source` (a gap filled from a review-grade source such as OCR), `order_date_rejected:…` (a borrowed order date after the effective date or > 2 years before it), `ambiguous_company` (a name shared by two ISINs), `undated_unattached` |

**One record per delisting event.** Rows are one company when they share an ISIN, a normalized name or an NSE symbol (never joining two ISINs). Within a company, rows are one event when their effective dates are within 30 days (7 if two specific types disagree), or an order date precedes an effective date by up to 270 days. A company delisted twice years apart keeps two records; sources that disagree by months are not merged, and `audit_dataset.py` flags them (`possible_duplicate_event`) for a person to decide. Records dated outside 2006–2026 go to `data/processed/out_of_scope_delistings.csv`.

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
├── docker-compose.yml, searxng/settings.yml, .env.example   # local SearXNG (secret in git-ignored .env)
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
- Scanned (image-only) PDFs are OCR'd with tesseract (`scripts/ocr.py`, cached in `data/processed/ocr/`). OCR can misread names and dates, so rows from OCR are marked `NEEDS_REVIEW`; where NSE's own list has the same company, the verified row wins on merge.
- Search results sometimes point to `nseindia.in`, which is not the official domain. Rows from it stay `NEEDS_REVIEW`.
- The parser's heuristics are tuned on the formats seen so far. New formats show up as `zero_rows` issues from the agent; extending the parser is a manual step.
