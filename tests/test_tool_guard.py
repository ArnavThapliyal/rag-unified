import json

import tool_guard as g

ROOT = str(g.ROOT)


def run(cmd):
    return g.check("run_commands", {"commands": [cmd]})


def test_agent_commands_are_allowed():
    assert run(f"cd {ROOT} && .venv/bin/python scripts/collect.py --dry-run") is None
    assert run(".venv/bin/python scripts/job.py wait 3") is None
    assert run(".venv/bin/python scripts/audit_dataset.py 2>&1 | tail -40") is None
    assert run('.venv/bin/python scripts/common.py log "cycle 2: done; records 590 (+3)"') is None
    assert run(".venv/bin/python scripts/status_report.py | grep -i engines") is None
    assert run("git status --short") is None


def test_everything_else_needs_approval():
    for cmd in ["curl http://x", "rm -rf data", '.venv/bin/python -c "print(1)"', "ls -la data",
                ".venv/bin/python scripts/collect.py --max-queries 5",
                ".venv/bin/python scripts/status_report.py\nrm -rf data",
                ".venv/bin/python scripts/status_report.py > out.txt",
                ".venv/bin/python scripts/status_report.py $(rm x)",
                ".venv/bin/python scripts/status_report.py | grep x /etc/passwd",
                ".venv/bin/python scripts/parse_nse_records.py --force",
                ".venv/bin/python scripts/report_issue.py --close 3",
                ".venv/bin/python scripts/job.py start --max-queries 8 &", "cd /tmp && git status"]:
        assert run(cmd), cmd


def test_reads_and_edits():
    assert g.check("read_files", {"files": [{"path": f"{ROOT}/config/query_plan.json"}]}) is None
    for p in ["scripts/collect.py", "/etc/passwd", "data/raw/master_lists/x.pdf", "~/.cline/data/x"]:
        assert g.check("read_files", {"files": [{"path": p}]}), p
    assert g.check("editor", {"path": f"{ROOT}/config/query_plan.json", "new_text": "x"}) is None
    assert g.check("editor", {"path": "config/../scripts/x.py", "new_text": "x"})
    assert g.check("apply_patch", {"input": "*** Begin Patch\n*** Update File: soul.md\n*** End Patch"})
    assert g.check("fetch_web_content", {"url": "https://x"})
    assert g.check("start_background_command", {"command": "sleep 1"})


def test_blocked_call_is_rewritten_not_cancelled(monkeypatch, capsys):
    monkeypatch.setattr(g, "record", lambda *a: None)
    monkeypatch.setattr(g, "audit", lambda *a: None)
    payload = {"tool_call": {"name": "run_commands", "input": {"commands": ["curl http://evil"]}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    g.main()
    out = json.loads(capsys.readouterr().out)
    assert "cancel" not in out  # cancel would end the agent's whole run
    assert out["overrideInput"]["commands"][0].startswith("printf ")
    assert "curl" in out["context"] or "not allowed" in out["context"]
