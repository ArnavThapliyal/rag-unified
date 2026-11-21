You are running UNATTENDED. Nobody will answer questions — never ask, decide and log the decision.
Read `soul.md` and `.cline/rules/01-nse-research.md` first, then work through this loop.

IMPORTANT: shell commands are killed after 30 seconds. Collection runs take minutes, so ALWAYS run them
through the job runner: `.venv/bin/python scripts/job.py start <collect.py args>` (returns immediately), then
repeat `.venv/bin/python scripts/job.py wait` (blocks ≤25 s, prints progress) until `"running": false`, then read
`result`. Never run `collect.py` without `--dry-run` directly, and never use `&`, `nohup` or /tmp logs.

## Loop (repeat until a stop condition)
1. `.venv/bin/python scripts/collect.py --dry-run` — see pending queries and the harvest backlog.
2. If `harvest_backlog` > 0: `.venv/bin/python scripts/job.py start --harvest-only --max-harvest 40`, then
   `job.py wait` until done (repeat this step until the backlog is 0 or nothing new is fetched).
3. Else if queries are pending: `.venv/bin/python scripts/job.py start --max-queries 8`, then `job.py wait` until done.
4. `.venv/bin/python scripts/status_report.py --json` and decide the next action:
   - `artifacts_zero_rows` grew → `.venv/bin/python scripts/inspect_artifact.py --zero-rows`, inspect up to 3
     with `scripts/inspect_artifact.py <path> --lines 40`. If a new document layout is clearly a list of
     delisted companies or a delisting order, improve `HEADER_MAP` / `TYPE_KEYWORDS` / `rows_from_order`
     in `scripts/parse_nse_records.py`, run `scripts/check.sh --offline` (must pass), then
     `parse_nse_records.py --force` and `normalize_and_merge.py` (if a step exceeds 30 s, it is killed: re-run it —
     parsing skips files already done unless --force).
     Log what you changed.
   - Priority-grid gaps (year × type with 0 records) after the plan for that year is done → add 2–4
     targeted queries to a phase named `agent_added` at the END of `config/query_plan.json`
     (use the discovery skill templates; set `year_tag`). Never delete existing queries.
   - `data/processed/proposed_delisting_leads.csv` has companies with `already_in_master = False` →
     add queries like `site:nseindia.com "<company name>" delisting order` to `agent_added` (max 10 per cycle).
   - `indicators.audit_errors` > 0 → `.venv/bin/python scripts/audit_dataset.py`, find which parser rule
     produced the bad rows, fix the rule (never the CSV), reparse and merge.
   - A `disk_warning` appears in the output → mention it in your milestone log line; keep going.
5. Log a one-line milestone: `.venv/bin/python scripts/common.py log "cycle N: <what you did>, records X"`.

## Stop conditions (then write a final summary log line and finish)
- All plan phases complete, harvest backlog 0, and the last two cycles added 0 new records; or
- SearXNG unavailable (collect.py returns `searxng_unavailable` / `searxng_errors`) twice in a row; or
- collect.py returns `stop_reason: low_disk_space` (the user must free disk space — stop immediately); or
- 25 cycles completed.

## Hard rules
- Never hand-edit files in `data/processed/` or `data/metadata/`; never delete `data/` or `logs/`.
- Never bypass blocks (no headless browsers, no WAF tricks, no removing delays). Blocked domains stay blocked.
- Don't open raw files directly; use `inspect_artifact.py` (bounded output).
- Keep your messages short: counts and file paths only.
