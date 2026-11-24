# 3. LanceDB schema

The contract with the LLM stage is defined once, in `pipeline/schema.py`, as the pydantic model `ChunkRecord`. `SCHEMA_VERSION` in `config/settings.py` is currently `2` and must be bumped when the model changes.

## Table, vector and metric

| Item | Value | Defined in |
|---|---|---|
| Database path | `data/lancedb` (`LANCEDB_PATH`) | `config/settings.py` |
| Table | `documents` (`LANCEDB_TABLE`) | `config/settings.py` |
| Embedding model | `BAAI/bge-m3`, dense output | `EMBEDDING_MODEL` |
| Vector column | `embedding`: fixed-size list of **1024** float32 | `Vector(EMBEDDING_DIM)` |
| Distance metric | **cosine** | `DISTANCE_METRIC`, `VECTOR_INDEX_KWARGS` |

LanceDB does not record a metric in the table schema. The metric is chosen when an index is created and when a search runs. Both sides must therefore pass it explicitly:

```python
from pipeline.schema import VECTOR_INDEX_KWARGS   # {"metric": "cosine", "vector_column_name": "embedding"}

table.search(query_vector).metric("cosine").limit(10)      # LLM side
```

The Processor builds the cosine vector index at the end of a run once the table has at least `VECTOR_INDEX_MIN_ROWS` rows; smaller tables are searched by exact scan. A search that omits the metric on an unindexed table uses L2. Vectors are L2-normalised at embedding time, so L2 and cosine rank identically, but readers should still set cosine explicitly.

Query vectors must come from the same model (`BAAI/bge-m3`, dense output) and should be normalised the same way.

The Arrow field for the vector is technically nullable, so `build_record` enforces the dimension: a vector that is not exactly 1024 long raises `ValueError`.

## Identifiers

| Id | Formula | Properties |
|---|---|---|
| `doc_id` | `sha256(file bytes)`, first 32 hex chars | Content-addressed. The same file under a different name or path gets the same id. |
| `chunk_id` | `sha256(doc_id + "\0" + chunk_index + "\0" + chunk text)`, first 32 hex chars | Deterministic and unique within and across documents. |
| `content_hash` | `sha256(chunk text)` | Informational only. **Not** used for deduplication. |

Why `chunk_id` hashes all three parts:

- `doc_id` scopes the id to a document, so identical boilerplate in two documents does not collide.
- `chunk_index` separates identical text repeated inside one document (for example a disclaimer on every page).
- The chunk text makes the id change when the content changes, so a change in chunking settings or parser output cannot silently reuse an id for different text.

Re-ingesting an unchanged file with unchanged settings produces identical ids, which is what makes skip-if-present and upsert safe.

## Columns

### Required (always set by the pipeline)

| Column | Type | Notes |
|---|---|---|
| `doc_id` | string | See above |
| `chunk_id` | string | See above |
| `chunk_index` | int | Position of the chunk within its document, from 0 |
| `text` | string | Chunk text; never empty |
| `embedding` | fixed list\[1024\] float32 | |
| `content_kind` | string | `prose` or `table` |
| `source_type` | string | `pdf`, `docx`, `pptx`, `html`, `json`, `csv`, `xlsx`, `xls`, `unknown` |
| `source_filename` | string | `archive.zip!member.pdf` for zip members |
| `content_hash` | string | |
| `ingested_at` | string | ISO-8601 UTC |
| `embedding_model` | string | |
| `pipeline_version` | string | |
| `ingest_run_id` | string | Identifies the batch run |
| `metadata_json` | string | JSON object; `"{}"` when empty |

### Optional (null when unknown)

| Column | Type | Source |
|---|---|---|
| `symbol` | string | Vendor metadata, or the row's `symbol`/`ticker` column for record-mode tabular files; never inferred from folder names |
| `source_url` | string | Vendor metadata |
| `doc_type` | string | Vendor metadata |
| `vendor` | string | Vendor metadata |
| `published_at` | string | ISO-8601 date or datetime as supplied |
| `section_path` | string | Heading breadcrumb, `H1 > H2 > H3` |
| `page_range` | string | `"5"` or `"5-7"`; null for formats without pages |
| `allowed_roles` | list\[string\] | Null means no restriction was declared |

### Financial metadata: 50 nullable columns

These are the fields the downstream LLM expects. All are nullable and listed in `EXTENDED_FIELDS`; a test enforces that the model and that mapping stay in sync.

| Group | Columns | Type |
|---|---|---|
| Instrument | `isin`, `nse_symbol`, `series`, `sector`, `industry` | string |
| | `market_cap_cr`, `free_float_pct` | float |
| | `is_fno` | bool |
| Price and volume | `trade_date` | string (ISO date) |
| | `open`, `high`, `low`, `close`, `turnover_cr`, `delivery_pct`, `oi_change_pct` | float |
| | `total_traded_qty`, `deliverable_qty`, `no_of_trades`, `futures_oi` | int |
| | `bulk_block_deal_flag` | bool |
| Fundamentals | `quarter_ending` | string (ISO date) |
| | `sales_growth_yoy`, `sales_growth_qoq`, `ebitda_margin_pct`, `pat_growth_yoy`, `eps_ttm`, `roce_pct`, `roe_pct`, `debt_to_equity`, `interest_coverage`, `cash_flow_from_ops` | float |
| Valuation | `pe_ratio`, `sector_median_pe`, `ev_ebitda`, `peg_ratio` | float |
| Shareholding | `promoter_holding_pct`, `promoter_pledge_pct`, `fii_holding_chg_qoq`, `dii_holding_chg_qoq`, `retail_holding_chg_qoq` | float |
| Surveillance and events | `surveillance_stage`, `broadcast_timestamp`, `announcement_category`, `announcement_headline`, `announcement_body_md`, `concall_commentary_md`, `concall_guidance_md`, `sast_insider_activity`, `bulk_block_deal_participants` | string |

The types were chosen by the Processor side because only names were specified. Confirm them with the LLM side before the first production load.

#### Where the values come from

- **Document level** (`SourceDocument.fields`): any vendor metadata key that exactly matches a column name. Applied to every chunk of the document.
- **Chunk level** (`Chunk.fields`): values specific to one chunk. They override the document-level value.

Chunk-level values come from tabular files in record mode: when a sheet's header names at least `TABULAR_RECORD_MIN_MATCHES` (default 3) of these columns, each row becomes its own chunk and its cells fill the typed columns. Header names are matched exactly, ignoring case. See [04_processing_stages.md](04_processing_stages.md).

For chunks of ordinary documents (a PDF filing, a presentation) these columns hold only what the vendor metadata supplied for the whole document.

#### Coercion (`coerce_extended`)

Coercion never raises.

| Input | Result |
|---|---|
| `"1,234.50"` for a float column | `1234.5` |
| `"51.2%"` | `51.2` |
| `"(12.5)"` (accounting negative) | `-12.5` |
| `"Yes"`, `"y"`, `1` for a bool column | `True` |
| `12.0` for an int column | `12` |
| `""` | null |
| `"n/a"`, `1.5` for an int column, `True` for a numeric column, NaN/inf | null, and the original value is kept in `metadata_json` |
| list or dict for a string column | JSON-encoded string |

## Row construction

`build_record(doc, chunk, embedding, *, ingested_at, ingest_run_id, embedding_model, pipeline_version)` is the only place a row is assembled. It raises `ValueError` for contract violations (missing `doc_id`, empty text, wrong vector length, unknown `content_kind`) and returns a plain dict ready for `table.add`.

`metadata_json` contains the document's unmapped vendor fields, the chunk's own metadata (for table chunks: `table_id`, sheet name, row range, column names) and any financial values that could not be converted.

## Authorisation compatibility

`tests/rag_authz.rego` reads `allowed_roles` from the document metadata. The column is top-level and nullable for that reason. Do not rename it or move it into `metadata_json` without changing the policies.
