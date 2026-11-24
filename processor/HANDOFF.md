# Handoff: rag-processor refactor

Last updated: 2026-11-16. Branch `refactor/processor-only` (branched from `main` at `e970c11`), pushed to `origin`.

## Goal
Reduce this repo to the **Processor** only: purchased mixed-format files (PDF, PPTX, DOCX, HTML, JSON, CSV/XLSX/XLS) are parsed, cleaned, chunked, embedded with bge-m3 (1024-dim) and ingested into LanceDB (cosine). Source (acquisition) and LLM are out of scope. Append-only batch load.

## Progress

| Step | Status |
|---|---|
| 0. Verify claims | Done |
| 1. `schema.py` + `settings.py` | Done |
| 2. Delete Source-side files/deps; recursive-root `main.py`, `pipeline.py` | Done |
| 3. `type_router.py`, `parser.py`, `tabular.py`, `metadata.py` | Done |
| 4. `cleaner.py`, `chunker.py` | Done |
| 5. `embedder.py`, `vector_store.py` | Done |
| 6. Multiprocess `indexer.py` + `worker.py`; tabular and metadata wired in | Done |
| 7. Tests, CI workflow, docs, repo cleanup | Done |

**What remains** is finishing the test-data run, validation on real purchased data, a few engineering follow-ups from the first run, and some decisions for the LLM side. See `NEXT_STEPS_PLAN.md`, sections 4 and 5.

## Current state (2026-11-16)

A first larger run was done on scraped test data and stopped part-way.

| Item | Where / what |
|---|---|
| Test data | `/Users/arnav/Projects/Software/test symbol data/`: 13 company folders, 354 files, 1.3 GB. `DATA_INDEX.md` there lists every file |
| How it was collected | Scripts in `/Users/arnav/Projects/Software/data_harvester/claude sample scrape script/` (`harvest_sample.py`, `market_files.py`, `variety_files.py`, `other_formats.py`, `build_index.py`), committed in that repo, not pushed |
| Per-company metadata | A `metadata.json` in each company folder sets `symbol`, `nse_symbol`, `isin`, `sector`, `industry`, `vendor` |
| Database | `test symbol data/lancedb`, table `documents`: 164 documents, 4,015 chunks |
| Coverage | 360ONE: 150 documents. POLYCAB: 14 of 20 items (6 PDFs unfinished). The other 11 companies: not started |
| Logs | `data/logs/test_symbol_data_ingest_2026-11-16_12-56-13.log` (all three runs combined) |
| Viewer | `view_db.py` (added by the user) inspects a database and runs test searches |

Bug found and fixed in that run: MPS device memory grew without bound until every embedding call failed. The embedder now pads batches to a small set of fixed shapes on MPS (commit `73dcac0`).

Still open from that run: scanned PDFs are very slow and a zip archive ties up one worker. (Fixed on `unified-pipeline`, 2026-11-24: killing the main process left worker processes behind; children now exit by themselves within ~2 s.)

## Where to read
- `README.md`: what it does, how to run it.
- `Doc/current/01-06`: overview, ingestion workflow, schema, processing stages, configuration, development.
- `NEXT_STEPS_PLAN.md`: what was built per step, decisions taken, what is left.
- `refactoring_flags.md`: the original audit (historical).

## Decisions (user-confirmed)
- Data includes tabular numeric data; vendor metadata is inconsistent, so every field is optional and nothing may crash on missing metadata.
- No required input layout: one ROOT, crawled with `Path.rglob`; folder names carry no meaning; file types come from content sniffing.
- LanceDB, bge-m3 1024-dim, **cosine**. Device order `cuda -> mps -> cpu`, no code change between machines.
- Multiprocessing start method **spawn**. CPU workers parse and chunk; bge-m3 lives in **one** process fed via bounded queues.
- Tables embedded as text for now, but isolated (`content_kind="table"`, `table_id`) so they can move to a relational DB later.
- `allowed_roles` is a top-level nullable column (the OPA policy depends on it).
- `chunk_id` hashes doc_id + chunk_index + chunk text.
- 50 downstream-LLM columns are nullable typed columns on `ChunkRecord`.
- Append-only: no superseded-filing handling.
- Table name is `documents` (was `company_documents`); the LLM side must read the new name.

## Decisions taken by Claude without explicit confirmation (review these)
- Record mode is on: price/fundamentals-style sheets become one chunk per row with typed columns filled (`TABULAR_RECORD_MODE=off` disables).
- The regex `doc_type` classifier and `normalizer.py` were deleted.
- Null `allowed_roles` is stored as null; no default.
- `.txt`/`.md` are not supported.
- Types chosen for the 50 columns: four ints, two bools, dates as strings, the rest floats.

## Working rules
- Commit after every major change, in small logical commits, for easy rollback.
- **No `Co-Authored-By` or other Claude attribution** in commits or PRs. Push only as the user over SSH.

## Risks to keep in mind
- Never fork after torch/MPS is initialised. Load the embedding model only in the embedder process.
- Numeric accuracy: embeddings are poor at exact numbers; keep tables isolated for a future structured store.
- RapidOCR covers English and Chinese only (no Hindi).
- Record mode on very large price files produces one vector per row.
- Each PDF worker holds docling's models in memory (1 to 5 GB each measured with 6 workers).
- On MPS, never feed the model freely varying batch shapes: device memory grows and is not released. The embedder's fixed-shape mode exists for this; keep it.
- Scanned PDFs dominate run time; decide the OCR policy before a large load.
- After a hard kill of `main.py`, look for leftover worker processes.

## How to resume
1. `cd /Users/arnav/Projects/Software/rag-processor && git checkout refactor/processor-only && git status`
2. `uv sync` if `.venv` is missing; `.venv/bin/python -m pytest -q` should pass (a few tests are skipped without `RUN_SLOW=1`).
3. To resume the test-data run (finished documents are skipped):
   ```bash
   O="/Users/arnav/Projects/Software/test symbol data"
   LANCEDB_PATH="$O/lancedb" WORKER_STALL_TIMEOUT_SECONDS=3600 caffeinate -i uv run python main.py "$O" --workers 6
   ```
   Pass `"$O/<SYMBOL>"` to do one company. Add `ENABLE_ML_OCR=false` for a much faster pass that skips scanned pages.
4. Look at the result: `uv run python view_db.py "$O"`.
5. For a real delivery: `uv run python main.py <folder> --dry-run`, then without `--dry-run`.
6. Tell Claude: "Read HANDOFF.md and NEXT_STEPS_PLAN.md, then continue with the follow-ups from the test run."


## Unified pipeline (branch `unified-pipeline`, 2026-11-24)

Sanidhya's Unified_Pipeline merged the collector with a quant package. It is split: the collector keeps collection and
the universe DB (repo rag-collector, also branch `unified-pipeline`), this repo gets the quant stage and ingests the
collector's outbox batches. Plan: `/Users/arnav/Projects/Software/RAG_PLAN.md`; inventory of his changes:
`/Users/arnav/Projects/Software/UNIFIED_PIPELINE_CHANGES.md`.

| Added | Where |
|---|---|
| Step contract (shared, identical file) | `pipeline/stepkit.py` |
| Batch verification, ledger, retries, universe install | `pipeline/batches.py` |
| Step CLI | `pipeline/steps.py` |
| Continuous mode | `pipeline/watch.py`, `main.py --watch` |
| Quant stage | `pipeline/quant/` (store, z_factor: his code; sync: adapters) |
| Orphan watchdog in child processes | `pipeline/indexer.py` |

Next: rag-unified (a new repo) runs an orchestrator agent over both repos' steps.
