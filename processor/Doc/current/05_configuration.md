# 5. Configuration

All configuration is in `config/settings.py`. Each setting below can be overridden by an environment variable **of the same name**, unless marked fixed. Importing the module creates no directories; `main.py` calls `ensure_dirs()` after argument parsing.

## Paths

| Setting | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `<repo>/data` | Parent of the defaults below |
| `INPUT_DIR` | `DATA_DIR/input` | Default ROOT when `main.py` gets no argument |
| `SCRATCH_DIR` | `DATA_DIR/scratch` | Temporary zip extraction |
| `LOG_DIR` | `DATA_DIR/logs` | `pipeline.log` |
| `LANCEDB_PATH` | `DATA_DIR/lancedb` | Database directory |

## Vector store

| Setting | Default | Notes |
|---|---|---|
| `LANCEDB_TABLE` | `documents` | The LLM stage must read this name |
| `DISTANCE_METRIC` | `cosine` | Fixed; part of the contract |

## Embedding

| Setting | Default | Notes |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | Changing the model requires a new table |
| `EMBEDDING_DIM` | `1024` | Fixed |
| `EMBED_DEVICE` | unset | Unset means auto: `cuda → mps → cpu`. Set `cuda`, `mps` or `cpu` to force |
| `EMBED_BATCH_SIZE` | `64` | Texts per encode call |
| `EMBED_FP16` | `false` | Off until bge-m3 quality in half precision is checked |
| `EMBED_QUEUE_MAX_BATCHES` | `8` | Capacity of the worker → embedder and embedder → main queues (backpressure) |
| `EMBED_MAX_SEQ_LENGTH` | `1024` | Longest input, in tokens, the model encodes; longer text is truncated |
| `MPS_EMPTY_CACHE_EVERY` | `50` | Encode calls between MPS cache releases |
| `MPS_MAX_BATCH_SIZE` | `32` | Upper limit on the batch size when the device is MPS; larger batches hold much more device memory |

## Chunking

| Setting | Default | Notes |
|---|---|---|
| `CHUNK_SIZE_TOKENS` | `512` | In bge-m3 tokens. Starting point, to be tuned with a retrieval evaluation |
| `CHUNK_OVERLAP_TOKENS` | `64` | |
| `TABULAR_ROWS_PER_CHUNK` | `40` | Maximum rows per table chunk |
| `TABULAR_MAX_ROWS_PER_FILE` | `2000000` | Rows beyond this are dropped with a warning |
| `TABULAR_RECORD_MODE` | `auto` | `auto`: one chunk per row for price/fundamentals-style tables. `off`: always row groups |
| `TABULAR_RECORD_MIN_MATCHES` | `3` | Header names that must match financial columns for record mode |

## Cleaning

| Setting | Default | Notes |
|---|---|---|
| `CLEAN_MIN_PAGES_FOR_REPEAT` | `8` | Shorter documents are never scanned for running headers/footers |
| `CLEAN_REPEAT_PAGE_FRACTION` | `0.4` | Share of pages a short paragraph must appear on to be removed |
| `CLEAN_MAX_BOILERPLATE_CHARS` | `80` | Longer paragraphs are never treated as boilerplate |
| `CLEAN_DENYLIST_PATTERNS` | see file | Whole-paragraph regexes that are always removed (not env-overridable) |

## Parallelism

| Setting | Default | Notes |
|---|---|---|
| `MP_START_METHOD` | `spawn` | Fixed. Forking after torch/MPS initialises is unsafe on macOS |
| `NUM_WORKERS` | CPU count − 2, at most `MAX_WORKERS` | Parse/chunk workers. Two cores are left for the main and embedding processes. `0` = single process |
| `MAX_WORKERS` | 4 | Hard ceiling on parse/chunk workers; `NUM_WORKERS` and `--workers` above it are lowered to it (each docling worker takes 1–5 GB) |
| `WORKER_STALL_TIMEOUT_SECONDS` | `1800` | A worker silent for this long is killed and its file marked failed. `0` disables |

## Writing

| Setting | Default | Notes |
|---|---|---|
| `WRITE_MAX_ROWS_PER_ADD` | `20000` | A document with more rows is written in parts, tracked by the ledger |
| `VECTOR_INDEX_MIN_ROWS` | `50000` | The vector index is built once the table reaches this size |

## Parsing

| Environment variable | Default | Notes |
|---|---|---|
| `ENABLE_ML_OCR` | `true` | Allow the OCR second pass |
| `MAX_PAGES_FOR_OCR` | `150` | OCR is skipped when more low-text pages than this are found (counted per file as `pages_unindexed`). Only the low-text page ranges are OCRed, ~5.5 s per page on CPU |
| `PDF_WINDOW_PAGES` | `40` | PDFs longer than this are converted in windows of this many pages, so a worker's memory does not grow with the document |
| `SCOPE_SKIP_OFFER_DOCUMENTS` | `true` | Skip IPO offer documents (DRHP / RHP / prospectus) before parsing (`skipped_out_of_scope`). Off for batches whose manifest says `scope_target` listed / both |
| `HF_OFFLINE_INGEST` | `true` | Ingest runs with `HF_HUB_OFFLINE=1` when bge-m3 and the docling models are already cached (a fresh install stays online to download them) |
| `MIN_CHARS_PER_PAGE_BEFORE_OCR` | `25` | A page with fewer characters counts as low-text |
| `TABLE_STRUCTURE_MODE` | `accurate` | `fast` or `accurate` (docling TableFormer) |
| `PARSER_DEVICE` | `cpu` | `cpu`, `auto`, `mps`, `cuda`. Keep `cpu` when running several workers |
| `PARSER_THREADS` | `1` | Threads per parser |

These four OCR/table values are exposed in code as the `COMPUTE_BUDGET` dict.

## Zip limits

| Setting | Default |
|---|---|
| `ZIP_MAX_MEMBERS` | `5000` |
| `ZIP_MAX_UNCOMPRESSED_BYTES` | 4 GiB |
| `ZIP_MAX_DEPTH` | `3` |

## Versioning

| Setting | Value | Bump when |
|---|---|---|
| `PIPELINE_VERSION` | `0.1.0` | Parse, clean or chunk behaviour changes |
| `SCHEMA_VERSION` | `2` | `ChunkRecord` changes |

## Running on different hardware

The code is the same on every machine; only environment variables differ.

| Machine | Suggested overrides |
|---|---|
| Apple Silicon (M4 Pro, 48 GB) | None. Device resolves to `mps`. Measured with 6 workers: 1 to 5 GB per worker, about 13 GB of device memory for the embedder at `MPS_MAX_BATCH_SIZE=32`. Lower `NUM_WORKERS` if memory is tight |
| CUDA server | None for the device. Consider a larger `EMBED_BATCH_SIZE`, and `EMBED_FP16=true` after a quality check |
| CPU only | Lower `EMBED_BATCH_SIZE`; expect slow embedding |

Child processes are started with `spawn` and read their settings from the environment again, so always configure through environment variables, not by editing values at runtime.

## Speed against quality

Scanned PDFs dominate run time. These settings trade quality for speed:

| Setting | Effect |
|---|---|
| `ENABLE_ML_OCR=false` | No OCR pass. Scanned pages yield little or no text; text PDFs are unaffected |
| `MAX_PAGES_FOR_OCR=<n>` | Documents with more low-text pages than this are not OCRed |
| `TABLE_STRUCTURE_MODE=fast` | Faster table recognition, somewhat less accurate |
| `WORKER_STALL_TIMEOUT_SECONDS=3600` | Give very large scanned documents more time before the worker is killed |

## System dependency

`python-magic` needs the libmagic library: `brew install libmagic` on macOS, `apt-get install libmagic1` on Debian/Ubuntu.

## Unified pipeline (collector batches, steps, quant)

| Setting | Default | Meaning |
|---|---|---|
| `COLLECTOR_OUTBOX` | `../collector/data/export` (else `../rag-collector/data/export`) | the collector's outbox that `--watch` and `steps ingest` read |
| `BATCH_LEDGER` | `data/processed_batches.jsonl` | one line per batch attempt (status, files, chunks, out-of-scope files, unindexed pages, triage summary, measured memory peaks) |
| `TRIAGE_DIR` | `data/triage` | `<run_id>.csv` per ingest: type, size, pages, low-text pages, size class, cost estimate, offer-document flag per file |
| `DENYLIST_FILE` | `data/denylist.txt` | doc_ids `processor.purge --yes` removed; never indexed again (`skipped_denylisted`) |
| `BATCH_MAX_ATTEMPTS` | `3` | attempts for a batch with failed files before it is given up |
| `WATCH_POLL_SECONDS` | `60` | seconds between outbox checks in `--watch` |
| `STEP_LOCK_DIR` | `data/locks` | per-step lock files |
| `STOP_FILE` | `data/STOP` | touch to stop `--watch` after the current batch |
| `UNIVERSE_DB` | `data/universe/universe.sqlite3` | latest universe snapshot from a collector batch |
| `QUANT_DB` | `data/quant.sqlite3` | quant stage store (`quant.state.json` next to it holds the sync watermark) |
| `QUANT_VALUE_FIELD` | `market_cap_cr` | the column Z-factors are computed on |
| `QUANT_TABLE` | `quant_factors` | LanceDB table with the Z-factor results |
