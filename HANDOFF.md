# Handoff: building the vector DB from a collector batch

You get the repo and one collector batch. The collector, its search engines and the agent's LLM are not needed:
the processor alone turns the batch into LanceDB.

## What you need

- This repo (branch `fix/e2e-findings`).
- The batch folder `20261124-182029/` (379 MB: `files/`, `manifest.json` with a sha256 per file, `READY`,
  `universe.sqlite3`). The processor verifies every hash before ingesting and refuses a batch that does not match.
- macOS on Apple Silicon (tested: M4 Pro 48 GB, M5 Max 128 GB) or Linux; `brew install libmagic`; `uv`.

## Commands

```bash
(cd processor && uv sync)                    # first run downloads bge-m3 and the docling models (~3 GB)
./rag step processor.health                  # environment + RAM headroom
MAX_WORKERS=10 NUM_WORKERS=10 ./rag step processor.ingest --batch /path/to/20261124-182029
./rag step processor.quant-sync              # universe snapshot -> quant DB
./rag step processor.status                  # rows, ledger (out-of-scope files, unindexed pages, memory peaks)
```

The database lands in `processor/data/lancedb` (`documents` table). Re-running the ingest is safe: documents
already stored are skipped by content hash. Any other folder of files: `./rag step processor.ingest-dir <folder>`.

## Settings for an M5 Max (128 GB)

- `MAX_WORKERS=10 NUM_WORKERS=10`. Measured here: a parse worker peaks at 4–6 GB (big PDFs are converted in
  40-page windows), the bge-m3 embedder at ~15.5 GB (GPU memory included). Ten workers ≈ 75 GB.
- More than ~10 does not help: the embedder runs at ~95 % GPU and is the bottleneck once workers keep it fed.
  After one ingest, `processor.health` → `memory.headroom` says how many workers fit from the measured peaks.
- Keep the defaults otherwise (`PDF_WINDOW_PAGES=40`, `MAX_PAGES_FOR_OCR=150`, offer documents skipped).

## What is excluded, and why

| Excluded | How | Why |
|---|---|---|
| 3 IPO offer documents (2 DRHPs + 1 prospectus, 430–510 pages) | triage (`skipped_out_of_scope`) + `processor/config/denylist.txt` | Not delisting records; they were 44 % of the chunks and half the ingest time |
| 34 Hindi / Marathi newspaper copies of NSE IPN/FPN notices + 1 copyright PDF | `processor/config/denylist.txt` | Old Devanagari font encodings: the text extracts as garbage. The 9 English copies of the same notices are kept |

Expected result for this batch: ~280 documents indexed, ~6,300 chunks, 0 failed.

## Cost per file (M4 Pro 48 GB, 4 workers; `audit_runs/perf/`)

| Class | Files | Median wall | Slowest | Worker peak |
|---|---|---|---|---|
| PDF 1–10 pages | 241 | 9 s | 103 s | ≤5.5 GB |
| PDF 11–50 pages | 43 | 32 s | 274 s (36-page scan) | ≤6 GB |
| PDF 51–150 pages | 5 | 109 s | 1,234 s (140-page scan, 125 pages OCR) | ≤4.3 GB |
| HTML | 21 | 17 s | 43 s | <2 GB |

Rules of thumb: text pages 0.7–1.7 s each; OCR ~5.5 s per scanned page (RapidOCR on CPU, English + Chinese
models, no Hindi); embedding scales with chunks (~47 per page on dense documents). The whole batch: 22.8 min
here (was 45.8 min before the changes, with the IPO documents and without windowing).

## Status notes

- **OCR is a temporary pass**: it runs and recovered a fully scanned 140-page PDF, but its text quality was not
  checked against ground truth and it reads no Hindi/Marathi (see AUDIT.md, open item 3).
- **Z-factors** use the target bracket **3,500–12,500 cr** (the default of `processor.quant-zfactor`). They stay
  `noop` until price / `market_cap_cr` rows are ingested; this batch has none.

## Things you will see in the logs

- `reading_order_rb ... something went wrong`: docling falls back to its default reading order; the file is fine.
- `RapidOCR returned empty result`: a page OCR could not read (blank or a picture); counted in `pages_unindexed`.
- `pages_unindexed` in the ledger: PDFs whose low-text pages stayed without text (over the OCR budget, or OCR
  found nothing).

## Removing a document later

`./rag step processor.purge --file <name>` shows what would go; add `--yes` to delete. The doc_id goes on
`processor/data/denylist.txt` (local). To share the decision, copy the line to `processor/config/denylist.txt`.
