# refactoring_flags.md

> Status: review document only. No code has been changed. Line numbers refer to the repo at commit `e970c11`.
> Items marked **[VERIFY]** are based on library behavior I could not run (the venv is not installed). Verify them in step 0 before relying on them.

## 0. Decisions this document is built on
| Topic | Decision |
|---|---|
| Input | Mixed static files: PDF, PPTX, DOCX, HTML, **JSON, CSV/XLSX (tabular numeric)**. Append-only batch load. |
| Vendor metadata | Inconsistent or absent. Every metadata field is optional. The pipeline must never crash on missing metadata. |
| Vector DB | LanceDB. Embedding is bge-m3, 1024-dim, **cosine** distance. |
| Hardware | Device selection `cuda -> mps -> cpu`, no code change between machines. On the M4 Pro, use MPS for embeddings and multiprocessing for chunking. |
| Out of scope | Source acquisition (crawler, collectors, universe scripts) and the LLM/serving side. |

### Target baseline schema (the contract with the LLM side)
Required: `doc_id`, `chunk_id`, `text`, `embedding` (fixed-size 1024), `source_type`, `symbol` (nullable), `source_filename`, `ingested_at`, `embedding_model`, `pipeline_version`, `ingest_run_id`.
Optional (nullable): `source_url`, `doc_type`, `published_at`, `page_range`, `section_path`, `vendor`, `content_hash`.
Dynamic: `metadata_json` (string) holds every extra vendor field and is never dropped.

---

## 1. Files to DELETE (Source side or dead code)
- `pipeline/Retrieval/Document/document_crawler.py` (whole file).
- `pipeline/Retrieval/Numeric/*.py` (all six collectors and the base class), `pipeline/Retrieval/registry.py`, the `Retrieval/__init__.py` files.
- `scripts/build_universe.py`, `scripts/url_discovery.py`, `scripts/__init__.py`.
- `config/company_universe.csv`, `config/company_urls.json`, `config/sources/*.csv`, `config/sector_data_sources.yaml` (empty).
- `test_async_document_crawler.py` (broken: awaits a non-async function).
- `pipeline/document_template/*.py` (all zero-byte) and `pipeline/document_template/registry.py`.
- `requirements.txt` (an unrelated Aider/litellm dump that lacks torch, docling and lancedb; `uv.lock` is authoritative).
- `data/embedding_index.db` (stale SQLite cache, no longer read), `data/pipeline.log`, `data/16 log 2nd run`, `data/log run 14- 20 junly`, all `.DS_Store`.
- `data/lancedb/**`: untrack it. It holds a 21-chunk test run in the old schema.
- `project-structure.txt` (stale; regenerate if needed).
- `.github/workflows/pipeline-test.yml` (empty). Replace it, don't leave it.
- `Doc/current/*.md` and the `README.md` sections that describe removed stages. Rewrite after the refactor.
- `tests/rag_authz*.rego`, `tests/prompt_guard*.rego`, `.github/workflows/opatest.yml`: these are LLM-side. Move to the LLM repo, or leave them but do not treat them as Processor tests.

## 2. Files to MODIFY

### `main.py`
- L29, L60-71 `load_known_symbols`, L130-147: remove the dependency on `company_urls.json` and symbol validation. New CLI is input-driven: `main.py ingest --input <dir|manifest> [--stage ...] [--workers N] [--run-id ID]`.
- L5-19 docstring and L74-114 argparse: replace the three invocation modes. Drop `--all-symbols`/`--symbol` as the primary selector (keep `--symbol` as an optional filter).
- L35-57 `setup_logging`: `Path("data")` and `data/pipeline.log` are CWD-relative. Use `config.settings` paths, and add the run id to the log format.
- L149-159: exit non-zero when any file failed, not only when a stage raised.

### `pipeline/pipeline.py`
- L66 `from ...document_crawler import run`: delete. This import pulls `crawl4ai` at import time, so the Processor can't start without Source-side dependencies.
- L68, L87 `Normalizer` import and instantiation: delete (deferred and reads dirs that no longer exist).
- L75-78 `STAGE_ORDER` and L96-97 `_call_document_crawler`: delete the crawler stage. New stages: `discover` (enumerate and register input docs), `index` (parse -> clean -> chunk -> embed -> upsert).
- L104-141 `run()`: stop treating "per symbol" as the unit of work. The unit is the **document** (`doc_id`). Return per-document status (`indexed | skipped_duplicate | failed | unsupported`) and aggregate counts.
- L145-161 `_normalize_stage_result`/`_validate_metadata`: a stage returning `None` currently counts as success. Make a missing or invalid result a failure.
- L1-61 docstring: remove all `[DEFERRED]`/`[CONFIRM]` narrative about stages that are gone.

### `config/settings.py`
- L5-14: remove `RAW_NUMERIC`, `TRANS_*`, `CLEANED_*`, `CHUNKED`, `DONE`, `RAW_DOCUMENTS_OTHER`, `COMPANY_*` (all unused after the refactor).
- L20-32: stop creating directories as an import side effect. Move to an explicit `ensure_dirs()` called from `main`.
- Add (single source of truth, so these stop being duplicated in three files): `LANCEDB_PATH` (absolute, from `BASE_DIR`), `LANCEDB_TABLE`, `EMBEDDING_MODEL = "BAAI/bge-m3"`, `EMBEDDING_DIM = 1024`, `DISTANCE_METRIC = "cosine"`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `PIPELINE_VERSION`, `INPUT_DIR`, `NUM_WORKERS`, `COMPUTE_BUDGET` (the OCR flags currently hard-coded in `parser.py`).
- Env-var overrides for all of the above, for the cloud move.

### `pipeline/type_router.py`
- L61-63 and L78-107 `route_zip_file`: **bug**. `finally: shutil.rmtree(temp_extract_dir)` runs before the caller parses the returned paths, so every zip member is gone by parse time. Extract into a per-document scratch dir that lives until the document is fully processed (context manager owned by the indexer), and clean up afterward.
- L29-46 `get_file_type`: **bug**. On an unknown extension it returns a MIME string, but L69 checks `file_type in HANDLER_MAP`, which only has extension keys, so the magic-bytes fallback never matches. Add a MIME -> handler map, and use extension and sniff together (vendors often mislabel files).
- L13-27 handler maps: add `.json` -> `json_handler`; `.csv`/`.xlsx`/`.xls` -> `tabular_handler`; `.htm` -> structured doc; `.ppt`/`.doc` -> convert or report unsupported explicitly. Return a structured result (`handler`, `path`, `detected_type`, `reason`) instead of `None`, so unsupported files are counted and logged instead of silently skipped.
- L81 `scratch_dir / ...` assumes a `Path`; the indexer passes `Path`, but `_index_file` passes `str`. Normalize the type.
- Zip safety: cap member count, uncompressed size and nesting depth.

### `pipeline/indexer.py` (becomes the orchestrator for batch ingestion)
- L50-63 manifest/url_map block: delete (crawler-specific). Replace with a **vendor metadata loader** that reads an optional sidecar (`<file>.meta.json` or a batch manifest) and returns a dict of optional fields. Missing or malformed metadata yields `{}` plus a warning, never an exception.
- L44-127 `index_symbol`: replace with `index_batch(documents, run_id)`. Remove the symbol-centric loop and the scratch dir under raw input (L66, L131).
- L84-90: `tabular_handler` is currently skipped. Wire it to the new tabular path (see `tabular.py` below) and add a `json_handler` path.
- L107-111: **bug**. `n == 0` is counted as `files_failed`, which misreports files whose chunks were all already indexed. Return an explicit status per file.
- L129-172 `_index_file`:
  - L134 parser: pass the real scratch dir and the compute budget.
  - L147-162: stop hand-building metadata. Build the baseline-schema record. `doc_type="unknown"` (L159), `downloaded_at=now` (L160, mislabeled) and `chunk["page_range"]` (L153) should come from the vendor metadata or be null.
  - L149: the content hash is computed per chunk. Also compute a **document-level hash** (`doc_id = sha256(file bytes)` or vendor id) for append-only duplicate detection.
  - L164-168: `VectorStore()` is instantiated per file and `store.run` is called per file. Hold one store, and write in batches.
  - L172: return value must come from the store's actual written count, not `len(embedded)`.
- Parallelism: file-level and chunk-level work runs in a process pool (use the `spawn` start method; do not fork after torch/MPS initialization). Embedding stays in the main process, on one device, and consumes chunks from a queue. See `embedder.py`.
- Add `ingest_run_id`, `pipeline_version`, `embedding_model` to every row.

### `pipeline/parser.py` (keep, rewrite the core)
- L40-53 `__init__`: builds `PdfPipelineOptions` but only OCR options; no table-structure options are set. Enable table structure (`do_table_structure`, accurate mode) explicitly **[VERIFY option names]**. Read the budget from settings (L55-63 `_load_compute_budget` is a placeholder).
- L44 and L117-129: OCR defaults to off, so scanned PDFs produce nothing and are indistinguishable from failures. Detect low-text pages, run OCR only on them (budget `max_pages_for_ocr` is defined and never used), and record `ocr_pages` in the output.
- L76-106 `run()` item mapping: **[VERIFY]** the `hasattr(item, 'table'/'header'/'list_item')` checks. To my knowledge docling's `TableItem`, `SectionHeaderItem` and `ListItem` do not have those attributes.
  - If so, tables go through the `.text` branch (or are dropped), headers become paragraphs, and `section_path` stays empty.
  - Rewrite using `isinstance(item, TableItem | SectionHeaderItem | ListItem | TextItem | PictureItem)` and `item.label`. Use the `level` returned by `iterate_items()` (currently ignored) and the `SectionHeaderItem.level`.
- L82 `export_to_dataframe(...).to_markdown`: needs `tabulate`, which is not a declared dependency **[VERIFY]**. Emit table blocks with structured cells (`rows`, `header`) as well as the markdown text, so the chunker can repeat the header row.
- L107-109 per-item `except Exception: continue`: count and report skipped items in the output so partial parses are visible.
- L116-311: **delete** `_configure_engine`, `_translate_document_to_ir`, `_extract_elements_from_document`, `_extract_children_recursive`, `_element_to_ir_block`, `_get_block_type`, `_extract_text_content`, `_extract_table_content`, `_to_markdown_table`, `_extract_image_content`, `_get_level`. They are unused, and `_extract_table_content`/`_to_markdown_table` return **fake placeholder tables** ("Column 1 | Column 2 / Data 1 | Data 2").
- L313-317 `parse_file`: constructs a new `DocumentConverter` per call (expensive). Remove it or reuse a cached parser.
- Return value: `{blocks, page_count, ocr_pages, warnings}` instead of a bare list, so the cleaner and indexer can record parse quality.
- Multiprocessing: each worker owns its own `Parser` (a `DocumentConverter` is heavy and not picklable). Initialize it in the pool initializer.
- Page numbers: every block must carry `page_number`. Pass through `None` explicitly for formats with no pages (HTML, JSON).

### New: `pipeline/tabular.py` (JSON / CSV / XLSX)
- New module, because docling does not cover these. For each file, parse to a table with a header row. Detect the header, types and units.
- Emit table blocks in the same IR as the parser (`type="table"`, `rows`, `header`, `sheet`, `row_range`). Chunk by **row groups with the header row repeated** (never mid-row), so numeric context survives.
- Large files: stream with a row cap per chunk and a total cap per file, with a logged warning when truncated.
- Flag for the later design discussion: whether to also store raw numeric rows in a separate LanceDB table for exact queries, since embeddings are poor at precise numbers. Out of scope until you decide.

### `pipeline/cleaner.py`
- L55-64 `run`, L68-108 `_clean_numeric`, L110-144 `_clean_document`, `_clean_json_file`: delete (file-based legacy paths; numeric moves to `tabular.py`).
- L216-243 `_detect_dynamic_deletion_candidates`: **bug**. Absolute threshold `3` pages deletes any short block repeating on 3+ pages ("Nil", "N.A.", "Rs. in crore", recurring section headings). Make it relative (for example appears on >= 40% of pages and the document has >= 8 pages), position-aware (top or bottom band by bounding box), and restrict to `paragraph`/`page_header`/`page_footer` labels, never `header` (section headings). Log what was removed.
- L42-50 `DENYLIST_PATTERNS`: "Confidential", "Copyright", "Powered by" only match whole-block text, so this is effectively dead. Decide whether they should be substring matches; keep them config-driven, not hard-coded. A bare page-number block (`"12"`) is mentioned in the docstring but not handled.
- L146-182 `render_blocks_to_text` and L165-168: **bug**. The `[[PAGE:n]]` marker is only emitted when the page changes, so chunks that fall wholly inside a page have no marker and get `page_range="unknown"` or a wrong range. Stop encoding provenance in the text stream. Carry `page_number`/`page_range` as structured per-segment metadata through the chunker.
- L170-181: table blocks are emitted as plain paragraphs. Keep them as separate table segments, and don't let the splitter cut them.
- L257-267 `_normalize_block`: collapses all whitespace, which also flattens list/code structure. Apply it only to prose blocks.
- Remove imports of removed settings (L36).

### `pipeline/chunker.py`
- L45, L65 `_global_chunk_id`, `_generate_chunk_id`: delete the class counter. **Bug**: `chunk_id = md5(content)[:6] + "_" + idx` has 24 bits of entropy, is per file, and is not scoped to a document, so it collides across files and shifts when a document changes. New id: `chunk_id = f"{doc_id}:{chunk_index:06d}"` (deterministic, document-scoped).
- L49-50: `chunk_size=1000`/`overlap=100` characters are hard-coded. Move to settings and measure in **tokens** (bge-m3 accepts 8192; characters under-use it). Tune with a small retrieval eval, not by guess.
- L127-168 `chunk_text`: accept structured segments (prose, table, with page numbers and section path), not one Markdown string.
  - Never split a table mid-row. Repeat the header row in each table chunk.
  - Compute `page_range` from the segments each chunk actually spans.
  - L144 `section_path` depends on headers surviving the parser (see `parser.py` [VERIFY]).
- L85-125 `run`/`_chunk_file`: delete (legacy disk path), and the CHUNKED import (L28).
- Multiprocessing: make chunking a pure function `chunk_document(doc) -> list[Chunk]` that is safe in a process pool (no shared counters, no torch import in workers).

### `pipeline/embedder.py`
- L26-27 constants: delete; use settings.
- L37-48 `get_device`: **order is wrong for your requirement**. Currently `mps -> cuda -> cpu`. Change to `cuda -> mps -> cpu`, with an env override (`EMBED_DEVICE`) and a logged choice. Not cached in a module global that breaks under multiprocessing.
- L51-71 `load_embedding_model`: `print` -> `logger`. Keep the 1024-dim check, but use the model's reported dimension against `EMBEDDING_DIM` and fail loudly. Set `model.max_seq_length` explicitly. Use fp16 on cuda/mps **[VERIFY quality on bge-m3]**.
- L73-99 `get_existing_hashes`: global `content_hash IN (...)` dedup. **Bug**: it drops identical text across different documents or symbols, so rows are missing for some queries. Remove it. For an append-only load, dedup at the **document** level (`doc_id` already present -> skip the whole file), not chunk level. Also this builds an unbounded SQL `IN` string.
- L114-177 `embed_chunks`: L164 `model.encode(batch_texts)`: add `normalize_embeddings=True` (cosine consistency), a larger batch size tuned to the M4 Pro's memory (32 is small), sort by length to reduce padding, `show_progress_bar=False`, and OOM backoff (halve the batch on failure). Replace every `print` with logging. Return embeddings as a numpy float32 matrix, not per-row `.tolist()`.
- L137-148: remove the "return `[]` when everything is skipped" semantic (it caused the false "failed" counts).
- L180-243 `Embedder` class, `_embed_file`, `_generate_dummy_embedding`: delete (legacy, and the dummy generator is a hazard).
- L246-273 `__main__` demo: broken (`KeyError: 'text'`, seen 16 times in the logs). Delete or turn into a test.
- Embed model loading must happen only in the main process, after workers are spawned.

### `pipeline/vector_store.py`
- L26-39 `CompanyDocument` schema: replace with the baseline schema from section 0.
  - `embedding` must be `Vector(1024)` (fixed-size list), not `List[float]`, or ANN indexing is unavailable **[VERIFY against the committed table]**.
  - Make optional fields nullable (`Optional[str]`), so missing vendor metadata can't fail inserts.
  - Add `doc_id`, `source_type`, `ingested_at`, `embedding_model`, `pipeline_version`, `ingest_run_id`. Rename `content` to `text` per the agreed contract.
- L59-137 `run`:
  - L72, L75-81: stop defaulting missing values to `''` and only warning. Validate required fields (`doc_id`, `chunk_id`, `text`, `embedding`); coerce optional fields to null.
  - L101, L103-105: `timestamp` is always empty; `doc_type` always "unknown". Source from the record.
  - L108 `metadata_json`: keep, but never nest the whole metadata dict twice.
  - L113-131: builds a DataFrame and uses `merge_insert("chunk_id")`. The logged production failure ("partial-schema source: non-nullable columns not provided") shows a schema/record mismatch. Build an Arrow table from the schema, so mismatches fail at construction. For append-only loads, use `table.add` in batches (5-10k rows); use `merge_insert` only for the explicit `--allow-replace` mode.
  - L117-121 broad `except Exception` around `open_table`: only treat "table not found" as create; surface other errors.
  - After load: create the ANN index (IVF-PQ or HNSW) with `metric="cosine"`, plus scalar indexes on `doc_id` and `symbol`, once per batch run, not per file.
  - L133-137: `raise` after logging is right, but the caller must record per-document failure, not abort the batch.
- L139-252 `search`, `get_by_id`, `get_by_symbol`, `get_metadata`: retrieval belongs to the LLM side. Keep only a minimal `get_by_doc_id`/`count` for verification and tests.
  - L153, L197, L219, L241: f-string filters with unvalidated `symbol`/`chunk_id`. Escape or validate if kept.
  - L56: dimension check should use the settings constant.
- L43, L47: `db_path` and `table_name` hard-coded, CWD-relative. Use settings.

### `pipeline/normalizer.py` and `pipeline/document_template/classifier.py` + `rules.yaml`
- `normalizer.py`: delete (not in the active chain, reads `data/cleaned/*.json`, which no longer exists, and appends to a shared Markdown file).
- `classifier.py`/`rules.yaml`: only 4 India-specific regex rules and the classifier is called only by the deleted normalizer. Decision: since vendor metadata may supply `doc_type`, keep a **fallback-only** classifier (used when `doc_type` is missing), moved to a single module. L31-32/L46-47 `print` -> `logger`; L22-23 `config` may be `None` for an empty YAML. If you don't want inferred types, delete it and store `doc_type = NULL`.

### `pyproject.toml`
- L2: rename the project (it is no longer a "data harvester").
- Remove unused/Source-side dependencies: `crawl4ai`, `yfinance`, `beautifulsoup4`, `tenacity`, `httpx` (unless used), `flagembedding`, `pdfplumber`, `pypdf`, `pypdf2`, `python-docx`, `python-pptx` (docling handles Office formats). Keep: `docling`, `lancedb`, `sentence-transformers`, `torch`, `langchain-text-splitters` (or drop for a token-based splitter), `pandas`, `pyarrow`, `pyyaml`, `python-magic`.
- Add if needed: `openpyxl` (xlsx), `tabulate` **[VERIFY]**, `tiktoken` or the HF tokenizer (token-based chunking).
- L30-36 and L38-42: the dev dependency list is duplicated (`optional-dependencies` and `dependency-groups`). Keep `dependency-groups` only.
- Document the system dependency `libmagic` (`brew install libmagic`) and add a startup check for it.

### `.gitignore` / `.cursorignore`
- Add `data/lancedb/`, `data/*.log`, `data/*.db`, `data/input/`, `.DS_Store`. Currently runtime artifacts were committed.

### `README.md` and `Doc/current/*`
- Rewrite after the refactor. Currently describes `exporter.py`, `--no-loop`, `data/cleaned/` and a SQLite cache that do not exist.

## 3. Files to ADD
- `pipeline/tabular.py` (described above).
- `pipeline/metadata.py`: loads optional vendor metadata and sidecars, normalizes field names, never raises.
- `pipeline/schema.py`: the baseline schema and a single `Chunk`/`Document` dataclass shared by chunker, embedder and store, so the contract is one definition.
- `tests/`: `test_type_router.py` (zip lifetime, MIME fallback), `test_parser.py` (golden fixture PDFs with a table and headings), `test_cleaner.py` (short repeated text is preserved on short docs), `test_chunker.py` (table header repeat, stable ids, `page_range`), `test_store.py` (missing metadata inserts, fixed-size vector, cosine ordering), `test_idempotency.py` (second run: 0 new rows, 0 failures).
- `.github/workflows/pipeline-test.yml`: run `ruff` + `pytest` on the fixtures (CPU, no model download in unit tests; stub the embedder).

## 4. Execution order (after your review)
0. **Verify the [VERIFY] items** (`uv sync`, docling item fields, the committed LanceDB schema, `tabulate`).
1. Define `schema.py` and `settings.py` (the contract).
2. Delete Source-side files and dependencies, re-point `main.py` and `pipeline.py`.
3. Fix `type_router.py`, then `parser.py`, then `tabular.py`.
4. Fix `cleaner.py` and `chunker.py` (provenance, ids, tables).
5. Fix `embedder.py` (device order, normalization, batching) and `vector_store.py` (schema, batch add, index).
6. Wire the multiprocess orchestration in `indexer.py`.
7. Tests, CI, docs, repo cleanup.

## 5. Residual risks to confirm with you
- MPS plus multiprocessing: use `spawn` and keep the model in one process. Parallel embedding on one MPS device gives no gain.
- Numeric accuracy: embedding tabular rows will not give exact-value retrieval. Consider a structured side table later.
- Vendor licensing or entitlement fields (the `allowed_roles` the OPA policy expects) are not in the baseline schema. They would travel in `metadata_json` unless you promote them.


---
## Plan amendments (2026-11-11)
- **Step 3 gained a shared block format.** `Block`/`TableData`/`ParseResult` live in `schema.py`; the parser and `tabular.py` both feed it, and Step 4's cleaner/chunker consume it. Tabular files bypass clean/chunk and stream straight to table `Chunk`s.
- **chunk_id** is `sha256(doc_id, chunk_index, text)[:32]` (decided with the user), not `{doc_id}:{index}`.
- **OCR** uses RapidOCR (torch backend); EasyOCR was never installed. `ocr_languages` setting removed.
- **Step 4 must also:** consume `Block` objects (drop the dict-based `clean_ir_blocks` path), call `tabular.table_chunks` for `BLOCK_TABLE` blocks, add `tiktoken`/HF-tokenizer sizing, and carry `page_number` as structured data.
- **Step 6 must also:** wire `tabular.read_table_chunks` and `metadata.build_source_document` into the indexer, and remove the unit-name-as-`symbol` shim.

- **Recursive ingestion (2026-11-11).** `main.py ROOT` crawls any tree with `rglob`; the unit/subdirectory concept is gone. `Indexer.index_files(files, root)` replaces `index_symbol`. Step 6 builds on `index_files`, not `index_symbol`.
- **50 LLM columns (2026-11-11).** Added to `ChunkRecord` as nullable columns (`schema.EXTENDED_FIELDS`); Step 5's `vector_store.py` must write them via `schema.build_record`.
