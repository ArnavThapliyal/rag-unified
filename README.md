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

## Check

```bash
scripts/check.sh --offline           # both test suites, the data audit, guard + prompt tests, a smoke run
```
