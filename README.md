# NSE Delisted Companies Archive

A local-first research pipeline for building a verified, source-cited dataset of companies delisted from the **National Stock Exchange of India (NSE)**, 2006–2026. Four years get deep coverage: **2007, 2017, 2020, 2024**.

It is built to be driven by a local coding agent (Roo Code / Cline running a ~27–30B model). Python scripts do all the fetching, parsing and merging. The agent only reads their short JSON summaries and the processed CSVs, so its context stays small.

Full specification: [`instructions.md`](instructions.md).

---

## How it works

```text
  SearXNG (localhost:8082)
          │  1. searxng_client.py      query → cached result list
          ▼
  data/search_cache/*.json
          │  2. polite_fetcher.py      download each URL once, slowly
          ▼
  data/raw/<year bucket>/*.pdf|html|xlsx  ──►  logs/fetch_manifest.jsonl (url, status, sha256, path)
          │  3. parse_nse_records.py   extract ISIN / name / symbol / dates / type
          ▼
  data/processed/parsed/*.jsonl       (one candidate row per line)
          │  4. normalize_and_merge.py dedupe, derive FY / regime / priority flag, score confidence
          ▼
  data/processed/nse_delisted_master.csv + .jsonl
  data/processed/priority_years_audit.csv
          │  5. status_report.py       what's covered, what's missing
          ▼
  next search  ──► (loop)
```

1. **Discovery.** SearXNG finds candidate documents: NSE circulars and delisted lists, SEBI orders, BSE notices, news. Each query and page is cached, so repeating a query costs nothing.
2. **Fetching.** Only URLs you choose (filtered by `--domains`) are downloaded. The downloads are spaced 2–5 s apart, each URL is fetched once, and failures go to quarantine. The manifest records where every file came from.
3. **Parsing.** Tables in spreadsheets, HTML and PDFs are matched by their column headers. Text documents are scanned for ISINs. Each document's text sets the delisting type (compulsory, voluntary, merger or IBC).
4. **Merging.** Rows for the same company and event are merged. Each record gets a stable `record_id`, an Indian financial year, a SEBI regulatory regime and a confidence level based on its source domain.
5. **Reporting.** A year × delisting-type grid shows the gaps, and the gaps decide the next query.

The agent (or you) repeats this loop until the priority years are filled, then moves on to the rest of 2006–2026.

---

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

# Optional: reference copies of the agent IDEs (git-ignored)
git clone --depth 1 https://github.com/RooCodeInc/Roo-Code vendor/Roo-Code
git clone --depth 1 https://github.com/cline/cline vendor/cline
```

---

## Starting data collection

### Option A: let the agent drive (recommended)

Before you start, check that both local services are up:

```bash
curl -s "http://localhost:8082/search?q=test&format=json" | head -c 100   # SearXNG returns JSON
curl -s http://localhost:1234/v1/models | head -c 200                     # LM Studio lists qwen/qwen3.8-27b
```

There are two ways to launch the agent. Both read the same rules (`.roo/rules/01-nse-research.md`; Cline reaches it through the `.cline/rules/` link) and use the same local model.

#### Method 1: Roo Code in VS Code or Cursor (GUI)

Use this when you want to watch and approve each step in a sidebar.

1. Install the extension (one-time):
   ```bash
   code --install-extension RooVeterinaryInc.roo-cline     # or: cursor --install-extension RooVeterinaryInc.roo-cline
   ```
2. Open the project:
   ```bash
   code /Users/arnav/Projects/Software/nse-delisted-comp
   ```
3. Click the Roo Code (kangaroo) icon → ⚙ Settings → **API Provider: LM Studio**:
   - Base URL: `http://localhost:1234`
   - Model: `qwen/qwen3.8-27b`
4. Start a new task and paste the starting prompt below.

The Roo Code CLI (`roo`) **can't** be used here, because it only supports cloud providers (Anthropic, OpenAI, Gemini, OpenRouter, Vercel). For a local model, use the extension or Cline.

#### Method 2: Cline CLI (terminal only)

Use this when you want everything in the terminal with no editor.

Cline is installed per project as a dev dependency (`package.json`, version pinned in `package-lock.json`):

```bash
cd /Users/arnav/Projects/Software/nse-delisted-comp
npm install                  # one-time: installs the pinned cline into node_modules/
npx cline auth lmstudio -b http://localhost:1234/v1 -m qwen/qwen3.8-27b   # one-time
npx cline -i                 # interactive terminal UI; paste the starting prompt below
```

Other useful forms:
- `npx cline "<prompt>"` runs a single task in act mode with auto-approve.
- `npx cline -p "<prompt>"` starts in plan mode, so it proposes steps before acting.
- `npx cline history` lists past sessions. Resume one with `npx cline --id <session-id>`.

Use `npx cline`, not `cline`: plain `cline` runs the global copy in `/opt/homebrew` (if installed), which can be a different version.

#### Starting prompt (for either method)

> Read `soul.md`. Start with master lists: run the searxng-discovery master-list queries with `--year-tag master`, fetch only nseindia.com / sebi.gov.in / bseindia.com results (limit 10 per cache file), parse, merge, and show me `status_report.py`. Then work through priority year 2017, compulsory delistings first.

Watch progress in `logs/agent_run.log` (`tail -f logs/agent_run.log` in a second terminal), which logs a line for every query, fetch, parse and merge. Run `status_report.py` whenever you want the overview.

Good follow-up prompts:
- "Fill the gaps listed in the status report for 2024, using the year-specific queries in the discovery skill."
- "List the NEEDS_REVIEW rows for 2020 and find an official source for each."
- "Check data/quarantine via the manifest and tell me which domains are blocking us."

### Option B: run it by hand

```bash
P=.venv/bin/python

# 1. discover
$P scripts/searxng_client.py --query "site:nseindia.com compulsory delisting 2017" --pages 2 --year-tag 2017
#    → prints "cache_files": ["data/search_cache/<id>.json"]

# 2. fetch (official domains only, 10 at a time)
$P scripts/polite_fetcher.py --from-cache-file data/search_cache/<id>.json \
   --limit 10 --domains nseindia.com,sebi.gov.in,bseindia.com

# 3–5. parse, merge, report
$P scripts/parse_nse_records.py
$P scripts/normalize_and_merge.py
$P scripts/status_report.py
```

To queue several priority-year searches in one go:

```bash
for Y in 2007 2017 2020 2024; do
  for Q in "site:nseindia.com delisting circular $Y" \
           "site:nseindia.com compulsory delisting $Y" \
           "site:sebi.gov.in delisting order $Y"; do
    .venv/bin/python scripts/searxng_client.py --query "$Q" --pages 2 --year-tag $Y
  done
done
.venv/bin/python scripts/searxng_client.py --list-cache   # see what's queued for fetching
```

### Recommended order

1. **Master lists** (`--year-tag master`): NSE's consolidated "delisted companies" spreadsheets and PDFs. They give the most rows per fetch.
2. **2017**: the shell-company crackdown, which produced large compulsory-delisting lists.
3. **2020**: IBC/NCLT delistings and the COVID-era relaxations.
4. **2024**: the SEBI 2024 amendment (fixed-price route, counter-offer changes).
5. **2007**: older, sparser material, mostly SEBI 2003-Guidelines-era documents.
6. **Everything else** in 2006–2026 (`--year-tag <year>` → `archive_2006_2026/`).

### Useful flags

- `searxng_client.py --list-cache` lists the queries already cached.
- `polite_fetcher.py --url "<url>" --year-tag 2020` fetches a single URL.
- `parse_nse_records.py --force` reparses every file. `--file <path>` parses one file.
- `status_report.py --year 2017 --needs-review` lists the rows that still need checking. `--json` prints machine-readable output.
- `python scripts/common.py log "message"` adds a milestone line to `logs/agent_run.log`.

---

## Where everything is saved

| Location | Contents | Tracked in git? |
|---|---|---|
| `data/search_cache/<hash>.json` | Raw SearXNG results: query, page, year tag, URLs, snippets | No (can be regenerated) |
| `data/raw/master_lists/` | Consolidated NSE/BSE/SEBI delisted lists (`--year-tag master`) | No |
| `data/raw/priority_2007/`, `priority_2017/`, `priority_2020/`, `priority_2024/` | Documents tagged with a priority year | No |
| `data/raw/archive_2006_2026/` | Documents for every other year | No |
| `data/quarantine/` | Non-200, empty or blocked responses, kept for inspection | No |
| `data/processed/parsed/<bucket>__<hash>.jsonl` | Candidate rows per document, before merging | No |
| `data/processed/nse_delisted_master.csv` / `.jsonl` | **The final dataset**, all years | **Yes** |
| `data/processed/priority_years_audit.csv` | Priority-year rows with `source_count` and `all_source_urls` | **Yes** |
| `logs/fetch_manifest.jsonl` | Append-only record of every download: timestamp, url, http_status, sha256, local_path, year_tag | **Yes** |
| `logs/agent_run.log` | Human-readable log of every script run | No |

Files are named by the first 16 hex characters of the URL's sha256, so the same URL always maps to the same file. To trace a row back to its source, follow `source_url` → `local_artifact_path` → the manifest entry with its sha256.

---

## Collection policy

- Waits a random 2–5 seconds between requests and backs off exponentially on `429` and `5xx` responses (up to 4 tries, honouring `Retry-After`).
- Never fetches the same URL twice (it checks the fetch manifest first).
- Moves non-200 and empty responses to `data/quarantine/`.
- **Never bypasses WAF/Akamai protection and does not scrape aggressively.** If a domain keeps blocking requests, stop using it and log the block.

## Output schema

The root [`nse_delisted_master.csv`](nse_delisted_master.csv) defines the columns:

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
| `confidence_status` | See below |

**Confidence levels:**
- `VERIFIED_PRIMARY`: from an official NSE, SEBI or BSE document
- `CORROBORATED_SECONDARY`: from a reputable financial or legal source, or confirmed by two or more independent domains
- `NEEDS_REVIEW`: everything else

Don't edit the processed files by hand. Fix the inputs or the parsing rules and re-run the scripts.

---

## What has been configured

| Item | Configuration |
|---|---|
| **SearXNG** (Docker container `searxng`, port 8082 → 8080) | Added `search.formats: [html, json]` to `/etc/searxng/settings.yml` inside the container. The original is saved as `settings.yml.bak` in the same folder. If JSON is ever disabled again, the client falls back to parsing the HTML results page. |
| **Python env** | `.venv/` (Python 3.11) with `requests`, `beautifulsoup4`, `lxml`, `pdfplumber`, `openpyxl`, `xlrd` and `pandas` |
| **Roo Code rules** | `.roo/rules/01-nse-research.md`, loaded automatically for every task |
| **Agent persona** | `soul.md`: accuracy over volume, never invent fields, every row cites a source |
| **Skills** | `skills/searxng-discovery`, `skills/nse-document-parser` and `skills/dataset-auditor`, which the agent reads before each phase |
| **`.rooignore`** | Hides `vendor/`, `.venv/`, `data/raw/`, `data/search_cache/`, `data/quarantine/` and `data/processed/parsed/` from the agent, so raw files never fill its context |
| **`.gitignore`** | Keeps out the virtualenv, vendor clones, raw downloads and the run log. The dataset and the manifest are committed. |
| **Trusted domains** (`scripts/common.py`) | Primary: `nseindia.com`, `sebi.gov.in`, `bseindia.com`. Secondary: Business Standard, ET, Moneycontrol, Mint, BusinessLine, Financial Express, IBBI, NCLT, TaxGuru, Indian Kanoon. Edit `PRIMARY_DOMAINS` / `SECONDARY_DOMAINS` to change them. |
| **Parser keywords** (`scripts/parse_nse_records.py`) | `HEADER_MAP` (column headers → fields) and `TYPE_KEYWORDS` (text → delisting type). Extend these when a new document format yields 0 rows. |
| **Cline CLI** | Per-project dev dependency in `package.json` / `package-lock.json`; run with `npx cline`. `node_modules/` is git-ignored. |
| **Vendor repos** | `vendor/Roo-Code` and `vendor/cline`, shallow clones for reference (e.g. how rules and ignore files are loaded) |
| **Git** | Local repo on `main`, a single author, one commit per change |

---

## Proposed uses for this dataset

The dataset links every delisting to its date, type, regime, ISIN and source, so it can support a range of analysis. None of the ideas below is built yet. They are possible next steps.

### Research and analysis

1. **Survivorship-bias-free backtesting.** Most free NSE datasets only include companies that are still listed. Joining this table by ISIN or symbol puts dead companies back into historical universes, so returns aren't overstated. Each `effective_delisting_date` marks where a stock's price series ends.
2. **Regulatory event studies (the four priority years).**
   - *2007*: the last years of the SEBI 2003 Guidelines, before the 2009 Regulations.
   - *2017*: the size and makeup of the shell-company crackdown (compulsory delistings by month, by sector, and suspension-to-delisting lag).
   - *2020*: how much IBC/NCLT-driven delisting grew and how COVID relaxations changed timelines.
   - *2024*: whether the fixed-price route and counter-offer changes moved voluntary delistings (count and premium) before and after `SEBI_2024_Amendment`.
3. **Regime comparison.** Group by `regulatory_regime` × `delisting_type` to see how each SEBI framework changed the mix of compulsory and voluntary exits.
4. **Investor recovery and exit pricing.** Use `exit_or_fair_value_inr` together with the last traded price (from bhavcopy data) to estimate what minority shareholders received on voluntary exits and compulsory fair-value buybacks.
5. **IBC outcome tracking.** Link `IBC_NCLT` rows to IBBI/NCLT orders to study whether resolution plans ended in delisting or relisting, and how long that took.
6. **Early-warning signals.** Label each company by whether it was later delisted, then test whether signals like suspension, non-filing or penny-stock status predict compulsory delisting. This is useful for screening current holdings.

### Engineering extensions

| Extension | Notes |
|---|---|
| **DuckDB/SQLite + Parquet export** | Add a `--parquet` flag to `normalize_and_merge.py` and query with SQL. Parquet suits joins against price data. |
| **Price-data join** | Pull NSE bhavcopy archives (via the same polite fetcher) to add last traded date, last price and volume to each row. |
| **BSE cross-listing** | Many companies were listed on both exchanges. A BSE pass would fill missing ISINs and dates and add corroboration. |
| **Analysis notebooks** | Add `notebooks/` with one notebook per research question above, reading only `data/processed/`. |
| **Local dashboard** | A small Streamlit/Plotly app for the year × type grid, regime timelines and drill-down to source documents. |
| **OCR for scanned PDFs** | An optional `ocrmypdf` step for files the parser flags as `no_text`. |
| **Scheduled refresh** | A weekly `cron`/`launchd` job that re-runs current-year queries and the pipeline to pick up new NSE circulars. |
| **Validation tests** | `pytest` checks on the processed CSV: ISIN format, date ranges, no duplicate `record_id`, every row has a `source_url`. |
| **Second agent for review** | Use Cline as a reviewer: a read-only rule set that only works through `--needs-review` rows and proposes sources. |

---

## Agent configuration files

| File | Purpose |
|---|---|
| `soul.md` | The agent's persona and core rules (no invented data, every row cites a source, polite fetching only) |
| `.roo/rules/01-nse-research.md` | Workspace rules Roo Code loads automatically: the workflow loop and exact commands |
| `skills/searxng-discovery/SKILL.md` | Query templates for each year and delisting type, plus pagination rules |
| `skills/nse-document-parser/SKILL.md` | How each field is extracted (ISIN regex, dates, type keywords, regulatory regime) |
| `skills/dataset-auditor/SKILL.md` | Deduplication, confidence upgrades and the audit checklist |
| `.rooignore` | Hides `vendor/`, `.venv/` and the raw data folders from the agent |
| `.cline/rules/01-nse-research.md` | Symlink to the Roo rules file, so Cline (CLI or extension) follows the same workflow. Edit the Roo file only. |
| `.clineignore` | Symlink to `.rooignore` |

## Layout

```text
.
├── instructions.md, soul.md, README.md, requirements.txt, package.json
├── nse_delisted_master.csv     # schema reference
├── .roo/rules/                 # Roo Code workspace rules (source of truth)
├── .cline/rules/               # symlink → .roo/rules file, for Cline
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
- The parser's header and keyword matching is heuristic. Check a sample of rows from each new document format before relying on it.
