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
- `.venv/bin/python scripts/job.py wait 1` → waits ≤25 s, prints progress and a `next` command (`wait 2`, `wait 3`, …);
  run `next` until `"running": false`. Don't repeat an identical command 5× in a row: Cline aborts that as a loop.
- `.venv/bin/python scripts/job.py status` / `stop`

## Main command: autonomous collection
`scripts/collect.py` runs the whole pipeline from `config/query_plan.json` and is resumable:
- `.venv/bin/python scripts/collect.py --dry-run` — pending queries per phase + harvest backlog
- `job.py start --max-queries 8` — next 8 pending queries (search → fetch → harvest linked docs → parse → merge)
- `job.py start --phase priority_2017` — one phase
- `job.py start --harvest-only --max-harvest 40` — fetch documents linked from already-downloaded official pages
- `.venv/bin/python scripts/status_report.py --json` — indicators (incl. `disk`, `audit_errors`) + year × type grid
- `.venv/bin/python scripts/audit_dataset.py` — data-quality audit of the dataset
- `.venv/bin/python scripts/report_issue.py --kind <kind> --summary "<what is wrong, with counts>" --evidence <path>` —
  report an anomaly for the user; `--list` shows open issues

Each run writes `logs/runs/<run_id>.json` and refreshes `data/metadata/`. Read the JSON summary it prints; don't tail raw logs.

## You collect; the user debugs
Never edit `scripts/`, `tests/`, `skills/`, `prompts/`, `.cline/` or `soul.md`, and never write ad-hoc
`python -c` probes of the parser or the data. When something looks wrong, report it with
`report_issue.py` (at most 3 `inspect_artifact.py` looks for evidence) and carry on collecting.
The only file you may edit is `config/query_plan.json` (append to the `agent_added` phase).

## Individual steps (reference — the user's debugging tools; you don't need them)
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
- If collect.py stops with `search_engines_suspended`, don't retry searches (it extends the ban): finish any
  harvest backlog with `--harvest-only`, log it, and stop. `run_agent.sh` restarts after the cooldown.
- Log milestones: `.venv/bin/python scripts/common.py log "<what happened>"`.
- If a script fails twice with the same error, report it (`--kind script_error`) and stop if collection can't continue.
- Keep answers short: report counts and file paths, not document contents.
