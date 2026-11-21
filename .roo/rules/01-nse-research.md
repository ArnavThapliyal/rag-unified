# NSE Delisting Research — Workspace Rules

Read `soul.md` once at the start of a task. Full spec: `instructions.md`.

## Environment
- Python: always use `.venv/bin/python` (run from the workspace root).
- Search: SearXNG at `http://localhost:8082` (JSON enabled). Only reach it through `scripts/searxng_client.py`.
- Never open files under `data/raw/`, `data/search_cache/`, `data/quarantine/`, or `vendor/` directly. They are ignored on purpose.

## Workflow loop (one cycle at a time)
1. **Discover** — `.venv/bin/python scripts/searxng_client.py --query "<q>" --pages 2 --year-tag 2017`
   Read `skills/searxng-discovery/SKILL.md` for query templates.
2. **Fetch** — `.venv/bin/python scripts/polite_fetcher.py --from-cache-file data/search_cache/<file>.json --limit 10 --domains nseindia.com,sebi.gov.in,bseindia.com`
   or a single URL: `--url "<url>" --year-tag 2017`
3. **Parse** — `.venv/bin/python scripts/parse_nse_records.py` (only new artifacts; `--force` to reparse)
   Read `skills/nse-document-parser/SKILL.md` before changing parsing rules.
4. **Merge** — `.venv/bin/python scripts/normalize_and_merge.py`
   Read `skills/dataset-auditor/SKILL.md` for dedupe/confidence rules.
5. **Report** — `.venv/bin/python scripts/status_report.py`
   Use the report to pick the next gap (year × delisting type) to search.

## Priority
- Priority years: **2007, 2017, 2020, 2024** — fill these first, then the rest of 2006–2026.
- Year tags: `2007|2017|2020|2024` → `data/raw/priority_<yr>/`; `master` → `data/raw/master_lists/`; any other year → `data/raw/archive_2006_2026/`.

## Hard rules
- No WAF/Akamai bypass, no headless-browser scraping, no removing delays.
- Do not hand-edit `data/processed/*`. Re-run scripts instead.
- If a script fails twice with the same error, stop and report the error text to the user.
- Keep answers short: report counts and file paths, not document contents.
