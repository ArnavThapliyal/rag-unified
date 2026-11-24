# Audit: the combined pipeline (rag-unified)

Date: 2026-11-24. Branch `feat/monorepo`. It replaces the per-repo audit documents: the collector's QA and
remediation reports and `instructions.md`, and the processor's `HANDOFF.md`, `NEXT_STEPS_PLAN.md` and
`refactoring_flags.md`. They are all still in git history. This file covers the pipeline as one system:
what each part does, who may run it, what was checked and how, and what is still open.

## 1. What runs where

| Step | Repo / command | What it does | Who runs it |
|---|---|---|---|
| discover / fetch / process / drain / collect | collector `rag_collector.steps` | search (SearXNG first), download at each site's pace, parse/OCR, merge into the master CSVs | you, `./rag loop` (deterministic mode); the agent only via the capped `./rag job start` |
| universe-sync | collector | rebuilds `data/nse_unified.sqlite3` (point-in-time universe); noop when its input CSVs are unchanged | `./rag loop` every cycle; the agent in the background |
| export | collector | new relevant documents → one immutable outbox batch (clones, sidecars, universe snapshot, manifest, READY) | `./rag loop` every cycle; the agent in the background (≤500 files) |
| status / list / audit | collector | dashboard JSON / step list / data-quality audit (read-only) | anyone; the agent directly |
| ingest | processor `pipeline.steps` | verify a READY batch (hashes, contract), docling/tabular → chunks → bge-m3 → LanceDB; ledger entry | `./rag loop` (the watcher); the agent in the background (`--next` / `--all` only) |
| ingest-dir | processor | ingest any folder (purchased files) | you only |
| quant-sync / quant-zfactor | processor | universe snapshot + price/fundamentals rows → quant DB → Z-factors (`quant_factors` table) | the watcher after an ingest; the agent in the background |
| status / list / health / quant-bracket | processor | dashboard / step list / environment check / read-only bracket queries | anyone; the agent directly |

Every step prints one JSON object and exits 0 (ok/noop/partial), 1 (failed), 2 (blocked) or 75 (retry later). It
holds a per-step lock, so the loop and the agent can't run the same step twice at once. The two `stepkit.py`
files are byte-identical; `scripts/check.sh` and a processor test both check this.

## 2. The agent boundary (one agent for both halves)

| Layer | Where | Checked by |
|---|---|---|
| Allowlist grammar: the only program is `./rag`; collector tools with their exact flags; unified commands against `unified/steps.toml` (access + argument ranges); stdin-only pipe filters; nothing bash would expand | `agent/tool_guard.py` (Cline PreToolUse hook) | `tests/agent/test_tool_guard.py`, `tests/agent/test_security_regressions.py` (incl. crafted tokens for user-only steps, `--batch` paths, `loop`/`agent`/`_bg`) |
| No shell: allowed calls are rewritten to `agent/agent_exec.py <plan>`, which re-validates and runs argv lists (collector tools in `collector/`, unified commands in the root) | `agent/agent_exec.py` | same tests |
| No reads of code, raw data, the outbox, LanceDB, docket DB, session logs, `.env*` | `tool_guard.NO_READ` + `.clineignore` | `test_reads_and_edits`, `test_secrets_and_search_settings_are_off_limits` |
| OS sandbox: the repo and Cline state only; Python installs and the Hugging Face cache read-only; keys, `.env` (top, `collector/`, `processor/`), `~/.ssh`, keychain denied | `agent/sandbox.py` (sandbox-exec) | `tests/agent/test_agent_sandbox.py` (throwaway HOME, canaries), `./rag doctor`, run before every session |
| Prompts only advertise commands the guard accepts; step names in the prompts exist with the access they claim | `prompts/agent.md`, `.cline/rules/01-rag-agent.md`, `soul.md`, `skills/` | `tests/agent/test_agent_prompts.py` |
| Restart / cooldown / stop decided from Cline's exit status, its run_result event and the collector's run summaries, never from model text | `agent/run_agent.sh` + `collector/scripts/agent_supervisor.py` | `collector/tests/test_agent_supervisor.py` (decisions), `tests/agent/test_run_agent.py` (the real runner with a fake Cline, incl. sandboxed home probe) |

## 3. Verification (2026-11-24, this machine)

| Check | Result |
|---|---|
| `scripts/check.sh --offline` | all ok: collector 347 tests, dataset audit 0 errors (409 records); processor 120 passed, 3 skipped, ruff clean; agent + unified 256 tests; sandbox probe ok; no stale references |
| `scripts/check.sh --offline --e2e` (temporary outbox + LanceDB, real models) | export of 3 real documents → ingest ok, 3 files, 14 chunks → second ingest noop → quant-sync ok |
| `./rag doctor` | both environments, Cline, LM Studio, SearXNG, processor health, sandbox: ok |
| Real agent session (Cline + qwen3.8-27b, sandbox on), a short boundary smoke prompt | `./rag job status`, `./rag step processor.quant-bracket`, `./rag start collector.list` (background run finished ok), `./rag runs`, `./rag log` ran; `cat soul.md` and `head -c` were blocked and filed as issues; session ended `completed` |
| Processor ingest inside the agent sandbox (real bge-m3) | ok: 2 files, 9 chunks (the model cache is readable, nothing in home writable) |
| `./rag loop --once --mode deterministic --max-queries 1` (temporary outbox + LanceDB) | collector cycle: 1 query (`partial max_queries_reached`), universe-sync noop, export 20 batches × 2 documents (the per-cycle export cap with `--export-batch 2`), decision line read; then the processor ingested all 20 batches: 39 of 40 files indexed, 1,248 chunks, 0 failed, every ledger entry ok; quant-sync ok; exit 0 |
| After the Neon fixes: `./rag loop --once … --export-batch 2` (temporary outbox + DATA_DIR) | export capped at 3 batches by `--max-pending` (was 20), all 3 ingested, quant-sync ok, exit 0; two concurrent `processor.status` calls both ok |
| After the ultrareview fixes (`audit/neon`): `scripts/check.sh --offline` | all ok: collector 354 tests, processor 125 passed / 3 skipped, agent + unified 279, dataset audit 0 errors, sandbox probe ok |
| Real-data step runs, isolated outboxes (`audit_runs/`, not tracked; bge-m3 on CPU) | export 5 real PDFs → ingest 5 files / 142 chunks → second ingest noop → quant-sync (3,654 entities, 457 events) → zfactor / bracket noop (no prices); 3 batches through `main.py --watch --once`, exit 0; the same batch re-ingested with `--workers 2` writes 0 chunks (dedup by doc_id) |
| Full agent-mode E2E (2026-11-24): `./rag loop --once`, Cline + qwen3.8-27b, sandbox on | agent session completed (8 queries, 38 files, 0 new records, issues #8–#10 filed); export 332 documents; ingest ok: 291 indexed + 41 already stored, 0 failed, 13,874 chunks, 45.8 min, bge-m3 on MPS; quant-sync ok, zfactor noop (no prices), second ingest noop. Bugs found and fixed: `add_queries` race corrupted `query_plan.json` (flock + atomic write); `status_report` traceback on a closed pipe; 12 docling workers + a loaded LLM swapped the 48 GB Mac to 31 GB (hard `MAX_WORKERS` cap) |
| Benchmark after the changes (same batch, scratch DATA_DIR) | 22.8 min (was 45.8): triage skipped 3 IPO offer documents (1,441 pages); peak worker 5.9 GB (was 13.8); 284 files in both runs give identical chunks except the 140-page scan (31 → 248 chunks, its 125 scanned pages now OCRed). `audit_runs/perf/baseline_20261124.*`, `new_20261125.*` |
| After the changes: `scripts/check.sh --offline --e2e`, `RUN_SLOW=1` parser tests | all ok: collector 357, processor 139 + 2 model-backed, agent + unified 281, ruff clean; e2e export → ingest → noop → quant-sync ok |

## 4. What changed when the repos were combined

- Both repos came in with `git subtree add` (full history): `collector/` from `nse-delisted-comp@unified-pipeline`,
  `processor/` from `rag-processor@unified-pipeline`.
- New: `unified/` (registry, `./rag`, background runs, the scheduler), `agent/`, top-level prompts, `tests/`,
  `scripts/check.sh`, `.github/workflows/ci.yml` (replaces the processor's workflow; the OPA workflow moved up).
- Moved from the collector: the tool guard, shell-free runner, sandbox and session runner (→ `agent/`), the prompt,
  rules, soul, skills and `.clineignore` (→ top level, rewritten for `./rag` and the processing side), their tests
  (→ `tests/agent/`). Removed: `collector/scripts/steps.py` (it existed only for the old guard grammar).
- Collector: `rag_collector.loop` gained `--agent-cmd` (default `../agent/run_agent.sh`) and prints its decision as
  JSON with `--once`; the installer and setup texts point at rag-unified.
- Processor: `COLLECTOR_OUTBOX` defaults to `../collector/data/export`; the 10 ruff errors in `check_pipeline.py`
  (which failed CI lint since `a8419e2`) are fixed (unused imports, an unused variable, `l` renamed).
- Gemini Neon review (`NEON_AUDIT.md`, branch `audit/neon`): 21 findings checked one by one; the confirmed ones
  are fixed with a regression test each (see its last section). The main ones: backpressure read the wrong key and
  never fired; indexer children that ignore SIGTERM are now killed; MPS out-of-memory retries now use smaller shapes;
  agent mode no longer acts on an earlier cycle's run summary; `job.py`'s `next` command passes the guard; a job
  and a collect step can't run at once; export bursts are capped by `--max-pending`; the sandbox blocks writes to
  `.env` and `.git/`; read-only steps take no lock.
- `/code-review ultra` round (commit "Fix ultrareview findings"): `touch logs/STOP` now stops a running collector
  cycle; the watcher runs in its own session so Ctrl-C lets its batch finish, and its restart count resets after an
  hour of healthy uptime; background runs hold `logs/bg/<run_id>.lock` (a free lock means lost, whoever has the pid
  now); agent sessions run Hugging Face offline and `./rag doctor` checks the embedding model is cached;
  collect.py never SIGTERMs workers that are saving after an interrupt; `./rag` refuses Python < 3.11 with a setup
  hint. Regression tests for each.
- Deleted per-repo audit/planning docs (in history): `collector/instructions.md`,
  `collector/docs/UNIFIED_PIPELINE_CHANGES.md`, `processor/HANDOFF.md`, `processor/NEXT_STEPS_PLAN.md`,
  `processor/refactoring_flags.md`.

## 5. Open items

1. **The production vector DB is built elsewhere** (M5 Max, see [HANDOFF.md](HANDOFF.md)); this checkout's
   LanceDB holds the E2E batch minus the purged documents (6,065 rows).
2. **Not built from the E2E plan:** a memory admission check per file, worker recycling after N files, more
   parser threads for big files, and a separate OCR backfill step. Windowed parsing bounds worker memory and
   page-range OCR made a 150-page budget affordable, so they were not needed for this corpus. `scope.target`
   listed / both only widens what reaches the corpus; there are no listed-company query templates yet.
3. **OCR: TEMPORARY PASS (2026-11-25), to revisit.** Accepted on "it works" evidence only, for time: the
   model-backed OCR test (`RUN_SLOW=1 tests/test_parser_pdf.py`) passes and the 140-page scan was fully OCRed
   (125 pages, 31 → 248 chunks). Not checked: OCR text quality (no ground truth compared), Hindi/Marathi
   (RapidOCR has English + Chinese models only; those newspaper copies are denylisted instead), speed (~5.5 s
   per page on CPU, single thread; ocrmac / Apple Vision not benchmarked), and the 18 pages of
   `f9a1855a64859d43.pdf` OCR returned nothing for.
4. **Z-factors: wired, not exercised on real data.** Target bracket 3,500–12,500 cr (default; calibrated z
   0.535–1.054, NSE prior). quant-sync / quant-zfactor / quant-bracket run and return `noop` (`no_price_rows`)
   because no price or `market_cap_cr` rows exist yet; the unit tests (`test_z_factor_brackets.py`,
   `test_quant_store.py`) cover the math.
5. **The old repos are frozen, not retired.** `nse-delisted-comp` is now `rag-collector` (local folder and
   GitHub, renamed 2026-11-24; its QA/remediation worktrees were removed, their branches kept). It and
   `rag-processor` still have their `unified-pipeline` branches, unpushed. Their committed `data/metadata` in
   `rag-collector@2e00307` is stale (416 records); here it is regenerated (409, matching the audit).
6. The `.rego` OPA policies under `processor/tests/` belong to the LLM stage; they are kept and run by CI unchanged.
7. CI on Linux skips the macOS sandbox tests; the sandbox is only verified locally.
8. Every export batch carries a full universe snapshot, even when the universe did not change (small, but
   repeated); the processor installs only the newest. Shipping it only when the fingerprint changes would save space.
9. Deferred from the Neon review: the process worker can't see that its fetch worker sibling has exited (a zombie)
   while discovery is still running, so the last parse batch waits for discovery (M-5). `agent_exec.py` does not tear down its
   child when Cline kills it after 30 s (L-2; agent-access steps finish well within that).
