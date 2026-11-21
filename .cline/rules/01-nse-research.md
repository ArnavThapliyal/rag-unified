# NSE Delisting Research — Workspace Rules

Read `soul.md` once at the start of a task. Full spec: `instructions.md`.

## Environment
- Python: always use `.venv/bin/python` (run from the workspace root).
- Search: SearXNG at `http://localhost:8082` (JSON enabled). Only reach it through the scripts.
- Never open files under `data/raw/`, `data/search_cache/`, `data/quarantine/`, or `vendor/` directly
  (they are in `.clineignore`). Use `scripts/inspect_artifact.py <path>` for a bounded excerpt.

## Shell commands are killed after 30 s
Run every collection through the job runner (detached, survives the timeout):
- `.venv/bin/python scripts/job.py start <collect.py args>` → returns at once
- `.venv/bin/python scripts/job.py wait` → waits ≤25 s, prints progress; repeat until `"running": false`
- `.venv/bin/python scripts/job.py status` / `stop`

## Main command: autonomous collection
`scripts/collect.py` runs the whole pipeline from `config/query_plan.json` and is resumable:
- `.venv/bin/python scripts/collect.py --dry-run` — pending queries per phase + harvest backlog
- `job.py start --max-queries 8` — next 8 pending queries (search → fetch → harvest linked docs → parse → merge)
- `job.py start --phase priority_2017` — one phase
- `job.py start --harvest-only --max-harvest 40` — fetch documents linked from already-downloaded official pages
- `.venv/bin/python scripts/status_report.py --json` — indicators (incl. `disk`, `audit_errors`) + year × type grid
- `.venv/bin/python scripts/audit_dataset.py` — data-quality audit of the dataset
- `scripts/check.sh --offline` — after editing any script: pyflakes + pytest must pass

Each run writes `logs/runs/<run_id>.json` and refreshes `data/metadata/`. Read the JSON summary it prints; don't tail raw logs.

## Individual steps (for debugging / one-off work)
1. Discover — `scripts/searxng_client.py --query "<q>" --pages 2 --year-tag 2017` (skill: searxng-discovery)
2. Fetch — `scripts/polite_fetcher.py --from-cache-file <file> --limit 10 --domains nseindia.com,sebi.gov.in,bseindia.com`, `--url <url>`, or `--harvest <downloaded html>`
3. Parse — `scripts/parse_nse_records.py` (`--force` to reparse; skill: nse-document-parser)
4. Merge — `scripts/normalize_and_merge.py` (skill: dataset-auditor)
5. Metadata — `scripts/build_metadata.py`

## Priority
- Priority years: **2007, 2017, 2020, 2024** — the plan runs master lists, then these, then the archive years.
- Year tags: `2007|2017|2020|2024` → `data/raw/priority_<yr>/`; `master` → `data/raw/master_lists/`; other years → `data/raw/archive_2006_2026/`.
- Lists of companies *proposed* to be delisted are leads (`data/processed/proposed_delisting_leads.csv`), not records.

## Hard rules
- No WAF/Akamai bypass, no headless-browser scraping, no removing delays. Blocked domains stay blocked.
- Do not hand-edit `data/processed/*` or `data/metadata/*`. Re-run scripts instead.
- To add searches, append to a phase named `agent_added` in `config/query_plan.json`; never delete queries.
- If collect.py stops with `low_disk_space`, stop working and log it — the user has to free space.
- Log milestones: `.venv/bin/python scripts/common.py log "<what happened>"`.
- If a script fails twice with the same error, log it and move on to a different step (or stop if unattended work can't continue).
- Keep answers short: report counts and file paths, not document contents.
