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
- Deleted per-repo audit/planning docs (in history): `collector/instructions.md`,
  `collector/docs/UNIFIED_PIPELINE_CHANGES.md`, `processor/HANDOFF.md`, `processor/NEXT_STEPS_PLAN.md`,
  `processor/refactoring_flags.md`.

## 5. Open items

1. **Z-factors need price data.** quant-sync / quant-zfactor return `noop` (`no_price_rows`) until price or
   fundamentals files with `market_cap_cr` are ingested; the collector does not gather prices.
2. **First real fill of the vector DB.** `collector/data/export/` and `processor/data/lancedb` in this checkout are
   empty; the first `./rag loop` exports the 327 relevant documents (≈370 MB of clones) and ingests them (expect
   tens of minutes on MPS).
3. **A full agent-mode `./rag loop` cycle** (real collection session, then export and ingest) has not been run; the
   pieces were run separately (section 3).
4. **The old repos are frozen, not retired.** `nse-delisted-comp` is now `rag-collector` (local folder and
   GitHub, renamed 2026-11-24; its QA/remediation worktrees were removed, their branches kept). It and
   `rag-processor` still have their `unified-pipeline` branches, unpushed. Their committed `data/metadata` in
   `rag-collector@2e00307` is stale (416 records); here it is regenerated (409, matching the audit).
5. The `.rego` OPA policies under `processor/tests/` belong to the LLM stage; they are kept and run by CI unchanged.
6. CI on Linux skips the macOS sandbox tests; the sandbox is only verified locally.
7. Every export batch carries a full universe snapshot, even when the universe did not change (small, but
   repeated); the processor installs only the newest. Shipping it only when the fingerprint changes would save space.
8. Deferred from the Neon review: the process worker can't see that its fetch worker sibling has exited (a zombie)
   while discovery is still running, so the last parse batch waits for discovery (M-5). `agent_exec.py` does not tear down its
   child when Cline kills it after 30 s (L-2; agent-access steps finish well within that).
