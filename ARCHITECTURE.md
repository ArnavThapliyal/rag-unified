# Architecture

## Data flow

```
                 ┌──────────────────────── collector/ ────────────────────────┐
 web (SearXNG) → │ discover → fetch → process (parse + merge + audit)          │
                 │      master CSVs, docket, universe-sync → nse_unified.sqlite3│
                 │      export → data/export/<YYYYMMDD-HHMMSS>/  (immutable)    │
                 └───────────────────────────┬────────────────────────────────┘
                                             │ READY batches (files + sidecars + universe snapshot)
                 ┌───────────────────────────▼──────── processor/ ────────────┐
                 │ ingest: verify → docling/tabular → chunk → bge-m3 → LanceDB │
                 │ quant-sync → quant-zfactor → quant_factors table            │
                 └────────────────────────────────────────────────────────────┘
```

The halves never import each other. They share two contracts, both versioned (`contract_version: 1`).

### Step contract
Every stage is a step: `python -m rag_collector.steps <name>` (collector) or `python -m pipeline.steps <name>`
(processor), wrapped by `./rag step <repo>.<name>`. A step prints exactly one JSON object on stdout:
`{step, status, reason, counts, next_suggested, contract_version, started_at, finished_at, duration_s}`.
`status` is one of ok, noop, partial (exit 0), failed (1), blocked (2, a precondition such as low disk or
no network), retry_later (75, e.g. search engines suspended or another run holds the step's lock).
Steps are idempotent and hold a per-step lock, so running one twice, or from the loop and the agent at
once, is safe. `pipeline/stepkit.py` and `rag_collector/stepkit.py` are the same file.

### Outbox contract (collector → processor)
`collector/data/export/<batch_id>/`: `files/<raw subdir>/<doc>` plus `<doc>.meta.json` sidecars (source_url,
doc_type, vendor, symbol, isin, published_at, collector record ids), `universe.sqlite3` (made with SQLite's
backup API), `manifest.json` (every file's sha256) and `READY` (written last, holds the manifest's sha256).
A batch is built in a hidden `.tmp` directory and renamed into place; files are APFS copy-on-write clones made
read-only, never hard links, so a later re-fetch cannot change an exported batch. The processor verifies every
hash before ingesting, never writes into the outbox, and records each attempt in its own ledger. A batch with
failed files is retried (up to 3 attempts); indexed documents are skipped by doc_id.

## unified/
- `steps.toml`: the registry. Each step: repo, command, a one-line description, `access` (who may run it) and,
  for the agent, the arguments it may pass with their ranges.
  - `agent`: fast and read-only, the agent runs it directly (`./rag step`), e.g. status, list, quant-bracket.
  - `agent-background`: takes minutes; the agent starts it with `./rag start` (commands are killed after 30 s)
    and reads the result later with `./rag runs`, e.g. processor.ingest, quant-sync, universe-sync, export.
  - `user`: only from your terminal, e.g. processor.ingest-dir (any path) and the collector's collect stages,
    which the agent runs through the capped `./rag job start` instead.
- `./rag` runs each step with its own repo's interpreter and working directory and passes the JSON through.
- `./rag start` / `./rag runs`: background step runs, one per step at a time, results in `logs/runs/`.
- `./rag loop`: the scheduler. The processor's `--watch` runs the whole time; collector cycles (agent session or
  deterministic collection, then universe-sync, then export) run one after another. If more than
  `--max-pending` batches are waiting for the processor, collection pauses until it catches up (backpressure).
  Every decision comes from step results, never from model text. Log: `logs/loop.jsonl`.

## agent/ (one agent for the whole pipeline)
- Cline with a local model (LM Studio), started by `agent/run_agent.sh` with the working directory at the repo
  root, reading `prompts/agent.md`, `.cline/rules/`, `skills/` and `soul.md`.
- **Tool guard** (`agent/tool_guard.py`, run by `.cline/hooks/PreToolUse.sh` before every tool call): an
  allowlist grammar. The agent's only program is `./rag`: its research tools (`./rag web_search`, `./rag job`,
  `./rag add_queries`, …, validated by the collector's own grammar, which lives next to those scripts) and the
  unified commands (`./rag status|steps|step|start|runs`, validated against `steps.toml`). No shell: an allowed
  call is rewritten into `agent/agent_exec.py <plan>`, which re-checks it and runs argv lists. No file edits.
  Code, tests, raw data, the outbox and LanceDB are unreadable.
- **OS sandbox** (`agent/sandbox.py`, macOS sandbox-exec): the agent and everything it starts can read and
  write only the repo, Cline's state and temp; the runtimes (both Pythons, the embedding model cache) are
  read-only; keys, `.env`, `~/.ssh` and the keychain are denied.
- **Supervisor**: after a session, restart / cooldown / stop is decided from Cline's exit status and the
  collector's run summaries only.

## Who may run what

| | you | `./rag loop` | agent |
|---|---|---|---|
| status, steps, list, audit, quant-bracket | yes | | directly |
| collector collect stages (discover/fetch/process/drain) | yes | yes (deterministic mode) | via capped `./rag job start` |
| collector universe-sync, export; processor ingest, quant-sync, quant-zfactor | yes | yes | in the background |
| processor ingest-dir (any directory) | yes | | no |
