# NSE Delisted Companies Archive

A local-first research pipeline for building a verified, source-cited dataset of companies delisted from the **National Stock Exchange of India (NSE)**, 2006–2026. Four years get deep coverage: **2007, 2017, 2020, 2024**.

It is built to be driven by a local coding agent (Roo Code / Cline running a ~27–30B model). Python scripts do all the fetching, parsing and merging. The agent only reads their short JSON summaries and the processed CSVs, so its context stays small.

Full specification: [`instructions.md`](instructions.md).

## Requirements

- Python 3.11+
- Docker with SearXNG running at `http://localhost:8082`, with JSON output enabled in its `settings.yml`:
  ```yaml
  search:
    formats:
      - html
      - json
  ```
  Check it with:
  ```bash
  curl -s "http://localhost:8082/search?q=test&format=json" | head -c 200
  ```
  You should get JSON back, not a `403` page.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Pipeline

Run each step from the workspace root. Every script prints a one-line JSON summary.

| Step | Command | What it does |
|---|---|---|
| 1. Discover | `.venv/bin/python scripts/searxng_client.py --query "site:nseindia.com compulsory delisting 2017" --pages 2 --year-tag 2017` | Searches SearXNG and caches results in `data/search_cache/` |
| 2. Fetch | `.venv/bin/python scripts/polite_fetcher.py --from-cache-file data/search_cache/<file>.json --limit 10 --domains nseindia.com,sebi.gov.in,bseindia.com` | Downloads documents into `data/raw/` and logs each one to `logs/fetch_manifest.jsonl` |
| 3. Parse | `.venv/bin/python scripts/parse_nse_records.py` | Pulls candidate records out of HTML, PDF, XLSX and CSV files into `data/processed/parsed/` |
| 4. Merge | `.venv/bin/python scripts/normalize_and_merge.py` | Normalizes and deduplicates records, then writes the master CSV/JSONL and the priority-year audit |
| 5. Report | `.venv/bin/python scripts/status_report.py` | Shows counts by year × delisting type × confidence, and lists priority-year gaps |

Useful flags:
- `searxng_client.py --list-cache` lists the queries already cached.
- `polite_fetcher.py --url "<url>" --year-tag 2020` fetches a single URL.
- `parse_nse_records.py --force` reparses every file. `--file <path>` parses one file.
- `status_report.py --year 2017 --needs-review` lists the rows that still need checking.
- `python scripts/common.py log "message"` adds a milestone line to `logs/agent_run.log`.

**Year tags** decide where downloads go:
- `2007`, `2017`, `2020`, `2024` → `data/raw/priority_<year>/`
- `master` → `data/raw/master_lists/`
- anything else → `data/raw/archive_2006_2026/`

## Collection policy

- Waits a random 2–5 seconds between requests and backs off exponentially on `429` and `5xx` responses.
- Never fetches the same URL twice (it checks the fetch manifest first).
- Moves non-200 and empty responses to `data/quarantine/`.
- **Never bypasses WAF/Akamai protection and does not scrape aggressively.** If a domain keeps blocking requests, stop using it and log the block.

## Output schema

The root [`nse_delisted_master.csv`](nse_delisted_master.csv) defines the columns: `record_id`, `company_name`, `nse_symbol`, `isin`, `delisting_type`, `order_date`, `effective_delisting_date`, `calendar_year`, `financial_year`, `is_priority_year`, `regulatory_regime`, `exit_or_fair_value_inr`, `source_url`, `local_artifact_path`, `confidence_status`.

Outputs in `data/processed/`:
- `nse_delisted_master.csv` and `nse_delisted_master.jsonl`: all years
- `priority_years_audit.csv`: 2007, 2017, 2020 and 2024 only, with `source_count` and `all_source_urls` added

**Confidence levels:**
- `VERIFIED_PRIMARY`: from an official NSE, SEBI or BSE document
- `CORROBORATED_SECONDARY`: from a reputable financial or legal source, or confirmed by two or more independent domains
- `NEEDS_REVIEW`: everything else

Don't edit the processed files by hand. Fix the inputs or the parsing rules and re-run the scripts.

## Agent configuration

| File | Purpose |
|---|---|
| `soul.md` | The agent's persona and core rules (no invented data, every row cites a source, polite fetching only) |
| `.roo/rules/01-nse-research.md` | Workspace rules Roo Code loads automatically: the workflow loop and exact commands |
| `skills/searxng-discovery/SKILL.md` | Query templates for each year and delisting type, plus pagination rules |
| `skills/nse-document-parser/SKILL.md` | How each field is extracted (ISIN regex, dates, type keywords, regulatory regime) |
| `skills/dataset-auditor/SKILL.md` | Deduplication, confidence upgrades and the audit checklist |
| `.rooignore` | Hides `vendor/`, `.venv/` and the raw data folders from the agent |

To start, open the folder in Roo Code and ask the agent to follow `.roo/rules/01-nse-research.md`, beginning with the master-list searches (`--year-tag master`).

## Layout

```text
.
├── instructions.md, soul.md, README.md, requirements.txt
├── nse_delisted_master.csv     # schema reference
├── .roo/rules/                 # Roo Code workspace rules
├── skills/                     # agent skill guides
├── scripts/                    # pipeline (common.py holds shared paths and helpers)
├── data/
│   ├── search_cache/           # cached SearXNG responses
│   ├── raw/                    # master_lists/, priority_<year>/, archive_2006_2026/
│   ├── quarantine/             # failed or empty downloads
│   └── processed/              # master dataset, audit, parsed/
├── logs/                       # fetch_manifest.jsonl, agent_run.log
└── vendor/                     # shallow clones of Roo-Code and cline (reference only)
```

## Known limitations

- Many `nseindia.com` pages load their content with JavaScript and block bots, so plain fetches often end up in quarantine. The `archives.nseindia.com` PDFs and spreadsheets, SEBI and BSE are more reliable sources.
- Scanned (image-only) PDFs are flagged as `no_text` and need OCR or manual review.
- Search results sometimes point to `nseindia.in`, which is not the official domain. Rows from it stay `NEEDS_REVIEW` until confirmed by an official source.
