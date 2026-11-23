# Release Candidate Report — `remediation/qa-phase1`

Baseline `35df260` (`main`, untouched). Phase 1 ended at `6e7c9fd`; this phase adds the commits in §9.
Production checkout, production dataset, raw archive, manifests and both audit worktrees were not modified (proof in §6).
Nothing was pushed or merged. No live collection was run. Every commit has a single author.

**Verdict (§10): a release candidate for independent review. Not production-ready.**

---

## 1. Outstanding issues resolved

| Item | What was wrong | What changed | Regression tests (failed before the fix) |
|---|---|---|---|
| **AGY: DFM Foods / ADHARSHILA** (`priority_2017/6ddf19798b18ad47.pdf`) | The document is DFM Foods' post-offer public announcement (voluntary delisting, 2023). It includes newspaper pages where multiple columns were OCR'd into one line. One merged line, `faites4 fra ISIN: IN9509C01016 (partly paid-up equity shares). ADHARSHILA CAPITAL SERVICES LIMITED`, is part of an unrelated call-money corrigendum for partly-paid shares. The free-text extractor accepted any text before an ISIN as a name, and took the first capitalised word after it as the symbol. | Names read from free text must look like a company (not OCR garbage, not a field label, not a private company). The symbol comes only from a `Symbol:` label or the single token right after the ISIN, never from a word of the name and never from the start of a following company name. A refused candidate goes to the new **`extraction_review.csv`** with the reason and the source line, so it is never silently dropped and never merged. **ADHARSHILA is not attributed to anything:** the line does not establish which company owns IN9509C01016. The document's real subject, DFM Foods (VOLUNTARY_EXIT_OFFER, 2023-04-05), is already in the dataset from NSE's Regulation 27 report. | `test_rc_false_entities.py` uses the exact extracted text (10 of 12 tests failed before) |
| Same class, found by scanning every name in the preview | (1) `BHAGYANAGAR` was taken as Bhagyanagar's symbol, but it is the first word of the name; the real symbol is BHAGYAPROP. (2) Visesh Infotecnics got the symbol `CDSL` from "Central Depository Services (India) Limited (CDSL)", which kept a BSE-only company in the NSE dataset. (3) An NCLT cause title, "In the matter of Supreme Hydro Engineering **Private** Limited ……Operational Creditor", became a "delisting". (4) Two BSE notices that *reverse* a delisting ("changed from 'Delisted' to 'Suspended'") produced delisting records. (5) Nine BSE **REDEMPTION** rows (bonds, NCDs, an InvIT) were typed AMALGAMATION_MERGER. (6) An unmapped PDF glyph appeared in a name (`Stephano(cid:415)s Finance Ltd`). | (1)–(2): no symbol from a word of the name or from an institution's abbreviation, and an order's symbol must sit next to the company's own name. (3): court roles and private companies go to review. (4): reversal notices make no records (sent to review). (5): debt-redemption reasons are not equity delistings (sent to review). (6): the name is kept and flagged `name_has_unmapped_pdf_glyph`. | same file + `test_rc_header_dates.py::test_reversal…` |
| **A. Cline guard integration** | It had never been verified that Cline actually runs the rewritten command. | Verified with the **real Cline CLI 3.0.75 and the real local model** (§5). One cosmetic guard bug was found and fixed: an empty argument `''` was refused with a misleading "redirection" reason. | `test_security_regressions.py::test_empty_argument…` |
| **B. OS-level credential isolation** | The agent ran as your user. Keys, SSH and home were protected by application policy only. | `run_agent.sh` now runs Cline inside a macOS `sandbox-exec` profile (`scripts/agent_sandbox.py`), on by default on macOS. Home is unreadable and unwritable except for the project, Cline's state and the runtimes. `~/.ssh`, `~/.config/nse-delisted-comp`, the project `.env`, `.git/config` and the keychain service are denied explicitly. The profile is probed before every session and the session refuses to start if a probe leaks. Nothing on your account, permissions or keychain was changed. Verified with real Cline sessions (§4, §5). | `test_rc_agent_sandbox.py` (11), plus a sandboxed end-to-end test in `test_rc_agent_supervisor.py` |
| **C. Single-domain network outage** | With one domain queued, an outage looked like that site failing, and each item lost one of its 4 attempts. | When a download gets no HTTP response, the worker probes connectivity: a TCP connect with no data, to `network_probe_hosts` (public resolvers, new in the config) or to the configured HTTP(S) proxy. Nothing answers → our own outage: the item is handed back with **no attempt counted**, no domain is penalised, and the run stops with `network_unavailable`. The probe answers → a genuine URL failure, counted as before. | `test_rc_network_outage.py` (all 9 failed before). Includes 20 runs during an outage leaving the item at 0 attempts. Integration scenario 5: the four outage items stayed `pending` with 0 attempts. |
| **D. Header dates** | Circulars and notices whose table has no date column produced undated records. | The effective date is read from the document's own "delisted / withdrawn … w.e.f. / with effect from &lt;date&gt;" sentence, only when none of the table's rows has a date. A suspension date is never used, and neither is a reference date ("circular dated …"). Two different delisting dates → no date, and every row is flagged `header_date_ambiguous:<d1>|<d2>`. A new column, **`date_source`** (`table_column` / `document_text` / `line_text` / `document_header`), records where each date came from. A broken "ff" ligature (`e(cid:431)ect`) is repaired only inside this date scan. | `test_rc_header_dates.py` (14 failed before) |
| **E. Agent restart logic** | `run_agent.sh` searched the session transcript for strings, so model text containing `stop_reason: search_engines_suspended` made it wait 31 minutes, and a structured `low_disk_space` stop was missed. | `scripts/agent_supervisor.py` decides from structured state only: Cline's exit status, the `finishReason` field of Cline's own `run_result` event (never its `text`), and the `stop_reason` of this session's `logs/runs/*.json`. Also fixed: under `pipefail`, the final `status_report \| head` could replace the script's exit code. | `test_rc_agent_supervisor.py`: unit tests, plus 9 end-to-end tests driving the real `run_agent.sh` with a scripted Cline stand-in. They cover completion, restart-then-completion, giving up, model-text bait, low disk, the cooldown limit, Ctrl-C, sandbox on, and sandbox control. Before-fix run: `12_…` (the wording-only failures are marked as such). |
| **Staging findings** | **First Leasing Company of India:** a BSE notice's own delisting date (2018-05-11) out-ranked NSE's rows and split NSE's event (letter: COMPULSORY, 2018-05-30) in two. "X **of** India" and "X India" were also treated as different companies. | NSE sources now anchor an event; a BSE date never re-dates or splits it and is recorded in `source_conflicts`. `OF` is ignored in names; in this archive that affects only First Leasing. | `test_first_leasing…` (failed before: 2 records) |
| **QA harness execution** | AGY's run stopped because `QA_SANDBOX` was unset. | Run properly with `qa/make_sandbox.sh` (§3). | — |
| **AGY spot checks** | — | Kept as named regression tests: ISO and named-month dates, implausible OCR dates rejected, plausible low-confidence dates flagged, ISIN and no-ISIN rows merged keeping the valid ISIN. | `test_rc_agy_spotchecks.py` (19) |

## 2. Remaining limitations

1. **The production dataset is not regenerated.** `main` still holds the 491-record master with the known defects. The staging output (§6) is the proposed replacement and needs your review and approval.
2. **The OS sandbox is macOS-only** and uses `sandbox-exec`, which Apple marks deprecated but still enforces. On other systems `run_agent.sh` warns, and the tool guard is the only boundary. Outbound network stays open for agent sessions, because the pipeline downloads from the web.
3. **Paid search keys are unreadable inside the agent sandbox**, so agent sessions search through SearXNG only. Most SearXNG engines are banned from this IP. `CLINE_SANDBOX_ALLOW_KEYS=1` re-allows the keys file, at the cost that a guard bypass could read it too. A key broker outside the sandbox would solve this properly; it is not built. Your direct `collect.py` runs are not sandboxed and use keys as before.
4. **Cline's tools run in the process that started them.** The sandboxed sessions prove this for the hub run_agent.sh starts. `run_agent.sh` kills a stale hub for this project first; a hub started by hand outside the sandbox and reused would bypass the OS layer.
5. **The connectivity probe adds outbound TCP connects** to 1.1.1.1, 8.8.8.8 and 9.9.9.9 on port 443 (no data sent), only after a failed download. Remove `network_probe_hosts` to disable it; the old 2-domain rule then applies.
6. **Classification, merge windows and name rules are heuristics.** 12 possible-duplicate warnings remain (the 6 ambiguous company pairs from phase 1: B S Appliances, Maars, Indus Fila, Jenson Nicholson, Vital Communications, Autoriders). They are flagged, not merged. 53 records carry `source_conflicts` and 21 carry `review_flags`.
7. **2 master records have no date:** Parsoli Corporation (an order without a decision date) and Suumaya Industries (an order without an effective date). Both are NEEDS_REVIEW, and no date was invented.
8. **8 adversarial QA tests still fail** (§3), as explained in phase 1.
9. **Accepted from phase 1:** O12 (a kill between download and docket commit causes one extra download) and O13 (parallel fetch workers would not share budgets; `job.py` allows one job).
10. **Search fallback** to paid providers stays off by default (`fallback_on_empty`). This is your decision.

## 3. Test results

| Suite | Result | Evidence |
|---|---|---|
| Project tests `pytest tests` | **442 passed** (350 at the end of phase 1, 136 at baseline). pyflakes clean. | `17_check_offline_rc.txt` |
| New RC tests, run against the pre-fix code | false entities 10/12 failed, outage 9/9, header dates 14/14, run_agent e2e 6/7 (3 behavioural, 3 wording; see file), First Leasing 1/1 | `09_`–`12_` |
| **QA adversarial harness `pytest qa/`**, properly sandboxed (`qa/make_sandbox.sh`, `QA_SANDBOX` set, qa/ unmodified) | **101 tests run: 93 passed, 8 failed.** Baseline was 57 failed. Of the 8: 6 assert the pre-fix behaviour as correct (sleeping after the last retry ×5, losing the last network error, `fetched` for an unknown URL); 1 is a design choice (a 20,000-row sheet is refused, not truncated); 1 is a harness limitation (two output folders cannot share the id registry). | `13_qa_harness_full_run.txt` |
| Offline integration harness `qa/run_integration.sh` | All scenarios pass. New: the outage scenario now stops with `network_unavailable` through the proxy probe, with no domain blocked and the four items at 0 attempts. Docket `integrity_check` is ok. | `14_integration_rc.out` |
| `scripts/check.sh --offline` | pyflakes, bash syntax, JSON configs, pytest, disk, tesseract and stale references are all ok. **The dataset audit FAILS on the unregenerated production master** (4 label-as-name errors, 97 possible duplicates). This is intended until regeneration. | `17_check_offline_rc.txt` |
| Dataset audit on the **staging** output | **0 errors**, 90 warnings (12 possible duplicates, 53 conflicts, 21 review flags, 2 undated, 1 order-after-effective). | `staging_rc/dataset_audit.json` |

## 4. Security and command-guard verification

- **Layer 1, tool guard:** an allowlist grammar, with shell-free execution through `agent_exec.py` (phase 1). Verified live in §5.
- **Layer 2, OS sandbox:** `agent_sandbox.py --check` runs real processes inside the profile:
  - Allowed: project read and write, venv Python with pandas.
  - Denied: listing home, `~/.ssh`, `~/.zshrc`, the keys file, the project `.env`, writing to home, `security list-keychains`.
  - Tests use a throwaway HOME, so nothing in your real home is read.
- **Layers 1 and 2 are independent:** with the hook *removed*, real Cline still could not read outside the project (§5b).
- **Restart logic** can no longer be steered by model output (§1E).
- **Credentials used during this work:** none. Every Cline run used a throwaway HOME or a sandboxed real HOME, a canary `.env`, and search pointed at a dead port or a local stub. Canary strings appeared in **0** session outputs.

## 5. Cline smoke-test results (real Cline 3.0.75, LM Studio `qwen/qwen3.8-27b`)

**(a) Guard, 16 scripted steps** in a disposable repo copy, with isolated Cline config, a throwaway HOME and localhost-only network (`15_cline_real_smoke_test.txt`).
- **Allowed calls:** `status_report.py | head -5` and `git status`. Cline executed `.venv/bin/python scripts/agent_exec.py <plan>`, **the guard's rewrite, not the original string**, and returned the correct output.
- **Denied calls (14):** `$HOME`, `~/…`, `ls *`, `> /tmp/…`, `>> scripts/common.py`, `sed -i` on config, `python -c`, `curl`, `job.py --max-queries 999`, an unknown subcommand, `read_files .env`, `read_files scripts/tool_guard.py`, editor on config, editor creating `scripts/backdoor.py`. Cline executed the harmless replacement each time: a `printf` of the reason, the `BLOCKED_READ.txt` notice, or an editor call on an impossible `/dev/null/` path.
- **Afterwards:** no source or config change, no `backdoor.py`, no redirect file, 0 canaries in the output. `finishReason: completed`.

**(b) OS sandbox, through the real `scripts/run_agent.sh`** with `CLINE_SANDBOX=1` and the real HOME (`16_cline_real_os_sandbox.txt`).
- **Hooked copy:** works normally inside the sandbox. The guard refused a `--help` and `agent_exec` ran the status report. Completed, exit 0.
- **Hook removed:**
  - Denied by the OS with *Operation not permitted*: `cat` of a canary outside the project, `ls ~/.ssh`, `cat .env`, `touch` outside the project, `security list-keychains`, and `read_files` of the canary.
  - Still allowed: reading `package.json` inside the project.
  - 0 canaries in the output. The temporary canary file was deleted afterwards.

## 6. Staging regeneration (`remediation/staging_rc/`)

- **Build:** an isolated copy of the raw archive (568 data files) and the manifest; code `fede71d`; `parse --force` → merge → audit, run under a localhost-only network sandbox.
- **Production untouched:** sha256 of 584 production files (every raw file, the master CSV/JSONL, the manifest, the metadata) is **identical before and after** (`production_hashes_{before,after}.txt`). `main` is clean at `35df260`. `qa/claude` (2360aa9) and `qa/agy` (35df260) are clean.

| | Production `35df260` | Staging |
|---|---|---|
| Master records | 491 | **409** |
| Pre-2006 (`out_of_scope_delistings.csv`) | in the master | 34 (2002: 4, 2003: 7, 2004: 17, 2005: 6) |
| BSE-only file | 115 | 107 |
| Audit errors | 4 (+ 97 possible duplicates) | **0** (12 possible duplicates, all flagged ambiguous pairs) |
| Types | COMP 193, VOL_NO_EXIT 129, IBC 122, OTHER 42, VEO 4, AMALG 1 | COMP 156, VOL_NO_EXIT 106, IBC 121, OTHER 23, VEO 3 |
| Confidence | — | VERIFIED_PRIMARY 407, NEEDS_REVIEW 2 |

What was inspected:
- **Every extracted name:** one suspicious name remains (`Stephano(cid:415)s Finance Ltd`, BSE-only, flagged).
- **Every NEEDS_REVIEW record:** 2, both undated orders (§2.7).
- **Every merged group:** 42 groups from 84 production records. **None joins more than two production records, and none joins effective dates more than 7 days apart.** They are the delisted.xlsx / Buy_Sell.xls duplicates (one day apart) and order + effective-date pairs.
- **Every split:** 4 — Husys (two genuine events), and BIL Industries and Omnitech, whose BSE delistings are now separate BSE-only events on different dates.
- **Every removal:** 16 (§7).
- **Every type change:** 61 records. 34 OTHER → COMPULSORY (Buy_Sell duplicates taking the typed source's type), 16 COMPULSORY → OTHER (the 15 ITP exits and Lakshmi Vilas Bank), 8 COMPULSORY → IBC_NCLT (orders merged with their IBC effective dates), 2 VOLUNTARY_NO_EXIT → IBC_NCLT (Pradip Overseas, Rohit Ferro-Tech, with the conflict recorded), and 1 OTHER → IBC_NCLT.
- **Every date change:** 29 date "corrections" are duplicate pairs keeping the primary list's date (delisted.xlsx, usually one day before Buy_Sell.xls), with the other date kept in `source_conflicts`; they are not new facts. 132 dates were added: mostly order dates joining effective dates, BSE notices getting their header date, and 4 master records dated from their circular's header (Celestial Biolabs and JIK 2024-10-17, Bhalchandram 2025-11-03, Future Supply Chain 2026-03-24). Each of these carries `date_source=document_header`.
- **`extraction_review.csv`:** 26 candidates — 11 debt redemptions, 11 sentence fragments, 2 reversal notices, ADHARSHILA, and Supreme Hydro.

## 7. Record-level reconciliation (`remediation/reconciliation/`)

`record_reconciliation.csv` has one row per production record (491 master + 115 BSE-only, **all 606 accounted for**) plus one per added record. Columns: old fields, `outcomes`, new record id(s), new file, new fields, `date_source`, flags, conflicts, field-by-field `changes`, and notes. Regenerate it with `remediation/tools/reconcile_master.py`.

| Outcome | Records |
|---|---|
| unchanged | 346 |
| merged (84 production records → 42 staging records) | 84 |
| date added / date corrected | 132 / 29 |
| reclassified | 61 |
| ids changed (ISIN/symbol filled or corrected) / renamed | 35 / 5 |
| moved to out_of_scope (pre-2006) / to bse_only | 34 / 1 (Visesh Infotecnics) |
| split | 4 |
| **removed as false entities** | **16**: `ISIN` ×2, `Symbol: IFBIND \| ISIN`, `faites4 fra ISIN`, Supreme Hydro (NCLT cause title), 2 reversal notices (India Infraspace, Birla Transasia Carpets), 9 BSE debt redemptions |
| removed for no known reason | **0** |
| added (in BSE-only) | 2 (BIL Industries' and Omnitech's separate BSE events) |

## 8. Target-year coverage (staging master)

| Year | Production (calendar_year) | Staging, by effective year | Staging, priority incl. orders that year | Types (staging, effective year) |
|---|---|---|---|---|
| 2007 | 7 | 7 | 7 | VOLUNTARY_NO_EXIT 7 |
| 2017 | 77 | 77 | 77 | COMPULSORY 56, IBC 11, VOL_NO_EXIT 5, OTHER 5 |
| 2020 | 32 | 24 | 36 | IBC 9, VOL_NO_EXIT 9, COMPULSORY 4, OTHER 2 |
| 2024 | 2 | 4 | 4 | COMPULSORY 4 (Celestial Biolabs and JIK newly dated from their circular) |

None of the 2020 records were lost. Production gave 8 records `calendar_year` 2020 from their 2020-11-27 order although they were delisted on 2021-02-24. Staging uses the effective year and still counts them as priority-2020 through the order date. **Coverage of 2007 and 2024 is thin.** That is a collection gap that needs live collection, not a code defect.

## 9. Commits and diff

Phase 1 (`35df260..6e7c9fd`): `88a5533 2bfacea 4673bb4 98aca2a c8d0480 014115d beb3fc1 6e7c9fd`.

This phase:
```
d47194f False entities: OCR-garbage and trailing names from free text go to extraction_review.csv; symbols never from the name or an institution
8d6df0e Network outages: a connectivity probe tells our outage from a failed URL, even with one domain queued
dbf54c0 Header dates: circulars without a date column take the delisting date stated in their text; delisting-reversal notices make no records
659b6bd Agent restarts decided from structured state, never model text; exit code no longer lost to the status report
1c160e9 OS sandbox for agent sessions (macOS sandbox-exec)
a917691 AGY spot checks as named regression tests; full QA harness run
bd75c93 Tool guard: an empty argument is not an operator; evidence: real Cline + LM Studio smoke test, integration harness
5846dc9 Evidence: real Cline sessions under the OS sandbox via run_agent.sh
60df957 Merge: NSE sources anchor an event; 'X of India' = 'X India' (staging finding: First Leasing)
fede71d BSE debt redemptions are not equity delistings; unmapped PDF glyphs in names flagged; record-level reconciliation tool
143683c Staging regeneration snapshot, production hash proof, record-level reconciliation
(next)  Release-candidate report and final evidence
```

Code, config, docs and tests:
- `6e7c9fd..HEAD`: 17 files, +1543 / −72.
- `35df260..HEAD` in total: 42 files, +4382 / −475 (excluding `qa/` and `remediation/`).

New files: `scripts/agent_sandbox.py`, `scripts/agent_supervisor.py`, and six `tests/test_rc_*.py`.

Changed this phase: `parse_nse_records.py`, `normalize_and_merge.py`, `fetch_worker.py`, `run_agent.sh`, `tool_guard.py`, `audit_dataset.py`, `config/query_plan.json` (`network_probe_hosts`), and `README.md`. The full diff is `git diff 35df260 remediation/qa-phase1`.

## 10. Release-readiness assessment

**Ready for independent review as a release candidate. Not production-ready.**

Done and verified:
- Every item in your brief is resolved with tests that failed before the fix.
- The guard and the OS sandbox are both proven with the real Cline and the real model.
- The staging dataset audits clean, and every production record is traced.

Before production use, in this order:
1. **Review `record_reconciliation.csv`**, mainly the 16 removals, the 61 type changes and the 42 merges, and `extraction_review.csv`.
2. **Approve replacing the production dataset** with the staging output, as a separate commit on a branch. It was not done here.
3. **Decide on search under the sandbox:** SearXNG-only, `CLINE_SANDBOX_ALLOW_KEYS=1`, or a key broker (not built).
4. **One supervised live collection session** with the sandbox on, watching `logs/tool_guard.jsonl` and the run summaries. Live collection has not been exercised since these changes.
5. **Then push and merge.**

Passing tests show that the defects found are fixed. They do not show that no others exist. This phase alone found four new defect classes (First Leasing, debt redemptions, reversal notices, symbol leakage) only by reviewing real output.
