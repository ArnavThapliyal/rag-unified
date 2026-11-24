# rag-unified Deep Audit Report (`NEON_AUDIT.md`)

- **Date:** 2026-11-24
- **Branch:** `audit/neon` (branched from `feat/monorepo` @ `18ec635`)
- **Scope:** Full static review of tracked files across `unified/`, `agent/`, `collector/`, `processor/`, `prompts/`, `.cline/`, `skills/`, `scripts/`, and `tests/`, plus offline verification checks (`scripts/check.sh --offline` and targeted read-only reproductions).
- **Focus Areas:**
  1. Parallel execution bugs, race conditions, lock-file leaks, and backpressure/overflow issues
  2. Memory leaks and orphaned child processes (especially MPS/embedding workers and `SIGINT`/`SIGTERM` handling)
  3. Automation loop bugs and misconfigurations between pipeline elements (`collector → outbox → processor → unified`)
  4. Agent prompt mismatches, sandbox/tool-guard bypasses, and false-flag/false-block rules

---

## Executive Summary

| Severity | Count | Key Themes |
|---|---:|---|
| **Critical** | 2 | Dead backpressure in `./rag loop` (key mismatch); orphaned MPS embedder & parse workers under `--watch` (no `SIGKILL` escalation + `join_thread()` hang) |
| **High** | 6 | MPS OOM retry pads halved batches back into failing bucket; stale run summary hijacks agent-mode loop decisions; `job.py` `next` command blocked by `tool_guard.py`; missing cross-lock between `job.py` and `rag_collector.steps`; `SIGTERM` leaves orphaned Cline/worker children; 20-batch export burst bypasses backpressure |
| **Medium** | 7 | `unified/bg.py` TOCTOU & orphan risk; duplicate `sha256`/path crash in `export.py`; `unified/loop.py` hardcoded `PROCESSOR_STOP`, unmonitored watcher during cycles, and masked `--once` processor failures; `indexer.py` consumed-sentinel replacement deadlock; `process_worker.py` sibling zombie blindness; `sandbox.py` writable `.env`/`.git/config`; read-only step lock contention |
| **Low** | 4 | Agent prompt self-contradictions; `agent_exec.py` child orphan on 30s kill; `parser.py` ignoring instance `compute_budget`; minor crash-window / edge-case bugs |

### Verification Baseline Notes
- `scripts/check.sh --offline` was run inside the restricted execution environment:
  - **Passed:** `pyflakes` (collector & unified/agent), `ruff` (processor), `bash -n`, `stepkit.py` byte-identity, `no stale references`, `dataset audit` (409 records, 0 errors), `./rag status`, `collector.export --dry-run`, `processor.health`.
  - **Environment-specific sandbox failures (not code regressions):** `test_probe_reports_up_when_a_host_accepts` (loopback connect blocked in sandbox), `test_agent_sandbox.py` / `sandbox.py --check` (nested `sandbox-exec` / `~/.ssh` stat blocked inside sandbox), and processor `pytest` (Metal/MPS init inside sandbox).
- Targeted offline Python reproductions **confirmed** Findings **C-1**, **H-1**, and **H-3** directly against the live code.

---

## Critical Findings

### [C-1] Backpressure in `./rag loop` Is Completely Non-Functional (Schema Key Mismatch)
- **Category:** Automation loop / Backpressure & overflow
- **Files & Lines:**
  - `unified/loop.py:112-114` (`Loop.pending`)
  - `unified/loop.py:159-164` (`Loop.main`)
  - `processor/pipeline/steps.py:130-139` (`status_step`)
  - `tests/test_unified.py:181` (test gap)
- **Details:**
  `Loop.pending()` looks for `pending_count` inside `res["counts"]`:
  ```python
  def pending(self) -> int | None:
      _, res = registry.run("processor.status", [], stderr=subprocess.DEVNULL, timeout=120)
      return (res.get("counts") or {}).get("pending_count")
  ```
  However, `processor/pipeline/steps.py:130-139` places `pending_count` under `res["outbox"]["pending_count"]` and leaves `res["counts"]` empty (`{}`). Because `unified/loop.py:160` checks `if waiting is not None and waiting > self.a.max_pending:`, and `waiting` is **always `None`**, backpressure never triggers. Collection will continue exporting batches unboundedly regardless of `--max-pending`.
  `tests/test_unified.py:181` missed this because it monkeypatches `lp.pending` instead of parsing a real `processor.status` payload.
- **Verification:** Confirmed offline: calling `Loop.pending()` against real `processor.status` returned `None` while `res["outbox"]["pending_count"]` was `0`.
- **Recommendation:** Read `(res.get("outbox") or {}).get("pending_count")` (with fallback to `counts`), treat `waiting is None` as a warning/pause rather than silently bypassing backpressure, and add a unit test exercising `Loop.pending()` against real `status_step()` output.

---

### [C-2] Orphaned MPS Embedder & Parse Workers + Queue Shutdown Deadlocks in `processor/pipeline/indexer.py`
- **Category:** Memory leaks / Orphaned child processes / `SIGINT` & `SIGTERM` handling
- **Files & Lines:**
  - `processor/pipeline/indexer.py:233-244` (`_start_orphan_watchdog`)
  - `processor/pipeline/indexer.py:297-302` (`_worker_main` `finally:`)
  - `processor/pipeline/indexer.py:305-361` (`_embedder_main`)
  - `processor/pipeline/indexer.py:520-522` (stalled worker termination)
  - `processor/pipeline/indexer.py:568-575` (`_run_parallel` `finally:`)
- **Details:**
  1. **No `SIGKILL` escalation in `finally:` (`lines 568-575`):** On abnormal exit (`KeyboardInterrupt`, `SIGTERM`, or exception), main sends `proc.terminate()` and calls `proc.join(timeout=10)`, then closes queues **without calling `proc.kill()`** on processes still alive after 10s.
  2. **Orphan watchdog does not fire under `main.py --watch` (`lines 233-244`):** `_start_orphan_watchdog` only calls `os._exit(1)` when `mp.parent_process().is_alive()` becomes `False`. Because `processor/main.py --watch` (`pipeline/watch.py`) is a long-lived daemon that stays alive across batches, any worker or MPS `embedder` process that ignores or exceeds 10s on `SIGTERM` (e.g., stuck in a native docling/RapidOCR/Metal call) becomes a **permanent orphan** holding multi-GB MPS GPU memory while `--watch` proceeds to spawn another embedder for the next batch.
  3. **Stalled worker never escalated to `SIGKILL` (`lines 520-522`):** When a worker exceeds `WORKER_STALL_TIMEOUT_SECONDS`, `killed[worker_id]` is recorded and `proc.terminate()` is sent once. If the worker is wedged in native code and ignores `SIGTERM`, `worker_id not in killed` is `False` on subsequent loop iterations, so `proc.kill()` is never called and the entire batch hangs indefinitely waiting for `workers` to empty.
  4. **Queue feeder-thread hang on `KeyboardInterrupt` (`lines 297-302`, `305-361`):** `_worker_main`'s `finally:` calls `chunk_q.join_thread()` unconditionally — even when exiting via `KeyboardInterrupt` (broadcast to the process group on `Ctrl-C`). If `embedder` also received `SIGINT` and exited while `chunk_q` is full, workers block indefinitely in `join_thread()`. Likewise, `_embedder_main` does not call `out_q.cancel_join_thread()` on `BaseException`, causing its `atexit` queue join to hang if `out_q` is full.
- **Recommendation:**
  - In `indexer.py:568-575`, escalate any process still `is_alive()` after `join(timeout=10)` to `proc.kill()` followed by `proc.join(timeout=5)`.
  - In `indexer.py:520-522`, record a `killed_at` timestamp and escalate stalled workers to `proc.kill()` after a short grace period (e.g. 15s).
  - In `_worker_main`, only call `chunk_q.join_thread()` on clean exit (`error is None`); on exception/interrupt, call `chunk_q.cancel_join_thread()`. Do the same for `out_q` in `_embedder_main`.

---

## High Findings

### [H-1] MPS OOM Batch-Halving Retries the Exact Same Padded Row Bucket
- **Category:** Memory leaks / MPS OOM recovery
- **Files & Lines:**
  - `processor/pipeline/embedder.py:54-59` (`_ROW_BUCKETS`, `_row_bucket`)
  - `processor/pipeline/embedder.py:112-133` (`Embedder.encode`)
  - `processor/pipeline/embedder.py:164-165` (`Embedder._forward_fixed`)
- **Details:**
  On MPS (`_fixed_shapes=True`), `encode()` reduces a local `batch_size` by half on OOM (`32 → 16 → 8 → 4 → 2 → 1`) and only updates `self.batch_size` at line 133 after the entire `encode()` loop succeeds.
  However, `_forward_fixed()` computes the padded row count using `self.batch_size` (still `32`):
  ```python
  rows = _row_bucket(len(batch), max(self.batch_size, len(batch)))
  ```
  With `_ROW_BUCKETS = (4, 16)`:
  - Halving `16 → 8` calls `_row_bucket(8, 32)` → **`16`** (re-runs the exact 16-row shape that just OOM'd).
  - Halving `4 → 2` and `2 → 1` calls `_row_bucket(2, 32)` and `_row_bucket(1, 32)` → **`4`** (re-runs the 4-row shape that just OOM'd).
  This prevents smaller batches (`8`, `2`, `1`) from ever running on MPS and forces premature fallback to CPU.
- **Verification:** Confirmed offline: `_row_bucket(8, 32) == 16`, `_row_bucket(2, 32) == 4`, `_row_bucket(1, 32) == 4`.
- **Recommendation:** Pass the active retry `batch_size` into `_encode_batch` / `_forward_fixed` (or update `self.batch_size` immediately on OOM), and ensure `_row_bucket` never pads above the reduced `batch_size` (e.g. include `1` or clamp to `batch_size`).

---

### [H-2] Stale Run Summary Hijacks Agent-Mode Loop Decisions
- **Category:** Automation loop / Pipeline state mismatch
- **Files & Lines:**
  - `collector/src/rag_collector/loop.py:103-111` (`Loop.segment`)
  - `collector/src/rag_collector/loop.py:123-130` (`Loop.last_run_summary`)
- **Details:**
  In `--mode agent`, after `agent/run_agent.sh` exits, `Loop.segment()` calls `self.last_run_summary()`, which simply reads the newest `collector/logs/runs/*.json` file on disk **without checking whether it was produced during the current cycle/session** (contrast with `collector/scripts/agent_supervisor.py:64-74`, which filters by `d.get("agent_session") == session`).
  If an agent session completes without launching `collect.py` (e.g., it only inspects status/issues and exits `0`, or fails preflight), `loop.py` reads the previous cycle's summary:
  - A prior `low_disk_space` or `network_unavailable` run summary will permanently classify future sessions as `blocked` and stop/stall the loop even after the issue is fixed.
  - A prior `search_engines_suspended` summary will trigger repeated cooldowns.
  - A prior productive run's `new_records` will falsely classify a no-op session as `ok`.
- **Recommendation:** Record the newest run summary filename (or `time.time()` / `started_at`) before launching `run_agent.sh` and only consider run summaries created after the segment started.

---

### [H-3] `job.py` Emits a Pre-Monorepo `next` Command That `tool_guard.py` Blocks
- **Category:** Agent prompt / Tool-guard mismatch (false-block)
- **Files & Lines:**
  - `collector/scripts/job.py:118, 128`
  - `prompts/agent.md:13`
  - `.cline/rules/01-rag-agent.md:55`
  - `agent/tool_guard.py:440-441`
  - `collector/scripts/status_report.py:83, 91` (related stale hints)
- **Details:**
  `collector/scripts/job.py:118` and `128` return:
  ```python
  "next": ".venv/bin/python scripts/job.py wait 1"
  "next": f".venv/bin/python scripts/job.py wait {poll + 1}"
  ```
  Both `prompts/agent.md:13` and `.cline/rules/01-rag-agent.md:55` instruct the agent to *"run the exact `next` command it prints"*. However, `agent/tool_guard.py:440-441` explicitly refuses `.venv/bin/python scripts/...` and logs a `tool_denied` issue in `collector/logs/issues.jsonl`. Similarly, `status_report.py:83, 91` still prints `scripts/docket.py --stats` and `scripts/web_search.py --engines`.
- **Verification:** Confirmed offline: `tool_guard.check("run_commands", {"commands": [".venv/bin/python scripts/job.py wait 1"]})` returns `"run everything through ./rag (e.g. ./rag status, ./rag job status), never python or scripts directly"`.
- **Recommendation:** Update `job.py:118, 128` to emit `./rag job wait 1` / `./rag job wait {poll + 1}` (or parameterize via env/flag for standalone collector use) and update `status_report.py:83, 91`.

---

### [H-4] Missing Cross-Lock and TOCTOU Race Between `./rag job start` and `./rag step collector.<stage>`
- **Category:** Parallel execution / Lock-file & state corruption
- **Files & Lines:**
  - `collector/scripts/job.py:101-116` (`start`)
  - `collector/src/rag_collector/steps.py:192-195` (`collect` lock)
  - `collector/scripts/collect.py:323` (`mark_stale_runs`)
- **Details:**
  - `rag_collector/steps.py:193` acquires `locks/collect.lock` before running `collect.py`, but `collector/scripts/job.py:101-116` launches `collect.py` directly **without acquiring `locks/collect.lock`**.
  - `job.py:start()` also has no lock around `load_job()` → `alive()` → `Popen` → `atomic_write_text(JOB_FILE)` (TOCTOU race between concurrent `job start` calls).
  - If an agent runs `./rag job start` concurrently with `./rag step collector.collect` (or `./rag loop`), two `collect.py` processes run simultaneously. The second instance's `mark_stale_runs(rid)` (`collect.py:122-133, 323`) immediately marks the first live instance's run summary as `"status": "killed"`, and both instances race on `collection_state.json`, `processed/nse_delisted_master.csv`, and `docket.sqlite`.
- **Recommendation:** Acquire `locks/collect.lock` inside `collect.py` itself (or have `job.py` launch `rag_collector.steps` / hold `collect.lock`), and protect `job.py:start()` with an exclusive `flock`.

---

### [H-5] `SIGTERM` Does Not Cleanly Terminate `agent/run_agent.sh` or `collect.py` Workers (Orphaned Processes & 2–4 min Shutdown Stalls)
- **Category:** Orphaned child processes / `SIGINT` & `SIGTERM` handling
- **Files & Lines:**
  - `agent/run_agent.sh:94-96, 117-120`
  - `unified/loop.py:48-51`
  - `collector/src/rag_collector/loop.py:46-49`
  - `collector/scripts/collect.py:407-416`
- **Details:**
  1. **`agent/run_agent.sh:95`** only traps `INT` (`trap 'interrupted=1' INT`) — **not `TERM`**. Yet `unified/loop.py:51` and `rag_collector/loop.py:49` send `SIGTERM` to their child on shutdown. When `run_agent.sh` receives `SIGTERM`, bash exits immediately without forwarding `SIGTERM` to its running `sandbox-exec` / `cline` pipeline (or `cline-hub-daemon`), leaving orphaned Cline/model sessions running in the background.
  2. **`collector/scripts/collect.py:407-416`** assumes in its comment that `# Ctrl-C / job.py stop reach the workers too (same process group)` and calls `c.wait(timeout=120)` *before* `c.terminate()`. That assumption fails on `SIGTERM` (which `rag_collector/loop.py:49` sends to `collect.py`'s PID only, not the process group) and on `LowDisk` or unhandled exceptions: `fetch_worker` and `process_worker` keep running for up to 120s *each* (4+ min total) before `collect.py` sends `SIGTERM`. Furthermore, `process_worker.py:40` runs `parse_nse_records.py` (which spawns `tesseract`) via `subprocess.run` without process-group termination.
- **Recommendation:**
  - In `agent/run_agent.sh`, trap both `INT` and `TERM`, run the Cline pipeline in a tracked subshell/process group (or record PIDs), and forward `SIGTERM` to it on trap/exit.
  - In `collect.py:407-416`, if `step["status"] != "completed"` (i.e. interrupted, `LowDisk`, or error), immediately `c.terminate()` all live children *before* waiting (with a shorter grace timeout, then `c.kill()`).

---

### [H-6] Single Collector Cycle Exports Up to 20 Batches, Bypassing `--max-pending` Backpressure
- **Category:** Backpressure & overflow / Automation loop
- **Files & Lines:**
  - `collector/src/rag_collector/loop.py:145-151`
  - `unified/loop.py:159-165`
- **Details:**
  Even after Fixing `[C-1]`, `unified/loop.py` only checks `pending() > max_pending` (default `3`) *between* collector cycles. Inside a single cycle, `rag_collector/loop.py:145-151` runs:
  ```python
  for _ in range(20):  # export in batches while documents are waiting
      ...
      ex = self.step("export", "--max-files", str(self.a.export_batch))
  ```
  with no awareness of `--max-pending`. When a backlog exists (or `--export-batch` is small), one cycle dumps up to **20 READY batches** into the outbox at once, overflowing the processor queue by nearly 7× the configured limit.
- **Recommendation:** Add an `--export-max-batches` option to `rag_collector.loop` (defaulting to `1` or `max_pending` when invoked by `unified/loop.py`) so backpressure bounds both collection and export batch bursts.

---

## Medium Findings

### [M-1] TOCTOU Race & Orphaned Child Risk in `unified/bg.py`
- **Category:** Parallel execution / Race conditions / Orphaned processes
- **Files & Lines:**
  - `unified/bg.py:32-36, 82-104` (`start`, `_write`)
  - `unified/bg.py:110-123` (`execute`)
- **Details:**
  - `bg.start()` checks `running()` (per-step uniqueness and `MAX_RUNNING = 3`), chooses `run_id`, and writes `logs/bg/<run_id>.json` **without a file lock**. Two concurrent `./rag start` calls can both pass the check, exceed `MAX_RUNNING`, launch duplicate runs of the same step, or collide on the same `run_id` and deterministic `.json.tmp` temp file (`line 34`).
  - `bg.execute()` installs no `SIGTERM` handler to terminate the child `registry.run()` subprocess or mark the run failed; killing `_bg` can orphan the underlying step process while leaving the run `"running"` until detected as `"lost"`.
- **Recommendation:** Wrap `bg.start()`'s check + `run_id` allocation + initial `_write()` in an `flock` on `BG_DIR / ".start.lock"`, use PID-qualified temp files, and install a `SIGTERM` handler in `bg.execute()`.

---

### [M-2] Duplicate `sha256` or `local_path` in Docket Crashes `export.py`
- **Category:** Pipeline integration (`collector → outbox`)
- **Files & Lines:**
  - `collector/src/rag_collector/export.py:240-250, 269-284`
- **Details:**
  `export.py:247` checks `if sha in exported:` against previously exported batches, but never deduplicates `sha` or destination path `sub` within the current `pending` list. If two `parsed` docket rows reference the same `local_path` (or map to the same `sub`), the first iteration sets `dst.chmod(0o444)` and `side.chmod(0o444)` (`lines 279, 284`), and the second iteration crashes with `PermissionError` when writing `side` or copying `dst`. If two different files share identical `sha256`, both are exported in the same batch.
- **Recommendation:** Track `seen_sha` and `seen_sub` sets while building `pending` in `export.py:240-250` and skip in-batch duplicates.

---

### [M-3] `unified/loop.py` Misconfigurations: Hardcoded `PROCESSOR_STOP`, Unmonitored Watcher During Cycles, and Masked `--once` Failures
- **Category:** Automation loop / Pipeline misconfiguration
- **Files & Lines:**
  - `unified/loop.py:31, 96-110` (`PROCESSOR_STOP`)
  - `unified/loop.py:126-141` (`collector_cycle`)
  - `unified/loop.py:168-174` (`--once` mode) & `processor/pipeline/watch.py:82-96`
  - `unified/loop.py:188-191` (`finally:`)
- **Details:**
  1. **`PROCESSOR_STOP` ignores `DATA_DIR` / `STOP_FILE` env overrides (`line 31`):** Hardcoded to `ROOT / "processor" / "data" / "STOP"`, whereas `processor/config/settings.py:146` uses `Path(_env("STOP_FILE", str(DATA_DIR / "STOP")))`. When `DATA_DIR` is overridden (e.g. temp/E2E runs), `stop_watch()` touches the wrong file and blocks for the full `--stop-timeout` (600s) before sending `SIGTERM`.
  2. **Watcher health & `logs/STOP` unmonitored during `collector_cycle()` (`lines 126-141`):** `for line in self.child.stdout:` blocks for the entire collector cycle (potentially hours in agent mode) without calling `self.check_watch()` or checking `STOP.exists()`. A crashed watcher stays dead until the cycle ends.
  3. **`--once` silently masks processor failures (`loop.py:170-173`, `watch.py:96`):** `Watch.main()` always returns `0` even when `ingest` or `quant` fails, so `./rag loop --once` never sees a non-zero `ingest.returncode`. Also, `--once` runs `processor_cmd(True)` without checking `self.stop_requested()`.
  4. **`finally:` does not terminate/reap `self.child` (`lines 188-191`):** If an unexpected exception occurs while `self.child` is alive, `self.child` is not terminated or waited on.
- **Recommendation:** Resolve `PROCESSOR_STOP` from `STOP_FILE` / `DATA_DIR` env vars; monitor `self.watch` and `STOP` concurrently (e.g. `selectors` or a watcher thread); have `Watch.main()` return non-zero on failed steps in `--once` mode; terminate/reap `self.child` in `finally:`.

---

### [M-4] Potential Deadlock in `Indexer._run_parallel` If a Worker Dies After Consuming Its `None` Sentinel
- **Category:** Parallel execution / Deadlock
- **Files & Lines:**
  - `processor/pipeline/indexer.py:524-539`
- **Details:**
  When a worker exits without an observed `("exit", worker_id, ...)` control message, main assumes it crashed mid-work and calls `start_worker()`, commenting `# its stop marker is still queued, so the replacement will finish cleanly`. If the worker popped its `None` sentinel from `task_q` and died before `ctl_q.put(("exit", ...))` completed, no `None` remains in `task_q` for the replacement worker — causing the replacement worker to block forever on `_get(task_q)` and preventing `MSG_STOP` from ever being sent to `embedder`.
- **Recommendation:** Track remaining work or push a fresh `None` sentinel (or only spawn a replacement if `task_q` still has unstarted tasks / `path is not None`).

---

### [M-5] Sibling Zombie Blindness in `collector/scripts/process_worker.py`
- **Category:** Parallel execution / Process lifecycle
- **Files & Lines:**
  - `collector/scripts/process_worker.py:29-36, 66`
  - `collector/scripts/collect.py:382-396`
- **Details:**
  `process_worker.py` checks whether `fetch_worker` is still running via `os.kill(pid, 0)`. Because both are siblings spawned by `collect.py`, if `fetch_worker` exits early (e.g. `max_files_reached`, `network_unavailable`, or crash) while `collect.py` is still inside `discover()`, `fetch_worker` remains a **zombie** until `collect.py` reaches `c.wait()` at line 396. `os.kill(zombie_pid, 0)` returns `True`, so `process_worker` delays flushing the final `< batch` downloaded files until `discover()` finishes.
- **Recommendation:** Use a marker file (like `collect.py`'s `.discovering` marker) or have `collect.py` reap `fetch_worker` with `poll()` during `discover()`.

---

### [M-6] `agent/sandbox.py` Allows Writes to `.env` / `.git/config` and Interpolates `root` Unescaped Into Regex
- **Category:** OS sandbox hardening
- **Files & Lines:**
  - `agent/sandbox.py:83, 93`
- **Details:**
  Line 83 grants `(allow file-read* file-write* (subpath root))`. Line 93 adds:
  ```scheme
  (deny file-read* (regex #"^{root}/((collector|processor)/)?\.env") (literal "{root}/.git/config"))
  ```
  1. Only `file-read*` is denied — **`file-write*` remains allowed**, so a tool-guard bypass could overwrite `.env` or `.git/config` (e.g. injecting `core.fsmonitor` / `core.hooksPath` for code execution outside the sandbox on the user's next `git` command).
  2. `{root}` is interpolated raw into the SBPL regex without escaping regex metacharacters (`.`, `+`, `(`, `)`, `[`, `]`).
- **Recommendation:** Deny both `file-read* file-write*` on `.env*` (except `.env.example` read-only) and `.git/` (or at minimum `.git/config` and `.git/hooks`), and regex-escape `str(root)`.

---

### [M-7] Exclusive Locks on Read-Only Steps Cause Spurious `retry_later` Contention
- **Category:** Parallel execution / Lock contention
- **Files & Lines:**
  - `collector/src/rag_collector/steps.py:32, 191, 201`
  - `processor/pipeline/steps.py:210, 236, 238`
- **Details:**
  `collector/src/rag_collector/steps.py:32` documents `status` as `(no lock)`, yet line 191 runs it under `run_step("status", status_step, locks / "readonly-status")` (which takes an exclusive non-blocking `flock` on `status.lock`). `audit`, `processor.status`, `processor.quant-bracket`, and `processor.health` similarly acquire exclusive locks. Concurrent invocations (e.g., `./rag loop` polling `processor.status` while the agent runs `./rag status`) fail with `retry_later` (exit 75).
- **Recommendation:** Make `step_lock` optional (`lock_dir: Path | None`) in `stepkit.py` and skip locking for genuinely read-only steps (`status`, `audit`, `health`, `quant-bracket`).

---

## Low Findings

### [L-1] Agent Prompt Internal Contradictions
- **Category:** Agent prompts
- **Files & Lines:**
  - `prompts/agent.md:2` vs `prompts/agent.md:6-7`
  - `.cline/rules/01-rag-agent.md:3` vs `.cline/rules/01-rag-agent.md:91-92`
  - `prompts/agent.md:56` vs `prompts/agent.md:64`
  - `prompts/agent.md:47-48` vs `prompts/agent.md:108` & `.cline/rules/01-rag-agent.md:49`
- **Details:**
  - Both `prompts/agent.md:2` and `.cline/rules/01-rag-agent.md:3` tell the agent to *"Read `soul.md`"* (and `01-rag-agent.md`) first, while `prompts/agent.md:6-7` and `01-rag-agent.md:91-92` forbid reading `prompts/, .cline/, skills/, soul.md` (`tool_guard.py:53-56` actually allows reading `soul.md` and `.cline/rules/01-rag-agent.md`).
  - `prompts/agent.md:56` says *"max 10 per cycle"* for lead queries, contradicting line 64 (*"At most 8 new queries per cycle in total"*); `tool_guard.py:227` and `add_queries.py:23` cap at 8 per call.
  - `prompts/agent.md:47-48` states `./rag inspect_artifact` prints *"no document text"*, while line 108 references its `untrusted_text` (document title).

---

### [L-2] `agent/agent_exec.py` Can Orphan Child Subprocesses When Killed by Cline's 30s Timeout
- **Category:** Orphaned child processes / `SIGTERM` handling
- **Files & Lines:** `agent/agent_exec.py:46-60`
- **Details:** `run_first()` invokes `subprocess.run(cmd, ...)` without a `SIGTERM` handler or child process-group teardown. If Cline kills `agent_exec.py` after 30s (e.g., during a slow `./rag status` or `./rag step`), the child subprocess can continue running orphaned.

---

### [L-3] `Parser._low_text_pages` Ignores Per-Instance `compute_budget`
- **Category:** Processor configuration bug
- **Files & Lines:** `processor/pipeline/parser.py:42, 169-170`
- **Details:** `Parser.__init__` stores `self.compute_budget`, but `_low_text_pages` is a `@staticmethod` that reads the module-global `COMPUTE_BUDGET` instead of `self.compute_budget`, silently ignoring per-instance `min_chars_per_page_before_ocr` overrides.

---

### [L-4] Minor Crash-Window, Guard, and Edge-Case Issues
- **Export state crash window (`collector/src/rag_collector/export.py:315-323`):** A crash between `os.replace(ready_tmp, final / "READY")` and `save_state(outbox, state)` leaves a `READY` batch unrecorded in `export_state.json`, causing duplicate re-export on the next run (harmless to LanceDB due to `doc_id` dedup, but wastes disk/processing).
- **Unhandled `IndexError` on `./rag _bg` (`unified/__main__.py:156-157`):** Calling `./rag _bg` with no arguments raises `IndexError` on `rest[0]`.
- **Pre-quote `2>&1` stripping (`agent/tool_guard.py:369`):** `re.sub(r"(?:(?<=\s)|^)2>&1(?=\s|$)", " ", cmd)` runs before quote scanning, silently mutating literal `2>&1` inside quoted arguments (e.g. `./rag log "stderr 2>&1 test"`).
- **Module-global `RUN_COUNTS` in `fetch_worker.py:163`:** `used = polite_fetcher.RUN_COUNTS` accumulates across multiple in-process `fetch_worker.run()` calls if reused in tests/library code.
- **Broad `data/` scope in `inspect_artifact` (`agent/tool_guard.py:120-124`):** `_data_path` allows any file under `collector/data/` (including `docket.sqlite`, `nse_unified.sqlite3`, `export/`, `search_cache/`), though `inspect_artifact.py` only emits aggregate counts/metadata rather than raw contents.

---

## Summary Table of Findings

| ID | Severity | Location | Issue |
|---|---|---|---|
| **C-1** | Critical | `unified/loop.py:112-114`, `processor/pipeline/steps.py:130-139` | `Loop.pending()` reads `counts.pending_count` instead of `outbox.pending_count` → backpressure completely dead |
| **C-2** | Critical | `processor/pipeline/indexer.py:233-244, 297-302, 520-522, 568-575` | No `SIGKILL` escalation on shutdown/stall + `join_thread()` hang leaks permanent MPS embedder & worker orphans under `--watch` |
| **H-1** | High | `processor/pipeline/embedder.py:54-59, 112-133, 164-165` | MPS OOM halving pads `8→16` and `2/1→4` using stale `self.batch_size`, retrying the failing shape |
| **H-2** | High | `collector/src/rag_collector/loop.py:103-111, 123-130` | `last_run_summary()` reads stale run JSON from earlier cycles in agent mode |
| **H-3** | High | `collector/scripts/job.py:118, 128`, `prompts/agent.md:13`, `agent/tool_guard.py:440-441` | `job.py` emits `.venv/bin/python scripts/job.py wait N` which `tool_guard.py` blocks |
| **H-4** | High | `collector/scripts/job.py:101-116`, `collector/src/rag_collector/steps.py:193` | `job.py` bypasses `locks/collect.lock` and has unlocked TOCTOU race on `job.json` |
| **H-5** | High | `agent/run_agent.sh:95`, `collector/scripts/collect.py:407-416` | `run_agent.sh` ignores `SIGTERM`; `collect.py` waits 120s/worker before terminating on `SIGTERM`/error |
| **H-6** | High | `collector/src/rag_collector/loop.py:145-151` | Single collector cycle exports up to 20 batches, bypassing `--max-pending` |
| **M-1** | Medium | `unified/bg.py:32-36, 82-123` | Unlocked TOCTOU in `bg.start()`; `_bg` lacks `SIGTERM` cleanup |
| **M-2** | Medium | `collector/src/rag_collector/export.py:240-250, 269-284` | In-batch duplicate `local_path`/`sha256` collides on `0o444` files/sidecars |
| **M-3** | Medium | `unified/loop.py:31, 126-141, 168-174, 188-191`, `processor/pipeline/watch.py:96` | Hardcoded `PROCESSOR_STOP`, unmonitored watcher during cycles, masked `--once` failures, unreaped `self.child` |
| **M-4** | Medium | `processor/pipeline/indexer.py:524-539` | Replacement worker deadlocks if crashed worker already consumed its `None` sentinel |
| **M-5** | Medium | `collector/scripts/process_worker.py:29-36` | `os.kill(pid, 0)` cannot detect zombie sibling `fetch_worker` during `discover()` |
| **M-6** | Medium | `agent/sandbox.py:83, 93` | `.env` and `.git/config` remain writable in SBPL; `root` unescaped in regex |
| **M-7** | Medium | `collector/src/rag_collector/steps.py:32, 191`, `processor/pipeline/steps.py:210, 236, 238` | Exclusive locks on read-only steps cause spurious `retry_later` |
| **L-1** | Low | `prompts/agent.md:2, 6-7, 47-48, 56, 64, 108`, `.cline/rules/01-rag-agent.md:3, 91-92` | Self-contradictory read/query-cap/inspect instructions |
| **L-2** | Low | `agent/agent_exec.py:46-60` | No `SIGTERM` child teardown on 30s Cline command timeout |
| **L-3** | Low | `processor/pipeline/parser.py:169-170` | `_low_text_pages` reads global `COMPUTE_BUDGET` instead of `self.compute_budget` |
| **L-4** | Low | `export.py:315-323`, `unified/__main__.py:157`, `tool_guard.py:120-124, 369`, `fetch_worker.py:163` | Crash window, `_bg` `IndexError`, `2>&1` quote strip, global `RUN_COUNTS`, broad `inspect_artifact` path |

---

## Verification and fixes (Claude, 2026-11-24)

Every finding was checked against the code on `audit/neon` before anything was changed. Each fix has a
regression test next to it. Afterwards `scripts/check.sh --offline --e2e` passed (collector 352 tests, processor
125 passed / 3 skipped, unified + agent 271, sandbox probe, end-to-end export → ingest → quant-sync).

| ID | Verdict | Fix | Test |
|---|---|---|---|
| C-1 | confirmed | `Loop.pending()` reads `outbox.pending_count` (then `counts`); an unreadable count is logged as `pending_unknown` instead of silently skipping backpressure | `test_pending_reads_the_real_processor_status_shape`, `test_backpressure_triggers_from_a_real_status_payload`, `test_unknown_pending_is_logged_not_silent` |
| C-2 | confirmed (1–3); 4 is covered by the kill escalation | `_run_parallel`: SIGKILL any child still alive 10 s after SIGTERM; a stalled worker gets SIGKILL 15 s after its SIGTERM | `test_stalled_worker_that_ignores_sigterm_is_killed` (worker ignores SIGTERM; it hung forever before the fix) |
| H-1 | confirmed | the retry's batch size is passed to `_forward_fixed`, so padding never goes back up to the shape that ran out of memory | `test_fixed_shape_out_of_memory_retries_smaller_shapes` |
| H-2 | confirmed | agent mode only reads run summaries written after the segment started | `test_agent_mode_ignores_run_summaries_from_earlier_cycles` |
| H-3 | confirmed | `job.py` prints `./rag job wait N`; `status_report` hints are `./rag docket --stats` / `./rag web_search --engines` | `test_hints_printed_by_the_collector_tools_pass_the_guard` |
| H-4 | confirmed | `job.py start` takes `locks/collect.lock` (the lock the collect steps use) and hands it to collect.py, which holds it for its whole run; busy → `started: false`. This also closes the start/start race | `test_job_holds_the_collect_lock` |
| H-5 | partly. run_agent.sh: confirmed. collect.py: it *does* handle SIGTERM (`common.graceful_sigterm`); the real problem was waiting up to 120 s per worker before stopping them | the collector loop runs the agent in its own process group and interrupts the whole group with SIGINT (= Ctrl-C, which run_agent.sh handles). collect.py: if the run did not complete, workers get SIGTERM after a 5 s grace, then 30 s, then SIGKILL | existing supervisor/e2e runner tests |
| H-6 | confirmed, lower severity (only with a small `--export-batch`) | `rag_collector.loop --export-max-batches`; `./rag loop` passes the room left under `--max-pending` | `test_export_burst_is_capped`; live: `./rag loop --once --export-batch 2` now writes 3 batches (was 20) |
| M-1 | confirmed, low impact | `bg.start` runs under an flock on `logs/bg/.start.lock`; pid-qualified temp files | `test_bg_start_is_atomic` (8 concurrent starts → 1 ok, 7 retry_later) |
| M-2 | **confirmed on real data**: the e2e run counted 3 in-batch duplicates among 328 candidates | in-batch sha256 / destination de-duplication (`duplicate_in_batch`) | `test_export_skips_in_batch_duplicates` |
| M-3 | confirmed (1, 2, 3, 4). Letting a running cycle finish after `logs/STOP` is by design | STOP path resolved from `STOP_FILE` / `DATA_DIR`; the watcher is checked every 5 s during a cycle; `watch --once` exits 1 on a failed step; `finally` reaps the collector child | `test_processor_stop_follows_its_settings`, `test_collector_cycle_keeps_the_watcher_alive`, `test_watch_once_exits_nonzero_when_a_step_failed` |
| M-4 | confirmed (narrow window) | a spare stop marker is queued for each replacement worker | no dedicated test (the window is hard to reproduce); the indexer suite still passes |
| M-5 | confirmed; only delays the last parse batch | **deferred** (AUDIT.md open items) | |
| M-6 | confirmed | sandbox denies writes to `.env*` and everything under `.git/`; `root` is regex-escaped | `test_secrets_and_git_metadata_are_not_writable`, `test_regex_metacharacters_in_the_root_are_escaped` |
| M-7 | confirmed | `run_step(lock_dir=None)` (both stepkits, still byte-identical) for collector/processor `status`, `health`, `quant-bracket`; `audit` keeps its lock (it writes `dataset_audit.json`) | `test_read_only_steps_take_no_lock`, `test_read_only_steps_run_while_their_lock_is_held` |
| L-1 | confirmed | prompts: the one-time read of `soul.md` + rules is an explicit exception; lead queries count toward the 8 per cycle; inspect_artifact: "no document body, only the title as `untrusted_text`" | `test_agent_prompts.py` |
| L-2 | plausible; agent-access steps finish well under 30 s | **deferred** | |
| L-3 | confirmed | `_low_text_pages` uses `self.compute_budget` | `test_low_text_threshold_uses_the_instance_budget` |
| L-4 | `_bg` without a run id: confirmed, now a usage error (exit 2). Export crash window, `2>&1` stripping, `RUN_COUNTS`, inspect_artifact scope: accepted as-is (harmless or aggregate-only) | | `test_internal_bg_without_a_run_id_is_a_usage_error` |

Also checked live:
- `./rag loop --once --mode deterministic --max-queries 1 --export-batch 2`, with a temporary outbox and DATA_DIR. It exported 3 batches (capped), the processor ingested all 3, quant-sync was ok, and it exited 0.
- Two `processor.status` calls at the same time both return ok.
- The real sandbox profile blocks writes to `.git/` and `collector/.env`, and `logs/` is still writable.
