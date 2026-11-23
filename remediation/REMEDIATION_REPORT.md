# Phase 1 Remediation Report

> Superseded for release status by `RELEASE_CANDIDATE_REPORT.md` (items 2, 3, 5, 6, 7 of §9 below are resolved there).

| | |
|---|---|
| Branch | `remediation/qa-phase1` (worktree `../nse-remediation`), from baseline `35df260` |
| Scope rules followed | No live collection. No network requests (mocks and a local proxy only). No real credentials read. The production master was not modified, and no dataset was regenerated in the repo. The audit worktrees were not touched. |
| Status | **Not production-ready.** The code defects the audits found are fixed and tested. The production dataset still contains the defects until you approve a regeneration (§8). Several operational risks remain (§9). |

## 1. Audit reconciliation

See [`RECONCILIATION.md`](RECONCILIATION.md). Main points:
- **37 vs 48 duplicates.** Both counts are right, for different measures:
  - 48 company names have more than one record.
  - 37 of those have records ≤ 60 days apart.
- **What the 48 actually are:**
  - 36 are the same event from two NSE lists, with type OTHER vs COMPULSORY, one day apart.
  - 3 are an exchange order paired with its later effective date.
  - 1 is garbage (`ISIN`).
  - 6 are sources that disagree and need a human.
  - 1 (Husys) is **two genuine delistings**.
- **No production duplicate is an ISIN/no-ISIN split.** That cause, named by both audits, is real in the code but not yet present in the data.
- **AGY's tool-guard assessment is wrong.** It says the agent "cannot execute arbitrary bash" and "cannot run `--unblock`", and rates the guard WARN. Claude's bypasses reproduce, and I found another one (S2: shell expansion) that neither audit tested.
- **Neither audit tested** shell expansion, pairing an order with its effective date, keeping distinct events of one company separate, or whether a fix might merge legitimate events.

## 2. Prioritized remediation plan (as executed)

| Priority | Items | Commit |
|---|---|---|
| P0 Security | S1 pipe-filter file access, S2 shell expansion, S3 script flags that undo user decisions, S4 config edits | `2bfacea` |
| P0 Execution | S5 silent worker crash, S6 poison item, S7 HTTP-date Retry-After, S8 any loop exception, O9 retry sleep/error loss | `4673bb4` |
| P1 Data integrity | D1–D16, O10 OCR failures, O11 parse retry loop, D12 atomic writes | `98aca2a` |
| P2 Resilience | O1 domain blocks, O2 retry classes, network outage, O3 starvation, O4 company tags, O5/O6 search, O7 disk, O8 size cap and soft blocks | `c8d0480` |
| P3 Tooling | read-only `check.sh`, test isolation, a test that tested nothing | `014115d` |
| P1 follow-up | 3 data issues found by the dataset preview (§8) | `beb3fc1` |
| Deferred | D17 (header-date back-fill), S9 (restart grep), O12/O13 (accepted) | §9 |

## 3. Implemented fixes

### Security (commit `2bfacea`)

The old guard checked the command *string* against a blacklist, but bash runs it with a different grammar. The new design has four layers:
1. **Exact grammar.** Each command is parsed into a plan: pipelines joined by `&&`, `||` or `;`. Every program, flag and value must match an allowlist:
   - `job.py start` takes only `--max-queries` (≤ 20), `--harvest-only`, `--phase`, and caps **no higher than the user's**.
   - `docket.py` takes only `--stats`, or `--list S [--limit ≤ 100]`.
   - `report_issue.py` cannot close issues.
   - Filters read stdin only: no operands, no `-o`, `-r` or `-f`.
2. **Nothing for bash to interpret.** Outside quotes, `$ \` ~ * ? [ ] { } ! \ # < > ( )` are refused, and inside double quotes so are `$ \` \ !`. The string bash would see therefore means exactly what was checked.
3. **No shell at all.** Every allowed call is rewritten to `.venv/bin/python scripts/agent_exec.py <plan>`. The runner re-validates the plan and runs the scripts as argv lists; `head`, `tail`, `wc`, `grep`, `sort`, `uniq` and `cut` are reimplemented in Python. A hand-written token gains nothing, because the runner validates it again.
4. **No direct edits.** `editor` and `apply_patch` are refused everywhere; the plan changes only through `add_queries.py`. Every argparse parser now has `allow_abbrev=False`, so `--clos 1` no longer means `--close`.

Supporting changes:
- The agent prompt and rules are updated.
- The prompt's stop rule named a run status that never exists (`failed`). It now names the real ones: `error`, `killed`, `unknown`.

### Execution reliability (commit `4673bb4`)

- **`retry_after()`** parses delay-seconds or an HTTP date. There is no sleep after the last attempt, and the last network error is kept.
- **Fetch worker:**
  - An exception on one item is recorded as a failed attempt, and the worker continues.
  - Three unexpected errors in a row stop it with `worker_errors`.
  - `ENOSPC` stops it with `low_disk_space`.
  - Any other error stops it with `error: …`, and the summary is still written.
  - It exits non-zero on failure.
  - A graceful stop releases the lease it holds.
- **Docket:** re-leasing an expired lease counts as a crash. After 2 crashes the item is `dead`, so a URL that kills the worker cannot kill every later run.
- **`collect.py`** checks each worker's exit code and whether it wrote a summary. A crash gives `status=error`, `stop_reason=fetch_worker_failed (exit N: …)` and exit 1, and the summary has a `workers` block.

### Data integrity (commit `98aca2a`)

**Parsing**
- ISO dates are never read day-first. Ordinals are stripped only after a digit, which fixes every "August" date and "March 21st".
- Label lines (`ISIN:`, `ISIN of …`, `Symbol: X | ISIN`, `Scrip Code N`, CINs) never become companies, and the label prefix is stripped from real names.
- Delisting types are classified by whole words, and the most-mentioned type wins. A row's type comes from its reason column or its non-name cells, never from the company name.
- ISINs are cleaned and validated. Exit prices are parsed correctly, and "Last Traded Price" is no longer read as the exit price.
- `PROPOSED_RE` no longer matches "proposals". A document linked from a delisting page must itself mention delisting, or have no text at all.
- More than 5,000 rows from one file is a parse error.

**OCR and parse errors**
- A failed or timed-out OCR page is skipped and reported, and only error-free OCR is cached.
- OCR that fails on every page is a parse error.
- A broken file is not re-parsed until the file or the parser changes.

**Merging (one record per event)**
- **Company:** union-find over ISIN, normalized name and symbol. It never joins two ISINs, and it never guesses a no-ISIN row into one of two ISINs that share a name.
- **Event:**
  - effective dates within 30 days, or 7 days when two specific types disagree;
  - or an order date up to 270 days before an effective date, the same window `verify_records.py` already used.
  - Anything else stays separate. Husys stays two records, and B S Appliances (14 months apart) is flagged, not merged.
- **New columns:**
  - `source_conflicts` records every disagreeing date or type.
  - `review_flags` records fields borrowed from NEEDS_REVIEW/OCR sources (`order_date:needs_review_source`), implausible order dates that were rejected, and ambiguous companies.
- Year, financial year and regime are recomputed from the merged dates.
- `record_id` stays stable through `record_sources.json`.
- Years outside 2006–2026 go to `out_of_scope_delistings.csv`.

**Atomic writes**
- Every dataset and metadata file is written atomically: temp file, fsync, then `os.replace`. That covers the master, the parsed rows, the metadata, run summaries, docket stats, the job file, issues, the search cache and the engine state.

**Audit**
- Duplicates are detected across ISIN, name and symbol, and across types, within 3 years.
- `label_as_company_name` is an error.
- New warnings: `order_after_effective`, `source_conflicts`, `review_flags`.
- `--out` lets read-only checks leave the tracked report alone.

### Operational resilience (commit `c8d0480`)

- **Failure classes:**
  - 400, 401, 404 and 410 → dead.
  - 403, 429 and soft blocks → refusals, retried with long backoff (2 h, 8 h, then daily; at most 10 attempts).
  - Network errors and 5xx → normal backoff (4 attempts).
  - Too large → dead.
- **Domain blocks:**
  - Only refusals count toward a block.
  - Blocks expire after 6 h, then 24 h, then 72 h, and a success clears the block.
  - Timeouts never block a domain.
  - Network errors on 2 or more domains in a row stop the run with `network_unavailable`, and the attempts are given back.
- **Docket:**
  - Candidates are selected per domain, so one slow or over-budget domain can't hide the others or end the run early.
  - Filing a URL again keeps its company tag, evidence and priority.
- **Downloads:**
  - They are streamed with a size cap (`max_download_mb`, default 40).
  - An HTML page served for a document URL, or a small WAF page, is treated as a soft block, not a document.
- **Disk:**
  - The process worker checks disk space before each batch.
  - Stale `.part` and `.tmp` files are cleaned up.
  - A non-numeric disk setting falls back to the default.
- **Search:**
  - A truncated cache file counts as a miss.
  - Falling back to the next provider after an empty answer is **available but off by default**. An existing test deliberately protects your paid Serper and Brave credits from free-provider misses, so this is your decision (§9).

## 4. Regression tests

There are four new files, 214 tests in total. Each was run against the unfixed code first:

| File | Before fix | After fix |
|---|---|---|
| `tests/test_security_regressions.py` | 75 failed / 14 passed | 89 passed |
| `tests/test_execution_reliability.py` | 12 failed / 1 passed | 14 passed |
| `tests/test_data_integrity.py` | 50 failed / 27 passed | 82 passed (77 + 5 added after the dataset preview, §8) |
| `tests/test_operational_resilience.py` | 15 failed, 6 errors / 8 passed | 29 passed |

The tests that passed before the fix cover behaviour that must keep working, such as legitimate agent commands, distinct events and existing guards.

Existing tests that changed, all tightening rather than weakening:
- **`tests/test_tool_guard.py`:** 2 assertions said the agent *may* edit `config/` directly. They now assert refusal.
- **`tests/test_parse.py`:** a test passed a literal `\n` and so never reached the guard it claimed to test. It now uses a real newline and asserts the matter line matches first.
- **`tests/test_audit_and_plan.py`:** the issue-lock file is redirected, so tests no longer write the real `logs/`.

No existing test was deleted or relaxed.

## 5. Test execution evidence (`remediation/evidence/`)

| Run | Result |
|---|---|
| Full suite `pytest -q tests` | **350 passed** (136 before remediation) |
| `pyflakes scripts tests`, `bash -n` (including the Cline hook) | clean |
| `check.sh --offline` (`05_check_offline.txt`) | all code checks ok. **Dataset audit FAILS on the unregenerated production master:** 4 `label_as_company_name` errors, and 95 records in possible-duplicate groups (the old audit reported 4). This is the intended signal until regeneration. |
| Claude's adversarial suite, unmodified, fresh sandbox (`00_…` → `06_…`) | **57 failed → 8 failed** (101 tests). See below for the 8. |
| Claude's offline integration harness (`07_integration_after_remediation.out`) | Every scenario now behaves correctly. Dates are correct (Poddar 2008-03-04, Augusta Mills 2008-08-21, Bhagyanagar 2017-08-15). Amalgamated Bean Coffee is COMPULSORY. The HTTP-date Retry-After no longer crashes anything, and the item is retried. A SIGKILL counts as a crash, and the item is retried. Graceful stop works, with each worker's exit code reported. The network outage blocks **no** domain (before: `nseindia.com` blocked permanently). Docket `integrity_check` is ok. |
| End-to-end hook test | A payload goes through `.cline/hooks/PreToolUse.sh` and bash runs the rewritten command. The allowed command produced correct output, and both bypass attempts were replaced with "BLOCKED" messages. |

The 8 remaining adversarial failures:
- **6 encode the pre-fix behaviour as correct:**
  - sleeping after the final attempt (`[4,8,16,32]` ×3, `[120]*4`);
  - losing the last network error;
  - `done_fetch` reporting `fetched` for an unknown URL.
- **1 is a design choice.** A 20,000-row sheet is refused as a parse error instead of being truncated to 5,000 rows.
- **1 is a harness limitation.** The `record_id` test uses two *different* output folders, so the registry can't be shared between runs. The equivalent test in `tests/` uses one folder and passes.

## 6. Security review

| Control | Status |
|---|---|
| Reading credential files (`.env*`, `~/.config/.../keys.env`) | `read_files` is blocked. Every shell route found by the audits and by me is closed: filter operands, `$HOME`, `~`, globs. Allowed scripts that take paths (`inspect_artifact.py`) are limited to `data/`, by both the guard and the script. |
| Writing or modifying source, config and protected files | No editor or patch tool is allowed. No redirections. Filters cannot write (`sort -o` is refused, and the filters are Python anyway). |
| Overriding user decisions | Caps can only be tightened. No docket management (`--unblock/--retry/--promote`), no closing issues, no abbreviated flags. |
| Shell injection | None possible through the runner: argv execution with no shell. If Cline ignored the rewrite, bash would run the original string, which the scanner has already proven contains nothing bash would expand. |
| Fail-closed | Garbage hook input → blocked (tested). Exceptions in the guard → blocked. |
| **Residual risks** | (a) It is **not verified in a live Cline session** that Cline applies `overrideInput` to *allowed* calls; the mechanism is the same one already used for blocked calls, and layer 2 makes the original string safe anyway. (b) What Cline does if the hook process itself fails (e.g. `.venv` missing) is untested. (c) Everything runs as your user, so the key file is protected by policy, not by the OS. An OS sandbox (`sandbox-exec` profile, or a separate macOS user) would be the real boundary. (d) Text from downloaded documents and search results remains a prompt-injection channel. The guard limits what an injected instruction can *do*, not what the model *reads*. |

## 7. Data-integrity review

- **Fixed in code, regression-tested and confirmed end to end:** dates, false entities, type classification, entity matching with and without ISINs, duplicate reconciliation that keeps distinct events, confidence propagation of OCR fields, atomic outputs, and the audit.
- **Not yet applied to the production dataset.** The tracked master still has the 4 `ISIN` rows, the 36 + 3 duplicate pairs, and 34 out-of-scope years. See §8 for what a regeneration would change.
- **Design choices you should confirm:**
  - The 30/7/270-day windows.
  - The 3-year audit window.
  - Out-of-scope years move to a separate file rather than being dropped.
  - A link from a delisting page is no longer enough on its own: the document must say so.
  - A `NEEDS_REVIEW` field borrowed into a `VERIFIED_PRIMARY` record keeps the record's confidence but is flagged per field.

## 8. Dataset impact preview (isolated copy; nothing in the repo was changed)

The new parser and merge were run on a **copy** of the production `data/raw/` (568 files) in a scratch sandbox. Production files were only read. Evidence: `evidence/08_dataset_preview.txt`.

| | Production master today | After a regeneration (preview) |
|---|---|---|
| Records in master | 491 | **413**, plus 34 moved to `out_of_scope_delistings.csv` (before 2006) and 1 more in `bse_only` |
| Dataset audit | (new audit) 4 errors | **0 errors**; 12 possible-duplicate warnings, 52 records with `source_conflicts`, 21 with `review_flags` |
| Garbage names (`ISIN`, `Symbol: IFBIND \| ISIN`, …) | 4 | 0. "ISIN of BHAGYANAGAR…" and "faites4 fra ISIN" now carry the real company names; no real company was lost. |
| Duplicate company groups | 48 | **7**: Husys (two genuine events) plus the 6 ambiguous groups from RECONCILIATION §1 (B S Appliances, Maars, Indus Fila, Jenson Nicholson, Vital Comm., Autoriders), left for a person |
| Type corrections | | 16 COMPULSORY → OTHER (15 "Exit from ITP platform" rows and Lakshmi Vilas Bank's gazette amalgamation, all wrongly COMPULSORY before); 5 OTHER → COMPULSORY (the source sheet says "Compulsory Delisting") |

The preview caught three issues that unit tests on synthetic data had missed. Each was fixed with a regression test in the same commit:
- **Unclassifiable reason cells fell back to a whole-workbook guess.** A reason like "Exit from ITP platform" made Husys 2016 VOLUNTARY. Such rows are now OTHER.
- **Sheets without a type column took their type from column headers.** The header "Exit Price" made Bhor Industries an exit offer. A sheet's own title lines and data now decide, not its column names.
- **Two pairing and routing rules were too strict or misrouted a record:**
  - An order dated 2 days after its effective date split Poddar Housing into two records. Up to 30 days of overlap is now tolerated.
  - A BSE-only event of an NSE-listed company landed in the master. It now goes to the BSE-only file.

This is a preview, not the regeneration. The repo's `data/processed` is unchanged.

## 9. Remaining unresolved risks

1. **The production dataset is stale.** It still holds the defects in §7 until you approve a regeneration (parse `--force` + merge), ideally after reviewing §8.
2. **No live verification.** Cline's handling of rewritten allowed calls, and of hook failures, has not been tested live (§6). Do one supervised short session before any unattended run.
3. **Same-user execution.** The key file is protected by policy only (§6c).
4. **Search fallback is off by default** (§3). Turn it on (`search.fallback_on_empty: true`) if you accept spending paid credits on queries the free provider answers with nothing.
5. **Network outage on a single domain.** If only one domain is queued, an outage looks like that site failing. Each item uses one of its 4 attempts, and nothing is blocked; network-unavailable detection needs errors on 2 or more domains.
6. **D17 not done.** Circular tables without a date column don't get the `w.e.f.` date from the document header.
7. **S9 not done.** `run_agent.sh` still decides whether to restart by grepping free model text.
8. **Accepted.** O12 (a kill between download and docket commit leads to one extra download), and O13 (two parallel fetch workers would not share budgets; `job.py` allows only one job).
9. **Classification and windows are heuristics.** A new kind of source document can still be mis-typed or mis-paired. `source_conflicts`, `review_flags` and the audit make such cases visible; they don't make them impossible.
10. **The search-ban problem remains:** most SearXNG engines are banned from this IP. Search capacity still depends on adding a Serper or Brave key.

## 10. Release readiness

**Not ready for unattended production collection.** The code-level blockers both audits raised are fixed and covered by tests. Release still needs, in order:
1. Your review of the reconciliation choices (§7) and the preview (§8).
2. An approved regeneration in a branch.
3. One supervised Cline session confirming the hook rewrite live.
4. A decision on the OS-level key isolation and the search fallback.

Passing tests show that the defects found are fixed. They do not show that no others exist.

## Commit history (`35df260..remediation/qa-phase1`)

```
beb3fc1 Data integrity follow-up from the dataset preview: reason-cell and sheet-level types, order/effective tolerance, BSE-only routing
014115d Tooling: read-only check.sh audit, isolated issue lock in tests, a parser test that tests what it claims
c8d0480 Operational resilience: expiring domain blocks, failure classes, outages, starvation, size cap, soft blocks (O1-O8)
98aca2a Data integrity: dates, false entities, type classification, entity/event reconciliation, atomic outputs, audit (D1-D16, O10, O11)
4673bb4 Execution reliability: worker crashes fail the run, poison items die, Retry-After dates (S5-S8, O9)
2bfacea Security: allowlist-grammar tool guard with shell-free execution (S1-S4)
88a5533 Remediation phase 0: import QA adversarial suite (unmodified) and the audit reconciliation matrix
(next) Remediation report and evidence

Code and docs diff (excluding qa/ and remediation/): 32 files changed, 2853 insertions(+), 417 deletions(-)
```
