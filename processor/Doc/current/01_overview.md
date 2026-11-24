# 1. Overview

## Scope

The RAG system is split into three stages owned separately:

| Stage | Responsibility | In this repo |
|---|---|---|
| Source | Acquire data. The data is now purchased and arrives as static files. | No |
| **Processor** | Parse, clean, chunk, embed, and load files into LanceDB. | **Yes** |
| LLM | Retrieval, authorisation, prompting, answering. | No (only its OPA policies are mirrored under `tests/`) |

The Processor is an **append-only batch loader**. It does not watch folders, does not run continuously, and does not handle superseded filings.

## Data flow

1. **Discover.** `main.py ROOT` crawls ROOT recursively with `Path.rglob`. No folder layout is required.
2. **Route.** `type_router` detects each file's type from its content and picks a handler. Zip archives are unpacked and their members routed the same way.
3. **Describe.** `metadata` builds a `SourceDocument` from whatever vendor metadata exists. Every field is optional.
4. **Extract.**
   - Documents (PDF, DOCX, PPTX, HTML) go through `parser` (docling), which returns structured blocks: headers, paragraphs, list items, tables.
   - Tabular files (CSV, XLSX, XLS, JSON) go through `tabular`, which streams rows straight into table chunks.
5. **Clean and chunk** (documents only). Boilerplate is removed and blocks are grouped into chunks. Tables are never split inside a row and repeat their header.
6. **Embed.** `BAAI/bge-m3` produces one normalised 1024-dim vector per chunk.
7. **Load.** `schema.build_record` validates each row, and the store appends each document to the `documents` table in one atomic write.

Steps 2 to 5 run in parallel CPU worker processes. Step 6 runs in a single process that owns the device. Step 7 runs in the main process. See [04_processing_stages.md](04_processing_stages.md).

## Module map

| Module | Role |
|---|---|
| `main.py` | CLI, run id, exit code |
| `config/settings.py` | All tunables, environment overrides |
| `config/logsetup.py` | Logging for the main and child processes |
| `pipeline/pipeline.py` | `discover_files`, `dry_run`, `PipelineRunner` |
| `pipeline/indexer.py` | Starts the processes, accounts per file, writes to LanceDB |
| `pipeline/worker.py` | CPU work for one file: route, metadata, parse, clean, chunk |
| `pipeline/type_router.py` | Type detection, zip extraction |
| `pipeline/metadata.py` | Vendor metadata → `SourceDocument` |
| `pipeline/parser.py` | docling → `Block` list, on-demand OCR |
| `pipeline/cleaner.py` | Boilerplate removal on blocks |
| `pipeline/chunker.py` | Blocks → token-sized chunks; tables isolated |
| `pipeline/tabular.py` | Tabular files and in-document tables → table chunks |
| `pipeline/embedder.py` | bge-m3 vectors, normalised |
| `pipeline/vector_store.py` | Append-only LanceDB writer, indexes |
| `pipeline/schema.py` | `ChunkRecord`, ids, `build_record`, parser IR |

## Design decisions

- **No required input layout.** Folder names are never interpreted. The tree matters only for finding batch metadata files.
- **Nothing crashes on missing metadata.** Vendor metadata is inconsistent, so every vendor field is nullable and unknown fields are preserved in `metadata_json`.
- **Deterministic ids.** `doc_id` is derived from file bytes and `chunk_id` from `doc_id`, chunk index and chunk text, so the same input always yields the same ids.
- **Tables are isolated.** Table chunks carry `content_kind="table"` so they can later move to a relational store without re-parsing. For now they are embedded as text.
- **One process owns the embedding device.** CPU worker processes parse and chunk; a single process holds bge-m3 on the device (`cuda → mps → cpu`) and receives batches through a bounded queue. Start method is `spawn`.
- **A document is all-or-nothing.** Its rows are written together when the whole document has been embedded, so a `doc_id` in the table always means a complete document, and re-runs can skip it safely.
- **`allowed_roles` is a real column.** The OPA policies in `tests/rag_authz.rego` read it, so it is not buried in `metadata_json`.
