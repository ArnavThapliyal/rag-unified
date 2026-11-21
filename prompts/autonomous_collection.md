You are running UNATTENDED. Nobody will answer questions — never ask, decide and log the decision.
Read `soul.md` and `.cline/rules/01-nse-research.md` first, then work through this loop.

## Loop (repeat until a stop condition)
1. `.venv/bin/python scripts/collect.py --dry-run` — see pending queries and the harvest backlog.
2. If `harvest_backlog` > 0: `.venv/bin/python scripts/collect.py --harvest-only --max-harvest 40`
   (repeat this step until the backlog is 0 or nothing new is fetched).
3. Else if queries are pending: `.venv/bin/python scripts/collect.py --max-queries 8`.
4. `.venv/bin/python scripts/status_report.py --json` and decide the next action:
   - `artifacts_zero_rows` grew → `.venv/bin/python scripts/inspect_artifact.py --zero-rows`, inspect up to 3
     with `scripts/inspect_artifact.py <path> --lines 40`. If a new document layout is clearly a list of
     delisted companies or a delisting order, improve `HEADER_MAP` / `TYPE_KEYWORDS` / `rows_from_order`
     in `scripts/parse_nse_records.py`, then `parse_nse_records.py --force` and `normalize_and_merge.py`.
     Log what you changed.
   - Priority-grid gaps (year × type with 0 records) after the plan for that year is done → add 2–4
     targeted queries to a phase named `agent_added` at the END of `config/query_plan.json`
     (use the discovery skill templates; set `year_tag`). Never delete existing queries.
   - `data/processed/proposed_delisting_leads.csv` has companies with `already_in_master = False` →
     add queries like `site:nseindia.com "<company name>" delisting order` to `agent_added` (max 10 per cycle).
5. Log a one-line milestone: `.venv/bin/python scripts/common.py log "cycle N: <what you did>, records X"`.

## Stop conditions (then write a final summary log line and finish)
- All plan phases complete, harvest backlog 0, and the last two cycles added 0 new records; or
- SearXNG unavailable (collect.py returns `searxng_unavailable` / `searxng_errors`) twice in a row; or
- 25 cycles completed.

## Hard rules
- Never hand-edit files in `data/processed/` or `data/metadata/`; never delete `data/` or `logs/`.
- Never bypass blocks (no headless browsers, no WAF tricks, no removing delays). Blocked domains stay blocked.
- Don't open raw files directly; use `inspect_artifact.py` (bounded output).
- Keep your messages short: counts and file paths only.
