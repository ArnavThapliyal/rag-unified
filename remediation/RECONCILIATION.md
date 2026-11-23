# Phase 0: Reconciling the Two QA Audits

Baseline: `main` at `35df260`. Remediation branch `remediation/qa-phase1` (worktree `../nse-remediation`).

Inputs:
- **Claude audit:** branch `qa/claude`, commit `2360aa9`. It has a report, a 101-test adversarial suite, an offline integration harness and its evidence.
- **AGY audit:** `~/.gemini/antigravity-cli/brain/9e8b7dcf-…/QA_SECURITY_AUDIT_REPORT.md`. Branch `qa/agy` has no commits beyond `35df260` and a clean worktree, so the audit is report-only. Its repro steps are `python -c` snippets plus source inspection.

I re-ran the Claude suite against the unchanged baseline in a throwaway sandbox: **57 failed, 44 passed**, the same as the report.
Evidence: `remediation/evidence/00_baseline_qa_suite.txt`.

The `qa/` folder was copied unmodified from `qa/claude` so it can be re-run as a regression suite. Neither audit worktree was touched.

## Evidence levels used below

| Level | Meaning |
|---|---|
| **R** | Reproduced: a failing test or a run on the baseline shows it. |
| **P** | Present in production data (`data/processed`, read-only). |
| **C** | Confirmed by reading the source. The defect is real, but neither audit executed it. |
| **T** | Theoretical: possible, but not reachable in the current deployment, or only under unusual conditions. |

## 1. The 37 vs 48 duplicate-company discrepancy

Both numbers are correct. They count different things in the same 491-record master.

| Measure | Count | Who used it |
|---|---|---|
| Normalized company names with more than 1 record | **48** names (96 records) | AGY, "48 duplicated entities" |
| Of those, same-name pairs whose dates are ≤ 60 days apart | **37** | Claude, "37 duplicate pairs" |
| Pairs involving an ISIN on one side and none on the other | **0** | (both audits attributed duplicates partly to this) |
| Records with an ISIN at all | 5 of 491 | |

Classifying the 48 groups, with every record inspected:

| Group | Count | Verdict |
|---|---|---|
| `DelistedCos_Buy_Sell.xls` (type OTHER, no symbol) vs `delisted.xlsx` (type COMPULSORY or IBC, with symbol), effective dates 1 day apart | 36 | **Duplicate sources of one event.** They are not merged because `same_event` requires equal types. |
| `ISIN` (2 garbage rows from label lines: F-H5 / AGY-1) | 1 | Not a company at all. |
| Exchange order (order date only) vs `delisted.xlsx` (effective date only), order 62–89 days before effective | 3 (Shri Lakshmi Cotsyn, Sunil Hitech, Zylog) | **Same event**: an order, then the delisting taking effect. |
| Same symbol and type, different dates from two NSE sources (B S Appliances 2017-03-24 vs 2018-05-22) | 1 | **Ambiguous**: the sources disagree. Must not be auto-merged; flag for review. |
| Buy_Sell.xls vs delisted.xlsx with dates 6–8 months apart (Maars, Indus Fila, Jenson Nicholson, Vital Comm.) | 4 | **Ambiguous**: the Buy_Sell date may be a different milestone. Flag; don't merge. |
| Order dated *after* the effective date (Autoriders: effective 2021-02-24, order 2023-03-31) | 1 | **Ambiguous / possibly distinct**: flag; don't merge. |
| Husys Consulting: `HUSYS` COMPULSORY 2016 and `HUSYSLTD` VOLUNTARY 2021, both from `delisted.xlsx` | 1 | **Two legitimate, distinct delisting events** under two symbols. Must stay separate. |
| Undated pair | 1 (the `ISIN` group) | n/a |

Conclusions:
- The main real cause is the type mismatch in `same_event` (36 of 47 real groups).
- The ISIN/no-ISIN split (AGY F2, Claude H1) is real in code and reproduced by a unit test. However, it causes **none** of the production duplicates today, because almost no production record has an ISIN. It will matter as soon as ISIN-bearing sources are merged.
- Any fix must merge the 36 + 3 groups, keep Husys as two events, and flag the 6 ambiguous groups for a human instead of silently merging them.

## 2. Consolidated findings matrix

Severity is reconciled: **Crit**, **High**, **Med** or **Low**. "C/A" shows which audit reported the finding (C = Claude, A = AGY; – = not reported).

### Security and execution

| ID | Finding | C/A | Evidence | Sev | Notes / reconciliation |
|---|---|---|---|---|---|
| S1 | Pipe filters can read and write files: `cut … .env`, `grep -eKEY .env`, `grep -r`, `sort -o…`, `sort --files0-from=` | C2 / – | R | Crit | AGY says "the agent cannot execute arbitrary bash" and rates the guard WARN. That is wrong: Claude's 6 bypasses reproduce. |
| S2 | **New (neither audit):** the guard treats `$VAR`, `~`, globs and brace expansion as literal text, but bash expands them. `common.py log $HOME` is allowed. `logs/issues.jsonl` and `logs/runs/*.json` are git-tracked and pushed to a public repo, so an expanded value can be published. | – / – | R | High | The guard reasons about a string that bash interprets differently. Root cause of S1/S2: a blacklist parse of a shell string. |
| S3 | Agent scripts accept flags that undo user decisions: `job.py start --unlimited` / `--max-files all`, `docket.py --stats --unblock/--retry/--promote`, `report_issue.py --close=1` / `--clos 1` (argparse prefix matching) | C-H3 / – | R | High | AGY says the agent "cannot run `docket.py --unblock`". That is wrong: `--stats --unblock` passes. |
| S4 | Editor/apply_patch on `config/` can change disk, politeness, deny-list and blocking settings, and can delete the plan | C-H3 / A-11 | R | High | AGY rated this Low. Raised to High because it disables the disk guard and the politeness delays. |
| S5 | Fetch-worker crash is reported as `completed`, exit 0. Child exit codes and missing summaries are ignored. | C-C1 / – | R (integration) | Crit | AGY F6 found the trigger but not the silent-success propagation. |
| S6 | The crashing item stays `fetching` with `attempts=0`. When the lease expires it crashes every later run (poison pill). | C-C1 / A (partial) | R | Crit | |
| S7 | HTTP-date `Retry-After` raises `ValueError` | C-C1 / A-6 | R | Crit | Both audits found it. |
| S8 | Any other unexpected exception in the fetch loop (ENOSPC, SQLite busy) kills the worker the same way | C-C1 / A (#8) | C | High | |
| S9 | `run_agent.sh` decides to restart by grepping free model text | C-L3 / – | C | Low | Works today, but fragile. Not changed in this phase; see unresolved risks. |

### Data integrity

| ID | Finding | C/A | Evidence | Sev | Notes |
|---|---|---|---|---|---|
| D1 | ISO dates read day-first (`2017-03-04` → `2017-04-03`) | C-C3 / A-3 | R | Crit | No ISO cells exist in production `data/raw` (Claude checked), so this is latent. |
| D2 | `.replace("st ", …)` breaks every "August" date. "March 21st, 2020" fails. | C-C3 / – | R, P (5 listing dates) | Crit | AGY missed it. |
| D3 | Label lines become companies (`ISIN`, `ISIN of …`, `Symbol: X \| ISIN`) with `VERIFIED_PRIMARY` | C-H5 / A-1 | R, P (4 records) | Crit | AGY listed the 4 production records; Claude reproduced the mechanism. |
| D4 | Same company with and without an ISIN is never compared (`_key = isin or name`) | C-H1 / A-2 | R | High | Not the cause of the current production duplicates (see §1). |
| D5 | Same event, different type (OTHER vs COMPULSORY, 1 day apart) is not merged | C-H1 / A-2 | R, P (36) | Crit | This is the real cause of the duplicates. |
| D6 | Order-date-only and effective-date-only records of one event are not paired | – / – | P (3) | Med | New. |
| D7 | Audit flags duplicates only with the same key **and** type: reports 4 of 37 | C-H1 / A | R, P | High | |
| D8 | Type from substring matching over the whole row, including the company name ("Amalgamated…", "Vib**c**on" → IBC). One NCLT mention makes a document IBC. | C-H4 / – | R | High | |
| D9 | A NEEDS_REVIEW (e.g. OCR) field is copied into a VERIFIED record without trace. There is no order ≤ effective plausibility check. | – / A-4 | R (A's snippet) | High | This is the "OCR confidence propagation" issue. |
| D10 | Conflicting dates or types across sources are merged silently, with no record of the conflict | C-M11 / – | R | Med | |
| D11 | `record_id` changes when a better source arrives | C-M11 / – | R | Med | |
| D12 | Non-atomic writes: master CSV/JSONL, parsed JSONL, search cache, metadata, audit | C-M2 / A-9 | C (AGY cites issue #4 race) | Med | |
| D13 | ISINs not normalized (spaces, lowercase) or validated in `normalize()` | C-M7 / – | R | Med | |
| D14 | Exit price: `Rs. 125.50` → None, `Rs.125/-` → 0.125; "Last Traded Price" mapped to exit price | C-M6 / – | R | Med | |
| D15 | `PROPOSED_RE` matches "proposals"; `harvested_from` URL alone makes any document relevant | C-M8 / – | R | Med | |
| D16 | Out-of-scope years (34 records before 2006) are kept in the master | C-M11 / – | R, P | Low | They will be routed to their own file, not dropped. |
| D17 | Circular tables without a date column: header-text `w.e.f.` dates are not back-filled | – / A-12 | C (production file cited) | Med | Feature gap. Deferred; see unresolved risks. |
| D18 | No row-count plausibility bound per sheet | C-M / – | R | Low | |

### Operational resilience

| ID | Finding | C/A | Evidence | Sev | Notes |
|---|---|---|---|---|---|
| O1 | Domain blocks never expire. Timeouts and connection errors count toward a block (a Wi-Fi blip blocks NSE permanently). | C-H2 / A-5 | R (integration) | High | AGY attributes the block to 429. Claude reproduced it with a plain network outage, which is worse. |
| O2 | One 403 marks the URL dead. Four 429s mark it dead. | C-H2 / – | R | High | |
| O3 | Docket starvation: the top 300 candidates can all be one delayed or over-budget domain → sleep, or a premature `sources_exhausted` | – / A-7 | R (new test) | Med | Theoretical at today's backlog, but reachable: one domain has thousands of parked items. |
| O4 | Re-filing a URL drops its company tag (per-company cap and coverage are wrong) | – / A-8 | R (new test) | Med | |
| O5 | A clean empty answer from provider 1 is final; fallback providers are never asked | – / A-10 | C | Med | Documented design choice. Changed to configurable, falling back by default. |
| O6 | A corrupt (truncated) search cache file poisons a query forever | C-M2 / – | R | Med | |
| O7 | Disk guard: none in the process worker/OCR, a non-numeric config value crashes, ENOSPC is unhandled | C-M5 / A (#8) | R | Med | |
| O8 | No download size cap; a 200 "Access Denied" soft-block page is saved as the PDF | C-M1, M5 / – | R | Med | |
| O9 | Sleeps after the final retry; the last connection error is lost after a 5xx | C-M9 / – | R | Low | The QA suite *documents* this behaviour in two passing tests, which will flip. |
| O10 | OCR failure (tesseract exit ≠ 0) is cached as empty forever; one page timeout fails the whole document | C-M3 / – | R | Med | |
| O11 | A file that raises is re-parsed every batch; `--retry parse_error` loops | C-M3 / – | R | Low | |
| O12 | Killed after download but before the docket commit → re-download on lease expiry | – / A (#1) | T | Low | Costs one extra request. Accepted. |
| O13 | Two fetch workers don't share domain budgets | – / A (#4) | T | Low | `job.py` allows one job at a time. Accepted and documented. |

### Tooling, tests, docs (Low)

`check.sh` rewrites the tracked audit file. Tests write `logs/.issues.lock` in the real repo. The README test count and the AUDIT.md statements are stale. `tests/test_parse.py` passes a literal `\n`, so the guard it claims to test is never reached (C-§5).

## 3. Defects neither audit adequately tested

1. **S2** (shell expansion: `$VAR`, `~`, globs) and the general mismatch between the guard's parser and bash.
2. **D6**: pairing an order date with its effective date.
3. **Husys-type distinct events.** Neither audit checked that a fix would *not* merge two genuine events of one company. A naive "same company within N days" merge would get B S Appliances and Husys wrong.
4. **The policy-denied branch in the fetch loop** calls `done_fetch` without a lease, and `done_fetch` for unknown URLs silently records nothing.
5. **What Cline does if the hook itself fails**, for example when `.venv` is missing. This needs a live Cline session, so it cannot be tested offline. Listed as an unresolved risk.
6. **The tool guard's `commands` entries given as `{command, args}` objects.** Covered by the new tests.

## 4. Theoretical vs reproduced

- **Reproduced:** S1–S8, D1–D5, D7–D11, D13–D15, D18, O1–O4, O6–O11.
- **In production data:** D2 (5 listing dates), D3 (4 rows), D5 (36 pairs), D6 (3 pairs), D16 (34 rows).
- **Confirmed by code but not exercised by either audit:** D12, D17, O5.
- **Theoretical or accepted:** O12, O13, S9.
- **AGY claims that are incorrect:**
  - "Agent cannot execute arbitrary bash… cannot run `--unblock`". S1 and S3 disprove this.
  - "48 companies duplicated *because* of ISIN vs non-ISIN keys". 0 of 48 are ISIN splits.
  - "Tata Motors 5 sources → 3 records". Not reproducible from the files; no such record exists.
