# Project Instructions: NSE Historical Delisted Companies Archive & Research Pipeline

## 1. Mission & Primary Objectives
You are scaffolding and operating a local-first research and data-engineering workspace to compile, verify, and structure a historical dataset of companies delisted from the **National Stock Exchange of India (NSE)** across a **15–20 year historical window (~2006–2026)**, with **high-priority deep-dive coverage** on four pivotal years:
- **2007**: Pre-2009 SEBI delisting framework transitions, SCRA/SCRR enforcement shifts, and early compulsory/voluntary removals.
- **2017**: Major regulatory crackdown on shell/non-compliant/suspended companies and widespread compulsory delisting orders.
- **2020**: COVID-19 pandemic disruptions, IBC/NCLT insolvency-driven delistings, and regulatory relief/timeline extensions.
- **2024**: SEBI (Delisting of Equity Shares) (Amendment) Regulations, 2024 (fixed-price delisting route, counter-offer changes, Investment Holding Company framework) and baseline calculation anchor.

Your immediate responsibility in this host folder is to **create the project architecture, agent configuration files (`soul.md`, Cline agent rules, and `/skills`), Python search/fetch/parse scripts, and structured logging** so the local coding agent (Cline CLI running a local ~27B–30B model via LM Studio on Apple Silicon 48 GB RAM) can execute deterministic collection and verification without context bloat or IP blocks.

---

## 2. Environment & Infrastructure Constraints

| Parameter | Specification / Value | Operational Rule |
| :--- | :--- | :--- |
| **Host Hardware** | MacBook Pro — Apple M4 Pro, 48 GB Unified Memory (14 CPU / 20 GPU cores) | Offload all HTTP fetching, HTML/PDF parsing, deduplication, and CSV/Parquet generation to Python scripts. Never dump raw HTML or large PDFs into LLM context. |
| **Local Agent** | Cline CLI (unattended, auto-approve) with local ~27B–30B model in LM Studio | Keep prompt/skill instructions modular and concise; pass only structured JSON/CSV summaries and file paths back to the agent. |
| **Search Endpoint** | Dockerized SearXNG at `http://localhost:8082` | Use `http://localhost:8082/search?q=<query>&format=json` as the primary discovery layer. Verify `format=json` is enabled in SearXNG `settings.yml`; provide an HTML fallback parser in the client script if `403` is returned. |
| **Network & Anti-Block Policy** | Polite, rate-limited, compliant collection | **Do not use WAF/Akamai bypass scripts or aggressive scraping.** Use SearXNG for discovery, official NSE/SEBI/BSE public archives and circulars, randomized delays (`2.0–5.0s`), exponential backoff on `429`/`5xx`, and local disk caching so no URL is fetched twice. |

---

## 3. Target Workspace Directory Structure
Create and maintain this exact folder layout in the host workspace:

```text
.
├── instructions.md                  # Master project specification (this file)
├── soul.md                          # Persona, discipline, and execution rules for the local Cline agent
├── .cline/
│   └── rules/
│       └── 01-nse-research.md       # Auto-loaded workspace rules for Cline
├── skills/
│   ├── searxng-discovery/
│   │   └── SKILL.md                 # Query patterns & pagination rules for http://localhost:8082
│   ├── nse-document-parser/
│   │   └── SKILL.md                 # Extracting ISIN, symbol, order date, effective date, & delisting track
│   └── dataset-auditor/
│       └── SKILL.md                 # Cross-checking, deduplication, & confidence scoring rules
├── scripts/
│   ├── web_search.py                # Search via pluggable providers (search_providers/), cache in data/search_cache/
│   ├── docket.py                    # Persistent queue between discovery and fetching (data/docket.sqlite)
│   ├── fetch_worker.py              # Downloads docket items at each domain's pace
│   ├── process_worker.py            # Parses / OCRs downloads, merges records
│   ├── polite_fetcher.py            # Cached, rate-limited downloader with status/hash logging & quarantine
│   ├── parse_nse_records.py         # Extract structured records from HTML circulars, PDFs, & XLSX sheets
│   ├── normalize_and_merge.py       # Deduplicate by ISIN/Symbol/Date, tag priority years, export CSV/JSONL
│   └── status_report.py             # Print concise progress dashboard by year and delisting category
├── data/
│   ├── search_cache/                # Raw JSON responses from http://localhost:8082
│   ├── raw/
│   │   ├── master_lists/            # Official NSE/BSE/SEBI consolidated XLSX/PDF files
│   │   ├── priority_2007/           # Raw circulars/orders/pages tagged 2007
│   │   ├── priority_2017/           # Raw circulars/orders/pages tagged 2017
│   │   ├── priority_2020/           # Raw circulars/orders/pages tagged 2020
│   │   ├── priority_2024/           # Raw circulars/orders/pages tagged 2024
│   │   └── archive_2006_2026/       # Remaining years across the 15–20 year span
│   ├── quarantine/                  # Non-200 responses, empty files, or unparseable artifacts for review
│   └── processed/
│       ├── nse_delisted_master.csv  # Master normalized table (all years)
│       ├── nse_delisted_master.jsonl
│       └── priority_years_audit.csv # Filtered & enriched dataset for 2007, 2017, 2020, 2024
└── logs/
    ├── fetch_manifest.jsonl         # Append-only log: timestamp, url, http_status, sha256, local_path, year_tag
    └── agent_run.log                # Human-readable milestone & error log (tail -f to watch live)