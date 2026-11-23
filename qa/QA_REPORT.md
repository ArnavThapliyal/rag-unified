# QA Report: NSE Historical Delisted Companies Archive

Assessed 2026-11-23 on branch `qa/claude` (worktree `../nse-qa-claude`, from `35df260`). No source code was changed.
All destructive and adversarial tests ran on throwaway copies (`qa/make_sandbox.sh`). The integration test used a
local mock (`qa/mock_server.py`) that served as both SearXNG and an HTTP proxy, and it refused every HTTPS CONNECT. No external
site, API, Docker service, LM Studio, Cline or `run_agent.sh` was started. No key file was read. The production
`data/` and `logs/` folders were only read, for impact checks.

## 1. Overall readiness: NOT READY for an unattended long run

The core design is sound: docket and queue, atomic downloads, leases, resumability, the relevance gate and backoff. The offline
checks pass. But three classes of defect would make a long unattended run either stall silently, write wrong
"verified" data, or let the agent cross the security boundary the README promises:

* **A fetch-worker crash is invisible, and one bad URL repeats it forever (C1).** The run reports `completed` with exit 0, and no
  downloads happen again.
* **The tool guard can be bypassed (C2).** Piped `cut`/`grep` can print `.env` or `~/.config/.../keys.env`, and `sort -o` can overwrite
  `scripts/`. Agent-allowed scripts also accept flags that undo the user's caps, unblock domains or close issues (H3).
* **Dates are silently corrupted (C3).** ISO dates are read day-first, and every "August" date is dropped. Because the parse
  succeeds, these rows still carry `VERIFIED_PRIMARY`.

The production dataset already has **37 duplicate company/event pairs** out of 491 records, and the audit reports only 4 (H1).

Fix C1–C3 and H1–H3 before starting a long run. The rest can follow.

## 2. Critical bugs

### C1. A fetch-worker crash is reported as success, and the item that caused it becomes a poison pill
* `polite_fetcher.py:132` runs `int(r.headers.get("Retry-After"))`. RFC 9110 allows Retry-After to be an HTTP date, and then this
  raises `ValueError`. `fetch_worker.run` only catches `KeyboardInterrupt` (`fetch_worker.py:177`), so the worker dies.
* `collect.py:383-415` ignores the child exit codes and the missing child summary. The run ends `status: completed`,
  `stop_reason: max_queries_reached`, `fetch_stop_reason: None`, exit 0.
* The URL is left `fetching` with `attempts=0`, because `done_fetch` never ran. Once the 15-minute lease expires, it is the top
  candidate again and crashes the worker on **every** later run. Downloads stop for good, while every run summary says
  `completed`. The agent's stop rule ("job ends failed twice") never fires.
* Any other unexpected exception in the fetch loop does the same: a disk-full `OSError`, or a SQLite `OperationalError` after the 60 s busy timeout.
* **Repro:** run `qa/run_integration.sh /tmp/x`, step 2. Evidence: `qa/evidence/integration_worker_crash.out` (traceback, then a
  `"status": "completed"` summary). In the runs after the lease expired: `crash=1 … summary=completed idle None docket=fetching/0`, twice.

### C2. Tool-guard bypasses: credential exposure and arbitrary file writes
`tool_guard.check_filter` (`tool_guard.py:97-105`) treats every word that starts with `-` as a harmless flag and
skips `cut`'s operands completely. All of the following are **allowed** (`check()` returns `None`), and I confirmed in a shell on dummy files
that they read or write files:

| Command after an allowed script, e.g. `.venv/bin/python scripts/common.py log x \| …` | Effect |
|---|---|
| `cut -c1-200 .env` or `cut -c1-200 /Users/<you>/.config/nse-delisted-comp/keys.env` | prints the key file |
| `grep -eKEY .env` (pattern attached to `-e`) | prints matching lines of the file |
| `grep -r API_KEY` | macOS grep with no operand searches `.`, so `.env:SERPER_API_KEY=…` is printed |
| `sort -oscripts/collect.py` | overwrites a source file |

The README says the agent "can't" read the keys, and that the tool guard enforces this. `run_agent.sh` only removes keys from the
environment. The files stay readable through these filters. The risk is real because the agent sees document titles and
search snippets, which are a prompt-injection channel.
**Repro:** `qa/test_adv_io_security.py::test_pipe_filters_cannot_read_or_write_files` (6 cases fail).

### C3. Date parsing silently corrupts or drops dates
`parse_nse_records.to_iso` (`parse_nse_records.py:113-114`):
* `dayfirst=True` is applied to ISO strings: `2017-03-04` → `2017-04-03`, `2007-06-05` → `2007-05-06`. The swapped date then
  drives the year, FY and regime. A date near a boundary can move to another FY or SEBI regime: for example, `2009-06-09` becomes `2009-09-06`,
  which flips the regime.
* `.replace("st ", " ")`, meant for "1st", also hits "Augu**st** ", so **every date containing "August" becomes `None`**:
  `15 August 2017`, `August 1, 2017`, `1st August 2017`. Also unmatched: `March 21st, 2020`.
* End-to-end proof: in the integration master, "Poddar Housing", given `2008-03-04` in the source table, has `effective 2008-04-03`.
  "Augusta Mills" lost both its order date and effective date (August 14 and August 21, 2008), so it has no year or regime.
  "Bhagyanagar" lost `15 August 2017`. See `qa/evidence/integration_master.csv`.
* Production impact so far: no ISO table cells were found in `data/raw`. XLSX dates arrive as datetime objects and are not affected.
  The 5 dropped August dates found in production order PDFs were listing dates, so the bug is mostly latent today. It will
  hit the first open-web source that uses ISO or "August" dates. These rows keep their `VERIFIED_PRIMARY` status, and the audit cannot detect the error.

## 3. High-priority bugs

### H1. Duplicate records, already in the production master
* `same_event` (`normalize_and_merge.py:93-102`) merges two rows only if their types match, or if they have the exact same effective date. In the
  **real master, 37 same-company pairs are ≤60 days apart**, most of them 1 day apart with `OTHER` vs `COMPULSORY`. Examples: Acropetal Technologies, Aftek,
  Arvind Remedies, Era Infra Engineering (2018-08-08 COMPULSORY and 2018-08-09 OTHER).
* `_key = isin or norm_name(name)` (`:89`): the same company with an ISIN in one source and none in another (the README says order PDFs
  usually have none) becomes two records. Reproduced in `test_same_company_with_and_without_isin_is_one_record`.
* The audit's `possible_duplicate_event` (`audit_dataset.py:69-74`) needs the same key **and** the same type, so it reports only 4.
  The ISIN-vs-name split is never flagged (`test_audit_flags_isin_vs_name_split`).

### H2. Domain blocks are permanent, and a local network blip triggers them
* `docket.domain_result` (`docket.py:213-228`) blocks a domain after 3 consecutive failures of any kind, including timeouts and connection
  errors, and nothing ever unblocks it. The agent is told that blocked domains stay blocked.
* **Repro (integration, step 5):** stop the network (the mock) while 4 NSE items are pending. Result: `nseindia.com` is blocked with
  "3 consecutive failures (last: None)". The run reports `status: completed, stop_reason: search_unavailable`.
  An unattended run that loses Wi-Fi for a minute stops using NSE permanently.
* Related: a single 403 marks the URL `dead` immediately, although WAF 403s are often temporary (`docket.py:202`). Four 429 attempts also mark it `dead`
  (each attempt is itself 4 HTTP requests), although the README calls 429 retryable (`test_429_throttling_eventually_kills_the_url`).

### H3. The agent can override user-owned limits and safeguards
Each of these is allowed by the tool guard, and the first three were executed in a sandbox:
* `job.py start --unlimited` (or `--max-files all …`): `_job` checks only the sub-command, so the user's run caps are bypassed.
  Dry-run proof: the job result shows `limits: {"mode": "unlimited", …}`.
* `docket.py --stats --unblock nseindia.com` / `--list dead --retry dead` / `--stats --promote --limit 100000`: `_docket` checks
  only `args[0]`, and `docket.main` then runs `--unblock`. Executed: `{"unblocked": 1}`, block removed.
* `report_issue.py --close=1` or `--clos 1`: the guard looks for the literal `--close`, and argparse accepts both forms. Executed: `{"closed": 1}`.
* The editor or apply_patch can change `disk.min_free_gb` (disabling the disk guard), `domain_limits.*.delay_s` / `max_per_run` (politeness),
  `deny_domains` and `block_domain_after_failures`, because `SEARCH_KEYS_RE` (`tool_guard.py:143-145`) protects only the search, run-limits and focus keys.
  `*** Delete File: config/query_plan.json` is also allowed.

### H4. Misclassified delisting types
`classify` does substring matching in priority order (IBC → merger → compulsory → …), applied to the whole row text including the company name
(`parse_nse_records.py:53-59, 94-99, 205`):
* "Amalgamated Bean Coffee …", whose reason is "Compulsory", is typed `AMALGAMATION_MERGER` (also seen end-to-end).
* "Vibcon Industries" is typed `IBC_NCLT` (the substring "ibc").
* A voluntary exit-offer document that mentions "NCLT" once is typed `IBC_NCLT`.

### H5. False-positive company names from ISIN label lines
`rows_from_text` passes `has_id=True`, which skips the company-name check (`parse_nse_records.py:256`). So `ISIN: INE002A01018`,
`ISIN of the Company INE002A01018` and `Scrip Code 500325 ISIN INE002A01018` each produce a "company" named `ISIN…`
with symbol `ISIN`. A row like that from an official domain gets `VERIFIED_PRIMARY`.

## 4. Medium and low-priority issues

**Medium**
1. **Soft-block pages are accepted as documents.** A 200 HTML "Access Denied" page served for a `.pdf` URL is saved and counted as
   fetched, so it is never re-fetched (`polite_fetcher.py:141`). It then becomes a permanent `parse_error`.
2. **A corrupt search cache poisons a query.** Cache files are written non-atomically (`web_search.py:341`), and a truncated one raises
   `JSONDecodeError` on every rerun (`:324`). `collect.py` marks the query `failed`, and `--retry-failed` reads the same broken file again.
3. **OCR failures stick.**
   * Tesseract exiting non-zero caches empty text forever (`ocr.py:48-55`).
   * One page's `TimeoutExpired` makes the whole document a `parse_error`.
   * Files that raise are re-parsed (and re-OCR'd) on every process batch, with no attempt cap.
   * `docket.py --retry parse_error` loops.
4. **Run summaries and indicators are inaccurate.**
   * `indicators.queries_done` and `query_plan_complete_pct` ignore the historical-focus queries (`build_metadata.py:41-57`). The integration
     run did 2 queries; the indicator showed `0/2`.
   * `quarantined` in the summary means failed + dead, not files in quarantine.
   * `harvest_backlog` excludes leased items.
   * The status stays `completed` when every fetch failed or a worker crashed (C1).
5. **The disk guard has gaps.**
   * It runs only in the fetch loop, once every 60 s (`fetch_worker.py:122`). `process_worker`/OCR never check.
   * There is no per-file size cap: a 50 MB body is accepted and held in memory.
   * A non-numeric `disk` value crashes every script (`common.py:200`, `ValueError`).
6. **Exit price parsing is wrong.** `Rs. 125.50` → `None`, `Rs.125/-` → `0.125` (`parse_nse_records.py:195`), and a "Last Traded Price"
   column is mapped to `exit_or_fair_value_inr` (`HEADER_MAP` "price").
7. **ISINs are not validated or cleaned up.** `normalize()` accepts any `isin` string as the dedupe key (`normalize_and_merge.py:69`), and table ISINs written
   `INE 009A01021` are dropped.
8. **The relevance and proposal rules are too broad.**
   * A document linked from any page whose URL contains "delist" is treated as relevant, annual reports included (`parse_nse_records.py:365`).
   * `PROPOSED_RE` matches "proposals", so completed delistings whose URL says "approved-delisting-proposals" are sent to the leads file.
9. **One slow URL stalls the whole fetch worker for about 4 minutes:** a 45 s timeout × 4 tries plus 4+8+16+32 s of sleeps, including a
   sleep after the final try. During that time no other domain is fetched. When a 5xx is followed by connection errors, the final error is lost
   (`polite_fetcher.py:127-161`).
10. **Rebuilding the docket loses state.** The README calls the docket rebuildable, but deleting it and re-importing it loses the blocked domains and attempt counts.
    Pending items come back as `parked` (and are never fetched without `--promote`), and `parse_error` items come back as `fetched`.
11. **`record_id` is not stable**, although the README says it is: a better source with a slightly different date changes the id of the same event
    (`normalize_and_merge.py:133`). Conflicting dates are merged without recording the disagreement.
    Rows outside 2006–2026 stay in the master (34 in production).

**Low**
1. `check.sh` rewrites the tracked file `data/metadata/dataset_audit.json`. The test suite writes `logs/.issues.lock` in the real `logs/` (it is not isolated).
   `bash -n` skips `.cline/hooks/PreToolUse.sh`.
2. Stale documentation:
   * The README says "24 passed" (the actual count is 136).
   * AUDIT.md says `data/` is empty (it holds 491 records).
   * The README's ISIN pattern `IN[A-Z0-9]{9}[0-9]` differs from the code (INE/IN9 plus a Luhn check).
   * `verify_records.py` is undocumented and not wired into the pipeline.
3. `run_agent.sh:92` decides whether to restart by grepping the model's free-text output for `stop_reason…search_engines_suspended`. I
   saw it match the agent's own reasoning. It works today, but it is fragile.
4. A graceful stop leaves the in-flight item leased for 15 minutes. `done_fetch` on a URL that is not in the docket returns `fetched` and records nothing.
5. A malformed `focus.years_ago` or a 20,000-row sheet is not bounded. `historical.window()` raises, and all rows are accepted.

## 5. Missing test coverage
* Dates: no test uses an August date, an ISO string with day ≤ 12, or an ordinal after the month ("March 21st"). `test_wrapped_names_are_joined` uses
  `2018-01-01`, which reads the same either way and so hides C3.
* `tests/test_parse.py::test_order_title_that_is_not_a_company_is_skipped` passes the two characters `\n`, not a newline. So `MATTER_RE`
  never matches, and the `name_ok` guard named in the test is never exercised. It passes for the wrong reason.
* No test covers a child-process failure in `collect.py`, unusual Retry-After values, the poison-item loop, or domain-block expiry.
* Tool-guard tests cover only obvious attacks. Missing: filter flags with attached values, `cut` operands, extra docket flags, `--close=`,
  `job.py start` flags, editing the politeness or disk settings, and patch deletes.
* No test for merging the same company with and without an ISIN, conflicting types or dates, `record_id` stability across new sources, or
  out-of-range years.
* No test of corrupt cache files, OCR failure caching, re-parse loops, soft-block pages, or download size limits.
* There is no end-to-end test across processes (collect → workers → merge → audit). Everything is unit-level with monkeypatching.

## 6. Existing safeguards that genuinely work (verified)
* `pyflakes`, `bash -n` and `pytest` (136 passed) all pass. I re-ran each of them separately from `check.sh`.
* Relevance gate: the broker/defaulter page was rejected (`irrelevant:non_listing_page`), and the SOP and defaulter PDFs were not harvested.
  Proposed lists are routed to leads, and BSE-only companies go to `bse_only_delistings.csv` (the OCR'd BSE notice ended up there).
* Corrupt PDF, corrupt XLSX and HTML-saved-as-PDF are recorded as `parse_error`, and the batch continues.
* A 404 marks the URL `dead` and does not count toward domain blocking. 429/5xx are retried with exponential backoff, then docket backoff (30 min).
  Timeouts are retried. A 404 on an in-window official URL triggers a Wayback lookup (the only outbound attempt, refused by the mock).
* Downloads are atomic (`.part` file, then rename): after a SIGKILL, no partial files remain and the master is unchanged.
* Resume: killed runs are marked `killed`, leases are recovered after they expire, SQLite `integrity_check` is ok, and WAL works across the 3 processes.
* Graceful `job.py stop` gives `interrupted / keyboard_interrupt` for the run and the fetch worker, with no orphan processes.
* The disk guard stops the fetch worker before any download when free space is critical, and its warning tiers work.
* Credentials: a git-tracked `.env` is refused, keys are redacted from provider errors, `where()` never reveals a value, and `read_files` blocks
  `.env*`, paths outside the project, `scripts/` and `data/raw/`. The hook fails closed on garbage input. It fires in real sessions: 51 checks,
  and its tool names match what Cline 3.0.75 emits.
* Search: a clean empty answer is final, failing units are benched, and a fully benched provider raises `SearchExhausted` with a retry time.

## 7. Test execution evidence
| Run | Command | Result |
|---|---|---|
| Project checks | `scripts/check.sh --offline` (worktree) | all ok, exit 0; pytest `136 passed in 0.41s`; audit `{"records": 491, "errors": {}}` |
| Re-run separately | `pytest -q tests`, `pyflakes scripts tests`, `bash -n …` | 136 passed; 0 pyflakes issues; syntax ok |
| Adversarial unit suite | `QA_SANDBOX=<copy> pytest qa/test_adv_*.py` | **101 tests: 57 failed (defects), 44 passed** (`qa/evidence/adversarial_results.xml`) |
| Integration, full path | `qa/run_integration.sh`, step 1 | exit 0; 2 queries, 14 URLs, 8 fetched, 3 harvested, 1 failed (429), 1 dead (404), 1 parse_error; 9 rows → 6 records; audit 0 errors (`qa/evidence/integration_*`) |
| Integration, worker crash | step 2 | traceback plus `completed`, exit 0; item stuck `fetching/0`; crashes again on each rerun |
| Integration, SIGKILL / resume | step 3 | run marked `killed`; master unchanged; lease recovered; re-fetch timed out after about 4 min, `failed` with retry scheduled |
| Integration, graceful stop | step 4 | `interrupted / keyboard_interrupt`; no orphans |
| Integration, outage | step 5 | `nseindia.com` blocked permanently; `completed / search_unavailable` |
| Tool-guard bypasses | shell demo on a dummy `.env`; sandbox `docket.py`, `report_issue.py`, `job.py` | file printed / file overwritten / `{"unblocked": 1}` / `{"closed": 1}` / `mode: unlimited` |
| Production impact (read-only) | scripts over `data/processed`, `data/raw` | 37 duplicate pairs; audit reports 4; no ISO cells in raw; 5 dropped August dates (listing dates) |

Not executed, because it was forbidden or unsafe: a live SearXNG, LM Studio, Cline or `run_agent.sh` session; real network fetches; reading real
key files. The Cline-dependent behaviour (hook invocation, tool names, restart grep) was checked against the existing session logs instead.

## 8. Reproduction
1. Build a sandbox outside the repo: `qa/make_sandbox.sh /tmp/nse_qa`. The script copies `scripts/` and `config/`, starts with empty data, uses SearXNG only, sets delays to 0 and runs `git init`.
2. Run the unit tests: `QA_SANDBOX=/tmp/nse_qa HOME=/tmp/nse_qa/home /tmp/nse_qa/.venv/bin/python -m pytest -q qa/test_adv_*.py -rf`.
   Each failing test name and docstring states the defect and its expected behaviour. C1–C3 and H1–H5 map to these tests:
   `test_retry_after_http_date_crashes`, `test_pipe_filters_cannot_read_or_write_files`, `test_to_iso_formats`,
   `test_same_company_with_and_without_isin_is_one_record` / `test_conflicting_types_a_few_days_apart_are_one_event`,
   `test_domain_block_never_expires`, `test_script_argument_rules` / `test_agent_cannot_edit_politeness_or_disk_settings`,
   `test_company_name_does_not_decide_the_type`, `test_isin_label_lines_are_not_companies`.
3. Run the integration test (about 8 min, local only): `qa/run_integration.sh /tmp/nse_qa_int`.
4. Production duplicate check (read-only): group `data/processed/nse_delisted_master.csv` by `normalize_and_merge.norm_name` and count
   same-name pairs whose dates are ≤ 60 days apart. Result: 37.

## 9. Recommended fixes, in priority order
1. **C1:** parse Retry-After safely (number or HTTP date, else the default). In `fetch_worker.run`, catch exceptions per item, call
   `done_fetch(..., error=…)` so `attempts` increases and the item eventually dies, and keep looping. In `collect.py`, check each child's
   return code and a missing summary, and set `status: error` (non-zero exit) so the agent's "failed twice" rule fires.
2. **C2/H3:** make pipe filters stdin-only: no operands at all, reject `-o`, `-r`, `-R`, `-f`, `--files0-from` and `-e<x>`, and drop `cut`, or
   allow an explicit set of flags. Give `job.py start`, `docket.py` and `report_issue.py` an allowlist of argv (exact flags; reject
   `--unlimited`, `--max-*`, `--unblock`, `--retry`, `--promote`, `--close*`, and argparse abbreviations with `allow_abbrev=False`). For config,
   only allow `add_queries.py` (deny editor and apply_patch on `config/` entirely), or extend the protected keys to `disk`, `domain_limits`,
   `deny_domains`, `open_web`, `block_domain_after_failures` and `fetch_limit_per_query`.
3. **C3:** in `to_iso`, parse ISO (`\d{4}-\d{2}-\d{2}`) with `dayfirst=False`, and strip ordinals only with `(?<=\d)(st|nd|rd|th)\b`. Add
   regression tests: August, ISO with day ≤ 12, "March 21st, 2020".
4. **H1:** merge on a company identity that links ISIN and name (union-find over ISIN ↔ normalized name ↔ symbol). Merge across types within
   a short window (for example ≤ 7 days) and prefer the more specific type and the primary source. Extend the audit to the same company within 60 days, regardless of type
   or ISIN. Then re-merge the production data.
5. **H2:** count only HTTP 403/429/5xx toward a domain block (not connection errors or timeouts). Make blocks expire (for example after 6–24 h), and
   treat 403 and repeated 429 as retryable with long backoff rather than `dead`.
6. **H4/H5:** classify from the reason, status and decision text, not the company name. Use word boundaries (`\bibc\b`, `\bnclt\b`). In
   `rows_from_text`, require the company-name check, or reject label words (`ISIN`, `Scrip Code`, `Face Value`).
7. Medium items, in this order:
   * Soft-block detection (content type vs URL extension, known WAF markers).
   * Atomic search-cache writes, and treat an unreadable cache as a miss.
   * OCR: don't cache a failed OCR, OCR page by page with a per-page timeout, and cap parse attempts.
   * Correct indicators (count historical queries).
   * Disk check in the process worker, plus a per-file size cap with streamed downloads.
   * Exit-price parsing and the price-column mapping.
   * Validate ISINs in `normalize`, and accept spaced or lowercase ISINs.
   * Narrow the `harvested_from` relevance rule and `PROPOSED_RE`.
   * Stop sleeping after the final retry attempt.
8. Low items and documentation: make `check.sh` read-only (write the audit to a temporary file), isolate `.issues.lock` in tests, update the README and AUDIT,
   and record a machine-readable stop reason in the run summary instead of grepping model text.
