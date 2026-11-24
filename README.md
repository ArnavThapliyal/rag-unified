# rag-unified

One repo for the whole RAG pipeline: **collect** NSE/BSE delisting documents from the web, **process** them
into a LanceDB vector database, and compute the **quant** factors, with **one agent** that can run every
step and one loop that runs everything continuously.

```
collector/   web research + fetching + parsing → master CSVs, universe DB, outbox batches   (was rag-collector)
processor/   outbox batches → docling parse → chunk → bge-m3 → LanceDB; quant stage          (was rag-processor)
unified/     step registry, `./rag` CLI, background step runs, the continuous scheduler
agent/       the one agent: tool guard, shell-free runner, OS sandbox, session runner
prompts/ .cline/ skills/ soul.md   what the agent reads
```

The two halves keep their own Python environments (the collector is light; the processor needs
docling/torch/bge-m3) and talk only through files: the collector writes immutable batches to
`collector/data/export/`, the processor reads them. See [ARCHITECTURE.md](ARCHITECTURE.md); what was checked
and what is still open: [AUDIT.md](AUDIT.md).

## Install

```bash
(cd collector && ./install.sh)                 # collector venv, SearXNG, Cline (see collector/README.md)
(cd processor && uv sync)                      # processor venv (see processor/README.md)
./rag doctor                                   # checks both environments and the services
```

Services the agent needs: LM Studio on `localhost:1234` with a model loaded, SearXNG on `localhost:8082`.

Tested hardware: runs end to end on Apple Silicon, an **M4 Pro with 48 GB** and an **M5 Max with 128 GB**
unified memory. On 48 GB, keep the processor at its default `MAX_WORKERS=4` and unload the agent's LLM in
LM Studio before a large ingest (the embedder alone uses about 13–17 GB, each docling worker 1–12 GB on big PDFs).

## Run

Everything goes through `./rag`. Every step prints one JSON result and exits 0 (ok/noop/partial),
1 (failed), 2 (blocked) or 75 (retry later).

```bash
./rag status                         # both halves: records, docket, outbox, ledger, LanceDB, quant, runs
./rag steps                          # every step, which repo it runs in, who may run it
./rag step collector.status          # one step in the foreground
./rag step processor.ingest --next   # ingest the oldest waiting batch
./rag start processor.quant-sync     # a step in the background; ./rag runs shows the result
./rag loop                           # continuous: collect → universe-sync → export ║ ingest → quant
./rag loop --mode deterministic --max-queries 10   # same, without the LLM (follows the query plan)
./rag agent                          # one agent session on its own (what `./rag loop` runs each cycle)
```

Stop the loop with Ctrl-C, SIGTERM or `touch logs/STOP`; the running step finishes first.

### First run, step by step (checked 2026-11-25 on an M4 Pro 48 GB)

```bash
./rag doctor                                   # environments, LM Studio, SearXNG, sandbox: all must say ok
./rag collect --dry-run                        # scope_target, pending queries per phase, docket backlog; nothing runs
./rag loop --once                              # one agent cycle: collect → universe-sync → export → ingest → quant
./rag status                                   # records, chunks, ledger (out-of-scope files, memory notes)
```

`./rag loop --once` in agent mode loads the LLM in LM Studio (about 23 GB). On a 48 GB Mac, unload it before a
big ingest (`lms unload --all`); `./rag step processor.health` reports how many parse workers fit.

### Delisted, listed or both

What is collected is set by `scope.target` in `collector/config/query_plan.json` (default `delisted`). Every
phase of the query plan has a `scope` (`delisted` or `listed`); only the phases the target covers are searched.

| `scope.target` | searched | reaches the vector DB | delisting records (master CSV) |
|---|---|---|---|
| `delisted` | historical focus + the delisting phases | documents with a delisting signal; IPO offer documents rejected | yes |
| `listed` | the `listed` and `listed_added` phases | every fetched document except site pages and procedures, offer documents included | none from these documents |
| `both` | everything, delisted and listed queries alternating | both of the above | from delisting documents only |

```bash
# permanently: edit collector/config/query_plan.json → "scope": {"target": "both"}
RAG_SCOPE_TARGET=both ./rag loop --once                     # one run only, without editing the config
RAG_SCOPE_TARGET=listed ./rag collect --dry-run             # see what a listed run would search
./rag add_queries --scope listed --year-tag 2025 --query "site:bseindia.com \"Acme Ltd\" annual report"
./rag collect --phase listed --max-queries 8                # only the listed queries, now
```

The `listed` phase ships with starter queries (NSE/BSE annual reports, results, board-meeting outcomes,
shareholding patterns, offer documents, the list of traded securities); add company-specific ones with
`--scope listed`. Everything lands in one LanceDB table (`documents`): filter by `doc_type`, `symbol` / `isin`
and `metadata_json.collector_relevance` (`corpus_listed:*` / `corpus_both:*` for listed-scope documents), and
join company status from the universe DB (`nse_entities.listing_status`). Each batch's manifest records its
`scope_target`; the processor skips IPO offer documents only in `delisted` batches.

## Check

```bash
scripts/check.sh --offline           # both test suites, the data audit, guard + prompt tests, a smoke run
```

## License

GNU Affero General Public License v3.0; see [LICENSE](LICENSE).
