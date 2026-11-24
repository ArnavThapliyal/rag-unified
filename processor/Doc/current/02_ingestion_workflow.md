# 2. Ingestion workflow

## Command

```bash
uv run python main.py [ROOT] [--workers N] [--dry-run]
```

| Option | Effect |
|---|---|
| `--workers N` | Number of parse/chunk processes. Defaults to `NUM_WORKERS`. `0` runs everything in one process. |
| `--dry-run` | Discover and detect file types only. Prints what would be processed and what would be skipped, with reasons. Nothing is parsed, embedded or written. |

`ROOT` is a directory or a single file. It defaults to `INPUT_DIR` (`data/input`). If ROOT does not exist, the command prints an error and exits with code 1 before creating anything.

Each invocation is one batch run with its own run id, which appears on every log line and in the `ingest_run_id` column. The run prints a summary and a list of failed files:

```
7 file(s) found under /data/vendor_drop: 4 indexed, 1 failed, 2 skipped | chunks written: 318
By status: indexed: 4, skipped_duplicate: 1, skipped_unsupported: 1, failed: 1
Indexed by type: csv: 1, html: 1, pdf: 2
Failed: broken.docx
Run id: 3f9c2a71d0be
```

| Exit code | Meaning |
|---|---|
| 0 | No file failed. Skipped files (already ingested, unsupported, empty) do not count as failures |
| 1 | At least one file failed, ROOT is missing, the index refresh failed, or the run was interrupted |

## Discovery (`pipeline.pipeline.discover_files`)

ROOT is walked with `Path.rglob("*")` and the result is sorted, so the run order is deterministic.

A path is **not** ingested when it is:

- a hidden file, or inside a hidden directory (any path part starting with `.`);
- a symlink (a linked file would otherwise be ingested twice);
- inside the pipeline's working directories (`SCRATCH_DIR`, `LOG_DIR`, `LANCEDB_PATH`), which matters when ROOT is the data directory itself;
- a metadata file: `*.meta.json`, `metadata.json`, `manifest.json`.

Folder names and nesting depth are ignored.

## Routing (`pipeline.type_router`)

`open_routed(path, scratch_dir)` is a context manager that yields a list of `RoutedFile` objects, one per processable unit.

| Detected type | Handler | Processed by |
|---|---|---|
| `pdf`, `docx`, `pptx`, `html` | `structured_doc_handler` | `parser.py` (docling) |
| `csv`, `xlsx` (incl. `.xlsm`), `xls`, `json` (incl. `.jsonl`, `.ndjson`) | `tabular_handler` | `tabular.py` |
| `doc`, `ppt`, anything unrecognised | `unsupported` | Counted as skipped, logged with a reason |

Detection rules:

- The type comes from the file's content (libmagic) combined with its extension. When they disagree, content wins.
- Ambiguous content types (`text/plain`, `application/octet-stream`) fall back to the extension.
- Office files sometimes sniff as plain zip; in that case a `.docx`/`.pptx`/`.xlsx` extension is trusted.

Zip archives:

- Members are extracted into a temporary directory under `SCRATCH_DIR` that exists until the `with` block exits, then is removed.
- Each member is routed like a normal file and recorded as `archive.zip!member.pdf`.
- Path-traversal entries, `__MACOSX` and `.DS_Store` are skipped.
- Limits: `ZIP_MAX_MEMBERS`, `ZIP_MAX_UNCOMPRESSED_BYTES`, `ZIP_MAX_DEPTH` (nested zips).

## Metadata (`pipeline.metadata`)

`build_source_document(routed, resolver)` returns a `SourceDocument`. It never raises because of metadata: a missing, empty or malformed source produces null fields and a logged warning.

Lookup order, where earlier sources win and later ones only fill gaps:

1. **Sidecar**: `<filename>.meta.json` beside the file (for a zip member, the sidecar of the archive).
2. **Batch file, per-file entry**: `metadata.json` → `files.<filename>`.
3. **Batch file, defaults**: `metadata.json` → `defaults`.
4. **Legacy `manifest.json`**: supplies `source_url` only.

Batch files are searched from the file's directory up to ROOT; the nearest directory wins. Files above ROOT are ignored.

```json
{
  "defaults": {"vendor": "acme", "sector": "IT"},
  "files": {
    "q2_results.pdf": {"ticker": "TCS", "doc_type": "quarterly_results", "roles": ["analyst"]}
  }
}
```

Field mapping (case-insensitive):

| Column | Accepted vendor keys |
|---|---|
| `symbol` | `symbol`, `ticker`, `scrip`, `scrip_code` |
| `source_url` | `source_url`, `url`, `link`, `source` |
| `doc_type` | `doc_type`, `document_type`, `type`, `category` |
| `vendor` | `vendor`, `provider`, `supplier`, `publisher` |
| `published_at` | `published_at`, `published`, `publication_date`, `date`, `published_date` |
| `allowed_roles` | `allowed_roles`, `roles`, `entitlements`, `access_roles` |
| any financial column | the column's exact name, e.g. `isin`, `close`, `roe_pct` |

Everything else, including the vendor's own document id, is stored in `metadata_json`. A financial value that cannot be converted to its column type becomes null and the original value is kept in `metadata_json`.

## Deduplication and re-runs

`doc_id` is the hash of the file's bytes, so:

- A file already in the table is skipped (`skipped_duplicate`). Re-running a folder writes nothing new and exits 0.
- The same bytes at two paths, or under two names, are ingested once per run; the second copy is reported as a duplicate of the first.
- A file whose content changed gets a new `doc_id` and is ingested as a new document. The old version is not removed (append-only).

## Failure model

- Failures are isolated per file. The reason is logged and shown in the summary, and the run continues.
- Unsupported, empty and already-ingested files are **skipped**, not failed.
- A document is written only when it has been processed completely, so a failed file leaves no rows behind.
- If a run is killed, the next run first removes any document that was left half-written, then carries on. Files completed before the kill are skipped as duplicates.

Details of the process layout and failure handling are in [04_processing_stages.md](04_processing_stages.md).
