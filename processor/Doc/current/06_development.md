# 6. Development

## Environment

```bash
brew install libmagic
uv sync                 # Python 3.11, dependencies from uv.lock
```

Runtime dependencies: `docling`, `lancedb`, `sentence-transformers`, `torch`, `tokenizers`, `numpy`, `pandas`, `pyarrow`, `openpyxl`, `xlrd`, `python-magic`. Dev: `pytest`, `ruff`, `reportlab` (builds PDF fixtures).

No scraping libraries are declared. `beautifulsoup4` and `httpx` still appear in `uv.lock` because docling and huggingface-hub depend on them.

## Repository layout

```
main.py                     CLI entry point
view_db.py                  inspect a LanceDB table and run test searches
config/settings.py          configuration
config/logsetup.py          logging for all processes
pipeline/
  pipeline.py               discovery, dry run, PipelineRunner
  indexer.py                process orchestration, accounting, writes
  worker.py                 per-file CPU work (runs in worker processes)
  type_router.py            type detection, zip handling
  metadata.py               vendor metadata -> SourceDocument
  parser.py                 docling -> Blocks
  cleaner.py                boilerplate removal
  chunker.py                Blocks -> Chunks
  tabular.py                csv/xlsx/xls/json and in-document tables -> table Chunks
  embedder.py               bge-m3 vectors
  vector_store.py           LanceDB writer
  schema.py                 ChunkRecord, ids, build_record, parser IR
tests/
  test_*.py                 Processor tests
  stub_embedder.py          deterministic stand-in for the embedding model
  *.rego                    OPA policies owned by the LLM stage
Doc/current/                this documentation
```

## Tests

```bash
uv run pytest               # fast tests, no model downloads
RUN_SLOW=1 uv run pytest    # plus docling on real PDFs (text + scanned) and the real bge-m3 model
```

| File | Covers |
|---|---|
| `test_schema.py` | ids, 1024-dim fixed vector, cosine, LanceDB round-trip with and without metadata, the 50 financial columns, coercion |
| `test_type_router.py` | content sniffing, mislabelled files, zip lifetime and safety |
| `test_metadata.py` | missing/malformed metadata, aliases, precedence, nearest-directory resolution |
| `test_tabular.py` | header repetition, whole rows, encodings, truncation, record mode, table ordinals, bad rows |
| `test_parser.py` | DOCX and HTML block mapping (no models needed) |
| `test_parser_pdf.py` | PDF tables and OCR (slow) |
| `test_cleaner.py` | running footers removed only on long documents; values, headings and tables kept |
| `test_chunker.py` | token limits, overlap, section paths, table isolation, page ranges |
| `test_embedder.py` | order, normalisation, out-of-memory backoff, device order, no torch at import; real model (slow) |
| `test_vector_store.py` | create/reopen, schema mismatch refused, id validation, crash recovery, cosine index |
| `test_pipeline.py` | discovery on an irregular tree, dry run, run summary |
| `test_indexer_e2e.py` | full runs into LanceDB, inline and multi-process: mixed tree, re-run, duplicates, large documents, embedding failures |

End-to-end tests replace the model with `tests/stub_embedder.py`, which returns a fixed unit vector per text. The multi-process test starts real worker and embedder processes with `spawn`.

CI: `../.github/workflows/ci.yml` runs `ruff` and the fast tests (job `processor`). `../.github/workflows/opatest.yml` runs the OPA policy tests.

## Conventions

- Configuration comes from `config/settings.py` only. No hard-coded paths, model names or sizes in modules.
- Vendor metadata handling must not raise. Return null and log a warning.
- New rows are built only through `schema.build_record`.
- Do not import torch or load models at module import time. Worker processes are started with `spawn`, and the embedding model must exist in one process only.
- Use `logging`, not `print`, inside `pipeline/`.
- Keep table content identifiable: table chunks use `content_kind="table"` and carry their sheet and row range.
- Bump `PIPELINE_VERSION` or `SCHEMA_VERSION` when behaviour or the schema changes.

## Shell note

The default shell on macOS is zsh, which does not split unquoted variables into words. Run multi-path shell one-liners under `bash -c '...'`.
