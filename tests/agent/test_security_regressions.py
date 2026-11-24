"""Regression tests for the agent permission boundary.

Design under test: the tool guard accepts only an exact grammar (programs, flags, values), refuses anything
bash would expand or redirect, and rewrites every allowed call into `agent/agent_exec.py <token>`, which
re-validates the pipeline and runs it without a shell (pipe filters are implemented in Python, stdin only)."""
import base64
import json
import subprocess
import sys

import pytest

import tool_guard as g

ROOT = str(g.ROOT)
PY = ".venv/bin/python"


def cmd(c):
    return g.check("run_commands", {"commands": [c]})


# ---------- S1: pipe filters may not touch files ---------------------------------------------------------
@pytest.mark.parametrize("c", [
    "./rag log x | cut -c1-200 .env",
    "./rag log x | cut -c1-200 /home/user/.config/rag-collector/keys.env",
    "./rag log x | grep -eKEY .env",
    "./rag log x | grep -r API_KEY",
    "./rag log x | grep -R KEY",
    "./rag log x | grep -f .env",
    "./rag log x | grep --file=.env",
    "./rag log x | sort -oscripts/collect.py",
    "./rag log x | sort -o scripts/collect.py",
    "./rag log x | sort --output=x",
    "./rag log x | sort --files0-from=.env",
    "./rag log x | sort .env",
    "./rag log x | head .env",
    "./rag log x | tail -n 5 .env",
    "./rag log x | wc -l .env",
    "./rag log x | uniq - out.txt",
    "./rag log x | grep KEY .env",
    "./rag log x | grep -e KEY -e TOKEN .env",
])
def test_pipe_filters_cannot_read_or_write_files(c):
    assert cmd(c), c


# ---------- S2: nothing bash would expand -------------------------------------------------------------------
@pytest.mark.parametrize("c", [
    "./rag log $HOME",
    './rag log "$HOME"',
    "./rag log ~/.config/rag-collector/keys.env",
    "./rag inspect_artifact data/raw/*",
    "./rag inspect_artifact data/raw/x?.pdf",
    "./rag inspect_artifact data/raw/{a,b}.pdf",
    "./rag inspect_artifact data/raw/[ab].pdf",
    "./rag log a\\$b",
    './rag log "a`id`"',
    "./rag log !!",
    './rag log "x\nrm -rf data"',
])
def test_shell_expansion_is_refused(c):
    assert cmd(c), c


# ---------- S3: agent scripts only with the agent's own arguments ------------------------------------------
@pytest.mark.parametrize("c", [
    "./rag docket --stats --unblock nseindia.com",
    "./rag docket --list dead --retry dead",
    "./rag docket --stats --promote --limit 100000",
    "./rag docket --list failed --limit 100000",
    "./rag report_issue --close=1",
    "./rag report_issue --clos 1",
    "./rag report_issue --list --close 1",
    "./rag job start --unlimited",
    "./rag job start --max-files all --max-docs-per-company all",
    "./rag job start --max-files 0",
    "./rag job start --max-companies 99999",
    "./rag job start --max-queries 8 --unl",
    "./rag job start --stage fetch --fetch-limit 999",
    "./rag job wait x",
    "./rag collect --dry-run --unlimited --stage all --max-files all extra",
    "./rag web_search --check --query x",
    "./rag status_report --watch 1",
    "./rag log a b",
    "./rag common notify x",
    "./rag add_queries --year-tag 2008 --plan /tmp/x.json",
    "./rag inspect_artifact ../../etc/passwd",
    "./rag agent_exec e30",
    f"{PY} scripts/../scripts/report_issue.py --close 1",
])
def test_script_argument_grammar(c):
    assert cmd(c), c


def test_job_caps_may_only_be_tightened(monkeypatch):
    monkeypatch.setattr(g, "user_caps", lambda: {"mode": "capped", "max_files": 60, "max_companies": 25,
                                                  "max_docs_per_company": 5})
    assert cmd("./rag job start --harvest-only --max-files 40") is None
    assert cmd("./rag job start --harvest-only --max-files 60") is None
    assert cmd("./rag job start --harvest-only --max-files 61")
    monkeypatch.setattr(g, "user_caps", lambda: {"mode": "unlimited", "max_files": 60})
    assert cmd("./rag job start --harvest-only --max-files 500") is None  # user chose unlimited


# ---------- S4: no direct edits at all; the plan changes only through add_queries.py -----------------------
@pytest.mark.parametrize("old,new", [
    ('"min_free_gb": 20', '"min_free_gb": 0'), ('"delay_s": [10, 20]', '"delay_s": [0, 0]'),
    ('"max_per_run": 12', '"max_per_run": 5000'), ('"deny_domains": [', '"deny_domains_x": ['),
    ('"block_domain_after_failures": 3', '"block_domain_after_failures": 9999'), ('"open_web": false', '"open_web": true'),
    ('"queries": [', '"queries": ["nse delisting 2019",'),
])
def test_agent_cannot_edit_config(old, new):
    assert g.check("editor", {"path": f"{ROOT}/collector/config/query_plan.json", "old_text": old, "new_text": new}), new


def test_agent_cannot_patch_or_delete_files():
    for verb in ("Delete File", "Update File", "Add File"):
        assert g.check("apply_patch", {"input": f"*** Begin Patch\n*** {verb}: collector/config/query_plan.json\n*** End Patch"})


# ---------- what the agent legitimately does keeps working ------------------------------------------------
@pytest.mark.parametrize("c", [
    f"cd {ROOT} && ./rag collect --dry-run",
    "./rag collect --dry-run --stage discover --max-queries 999",
    "./rag job start --max-queries 8",
    "./rag job wait 3",
    "./rag job status",
    "./rag audit_dataset 2>&1 | tail -40",
    './rag log "cycle 2: done; records 590 (+3)"',
    "./rag status_report --json",
    "./rag status_report | grep -i engines",
    './rag status_report | grep -E "zero.*rows|audit" | head -5',
    "./rag report_issue --list",
    './rag report_issue --kind zero_rows --summary "3 orders gave 0 rows" --evidence data/raw/a/x.pdf '
    '--evidence data/raw/a/y.pdf',
    "./rag inspect_artifact --zero-rows",
    "./rag inspect_artifact data/raw/master_lists/18fea6d29ab81379.pdf",
    "./rag docket --stats",
    "./rag docket --list failed --limit 5",
    "./rag web_search --check",
    './rag add_queries --year-tag 2008 --query "SEBI order delisting 2008" --query \'"Acme Ltd" delisted\'',
    "./rag add_queries --add-domain capitalmarket.com",
    "./rag build_metadata && ./rag status_report | sort | uniq -c | sort -rn | cut -c1-80",
    "git status --short",
])
def test_agent_commands_still_allowed(c, monkeypatch):
    monkeypatch.setattr(g, "user_caps", lambda: {"mode": "capped", "max_files": 60, "max_companies": 25,
                                                  "max_docs_per_company": 5})
    assert cmd(c) is None, (c, cmd(c))


# ---------- allowed calls are rewritten to the shell-free runner ------------------------------------------
def test_allowed_call_is_rewritten_to_agent_exec(monkeypatch, capsys):
    monkeypatch.setattr(g, "audit", lambda *a: None)
    c = './rag status_report | grep -i "engines" | head -3'
    payload = {"tool_call": {"name": "run_commands", "input": {"commands": [c]}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    g.main()
    out = json.loads(capsys.readouterr().out)
    [new] = out["overrideInput"]["commands"]
    prog, script, token = new.split(" ")
    assert (prog, script) == ("collector/.venv/bin/python", "agent/agent_exec.py")
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    plan = json.loads(base64.urlsafe_b64decode(token))
    assert plan == [{"op": None, "pipeline": [["scripts/status_report.py"], ["grep", "-i", "engines"], ["head", "-3"]]}]


def test_agent_exec_revalidates_a_crafted_token():
    import agent_exec
    evil = base64.urlsafe_b64encode(json.dumps(
        [{"op": None, "pipeline": [["scripts/docket.py", "--unblock", "nseindia.com"]]}]).encode()).decode()
    with pytest.raises(agent_exec.Refused):
        agent_exec.decode(evil)
    evil2 = base64.urlsafe_b64encode(json.dumps([{"op": None, "pipeline": [["/bin/sh", "-c", "id"]]}]).encode()).decode()
    with pytest.raises(agent_exec.Refused):
        agent_exec.decode(evil2)
    for pipeline in (["rag", "loop"], ["rag", "agent"], ["rag", "_bg", "x"], ["rag", "step", "processor.ingest-dir", "/"],
                     ["rag", "start", "processor.ingest", "--batch", "/etc"], ["rag", "step", "processor.ingest"]):
        token = base64.urlsafe_b64encode(json.dumps([{"op": None, "pipeline": [pipeline]}]).encode()).decode()
        with pytest.raises(agent_exec.Refused):
            agent_exec.decode(token)


def test_python_filters_never_open_files():
    import agent_exec as ax
    lines = ["b 2", "a 1", "KEY=1", "a 1", "c 10"]
    assert ax.filter_lines(["grep", "-i", "key"], lines) == (["KEY=1"], 0)
    assert ax.filter_lines(["grep", "-v", "-E", "^a|KEY"], lines) == (["b 2", "c 10"], 0)
    assert ax.filter_lines(["grep", "-c", "a"], lines) == (["2"], 0)
    assert ax.filter_lines(["grep", "zzz"], lines) == ([], 1)
    assert ax.filter_lines(["head", "-2"], lines) == (["b 2", "a 1"], 0)
    assert ax.filter_lines(["tail", "-n", "1"], lines) == (["c 10"], 0)
    assert ax.filter_lines(["sort", "-u"], lines)[0] == ["KEY=1", "a 1", "b 2", "c 10"]
    assert ax.filter_lines(["sort", "-k", "2", "-n", "-r"], lines)[0][0] == "c 10"
    assert ax.filter_lines(["uniq", "-c"], ["a", "a", "b"])[0] == ["      2 a", "      1 b"]
    assert ax.filter_lines(["cut", "-d", " ", "-f", "2"], lines)[0] == ["2", "1", "KEY=1", "1", "10"]  # POSIX: no delimiter -> whole line
    assert ax.filter_lines(["cut", "-c1-3"], ["abcdef"])[0] == ["abc"]
    assert ax.filter_lines(["wc", "-l"], lines)[0] == ["5"]
    assert ax.filter_lines(["grep", "a.1"], lines)[0] == ["a 1", "a 1"]          # BRE '.' still a wildcard
    assert ax.filter_lines(["grep", "a|b"], ["a|b", "a"])[0] == ["a|b"]          # BRE '|' is literal


def test_agent_exec_runs_without_a_shell(tmp_path):
    """End to end: `common.py log` with a message full of shell syntax is passed as one literal argument."""
    token = g.encode_plan([{"op": None, "pipeline": [["git", "status", "--porcelain"], ["wc", "-l"]]}])
    r = subprocess.run([sys.executable, str(g.ROOT / "agent" / "agent_exec.py"), token], capture_output=True,
                       text=True, cwd=g.ROOT)
    assert r.returncode == 0 and r.stdout.strip().isdigit(), r.stderr


# ---------- scripts reject abbreviated options themselves (defence in depth) ------------------------------
@pytest.mark.parametrize("argv", [["scripts/report_issue.py", "--clos", "1"], ["scripts/docket.py", "--unbl", "x"],
                                  ["scripts/collect.py", "--dry-run", "--unl"]])
def test_scripts_refuse_abbreviated_options(argv, tmp_path):
    """Run in a throwaway copy (scripts + config only): on a vulnerable version these commands would act."""
    import shutil
    for d in ("scripts", "config"):
        shutil.copytree(g.COLLECTOR / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("logs/runs", "data/metadata", "data/processed/parsed", "data/raw"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "issues.jsonl").write_text('{"id": 1, "kind": "other", "summary": "x", "status": "open", '
                                                     '"evidence": []}\n')
    r = subprocess.run([sys.executable, *argv], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 2 and "unrecognized arguments" in r.stderr, (r.returncode, r.stdout[-200:], r.stderr[-300:])
    assert '"status": "open"' in (tmp_path / "logs" / "issues.jsonl").read_text()


# ---------- RC: an empty argument is an argument, not an operator (real-Cline smoke test finding) -----------------
def test_empty_argument_gets_the_real_refusal_reason():
    import tool_guard as tg
    with pytest.raises(tg.Bad, match="command not allowed: sed"):
        tg.plan_command("sed -i '' 's/3/99/' collector/config/query_plan.json")
    with pytest.raises(tg.Bad) as e:
        tg.plan_command("git status ''")
    assert "redirection" not in str(e.value)
    with pytest.raises(tg.Bad, match="redirection"):
        tg.plan_command("git status > x")


# ---------- Neon H-3: every "next" / hint command a tool prints is one the guard accepts ------------------
def test_hints_printed_by_the_collector_tools_pass_the_guard():
    import re

    col = g.ROOT / "collector" / "scripts"
    hints = set()
    for script in ("job.py", "status_report.py"):
        hints |= set(re.findall(r"\./rag [a-z_]+ [a-z0-9 \-]+?(?=[\"')`]|\{)", (col / script).read_text()))
    hints = {h.strip() for h in hints} | {"./rag job wait 1", "./rag job wait 7"}
    assert {"./rag docket --stats", "./rag web_search --engines"} <= hints
    for h in sorted(hints):
        if h.endswith("wait"):
            h += " 2"   # f"./rag job wait {poll + 1}"
        assert cmd(h) is None, (h, cmd(h))
