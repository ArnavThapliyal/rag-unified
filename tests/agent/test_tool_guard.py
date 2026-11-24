import json

import tool_guard as g

ROOT = str(g.ROOT)
PLAN = f"{ROOT}/collector/config/query_plan.json"


def run(cmd):
    return g.check("run_commands", {"commands": [cmd]})


def test_agent_commands_are_allowed():
    assert run(f"cd {ROOT} && ./rag collect --dry-run") is None
    assert run("./rag job wait 3") is None
    assert run("./rag audit_dataset 2>&1 | tail -40") is None
    assert run('./rag log "cycle 2: done; records 590 (+3)"') is None
    assert run("./rag status_report | grep -i engines") is None
    assert run(f"{ROOT}/rag status") is None
    assert run("git status --short") is None


def test_everything_else_needs_approval():
    for cmd in ["curl http://x", "rm -rf data", '.venv/bin/python -c "print(1)"', "ls -la data",
                "./rag collect --max-queries 5",
                "./rag status_report\nrm -rf data",
                "./rag status_report > out.txt",
                "./rag status_report $(rm x)",
                "./rag status_report | grep x /etc/passwd",
                "./rag parse_nse_records --force",
                "./rag report_issue --close 3",
                "./rag job start --max-queries 8 &", "cd /tmp && git status",
                # the old direct forms are gone: everything goes through ./rag
                "collector/.venv/bin/python collector/scripts/job.py status",
                ".venv/bin/python scripts/job.py status", "python3 -m unified status", "bash rag status",
                "rag status", "./collector/../rag status", "./rag", "./rag loop", "./rag agent", "./rag doctor",
                "./rag _bg 20261124-101010-processor.ingest", "agent/run_agent.sh"]:
        assert run(cmd), cmd


def test_reads_and_edits():
    assert g.check("read_files", {"files": [{"path": PLAN}]}) is None
    assert g.check("read_files", {"files": [{"path": "collector/data/processed/proposed_delisting_leads.csv"}]}) is None
    for p in ["collector/scripts/collect.py", "/etc/passwd", "collector/data/raw/master_lists/x.pdf", "~/.cline/data/x",
              "processor/pipeline/indexer.py", "processor/data/lancedb/x", "unified/steps.toml", "agent/tool_guard.py",
              "tests/agent/test_tool_guard.py", "collector/data/export/20261124-000000/manifest.json",
              "logs/bg/x.json", ".cline/hooks/PreToolUse.sh", "collector/data/docket.sqlite"]:
        assert g.check("read_files", {"files": [{"path": p}]}), p
    assert g.check("editor", {"path": PLAN, "new_text": "x"})  # no direct edits at all
    assert g.check("editor", {"path": "collector/config/../scripts/x.py", "new_text": "x"})
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


def test_allowed_call_is_rewritten_into_the_runner(monkeypatch, capsys):
    monkeypatch.setattr(g, "audit", lambda *a: None)
    payload = {"tool_call": {"name": "run_commands", "input": {"commands": ["./rag job status"]}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    g.main()
    cmd = json.loads(capsys.readouterr().out)["overrideInput"]["commands"][0]
    assert cmd.startswith(g.RUNNER + " ")


def test_secrets_and_search_settings_are_off_limits():
    for p in [".env", f"{ROOT}/.env.local", "collector/.env", "processor/.env", "~/.config/rag-collector/keys.env"]:
        assert g.check("read_files", {"files": [{"path": p}]}), p
    assert g.check("read_files", {"files": [{"path": "collector/.env.example"}]}) is None
    assert g.check("editor", {"path": PLAN, "old_text": '"daily_limit": 50', "new_text": '"daily_limit": 5000'})
    assert g.check("editor", {"path": PLAN, "new_text": '"providers": ["serper"]'})
    assert g.check("apply_patch", {"input": '*** Begin Patch\n*** Update File: collector/config/query_plan.json\n'
                                            '+ "monthly_limit": null\n*** End Patch'})
    # policy: queries are added only with ./rag add_queries, never by editing the plan
    assert g.check("editor", {"path": PLAN, "old_text": '"queries": [', "new_text": '"queries": ["nse delisting 2019",'})
    assert run("./rag web_search --check") is None
    assert run("./rag web_search --query x")


def test_docket_is_read_only_for_the_agent():
    assert run("./rag docket --stats") is None
    assert run("./rag docket --list failed --limit 5") is None
    for cmd in ["--retry dead", "--unblock nseindia.com", "--promote"]:
        assert run(f"./rag docket {cmd}"), cmd
    assert run("./rag fetch_worker")


def test_blocked_config_script_points_to_add_queries():
    inp = {"commands": [".venv/bin/python - <<'PY'\nimport json; open('config/query_plan.json')\nPY"]}
    reason = g.check("run_commands", inp)
    assert reason and "./rag add_queries" in g.blocked_input("run_commands", inp, reason)["commands"][0]


def test_add_queries_is_allowed():  # was in the collector's test_add_queries.py
    assert run('./rag add_queries --year-tag 2008 --query "SEBI order delisting 2008"') is None


def test_agent_may_not_change_limits_or_focus():  # was in the collector's test_limits_historical.py
    for txt in ['"max_files": 100000', '"mode": "unlimited"', '"years_ago": [1, 2]']:
        assert g.check("editor", {"path": PLAN, "new_text": txt}), txt


def test_inspect_artifact_paths_are_the_collectors():
    assert run("./rag inspect_artifact data/raw/priority_2017/x.pdf") is None   # as the collector prints them
    assert run(f"./rag inspect_artifact {ROOT}/collector/data/raw/priority_2017/x.pdf") is None
    for p in ["collector/data/raw/x.pdf", "../processor/data/x", "/etc/passwd", "config/query_plan.json"]:
        assert run(f"./rag inspect_artifact {p}"), p


# ---------- the unified commands, against unified/steps.toml --------------------------------------------------------
def test_unified_read_only_commands():
    for cmd in ["./rag status", "./rag steps", "./rag runs", "./rag runs 20261124-101010-processor.ingest",
                "./rag step collector.status", "./rag step collector.list", "./rag step collector.audit",
                "./rag step processor.status", "./rag step processor.health", "./rag step processor.list",
                "./rag step processor.quant-bracket --date 2020-06-30 --limit 10",
                "./rag step processor.quant-bracket --schedule --start-date 2019-01-01 --end-date 2020-01-01",
                "./rag status | head -40"]:
        assert run(cmd) is None, cmd


def test_long_steps_only_in_the_background():
    for name in ["processor.ingest", "processor.quant-sync", "processor.quant-zfactor", "collector.universe-sync",
                 "collector.export"]:
        reason = run(f"./rag step {name}")
        assert reason and f"./rag start {name}" in reason, name
        assert run(f"./rag start {name}") is None, name
    assert run("./rag start processor.ingest --next") is None
    assert run("./rag start collector.export --max-files 200") is None
    assert run("./rag start collector.status") is None   # quick steps may also run in the background


def test_user_steps_are_refused():
    for name in ["collector.discover", "collector.fetch", "collector.process", "collector.drain", "collector.collect",
                 "processor.ingest-dir"]:
        for sub in ("step", "start"):
            reason = run(f"./rag {sub} {name}")
            assert reason and "not the agent" in reason, (sub, name)
    assert "./rag job start" in run("./rag start collector.collect")


def test_step_arguments_are_checked():
    for cmd in ["./rag start processor.ingest --batch /etc", "./rag start processor.ingest --next --next",
                "./rag start processor.ingest --next=1", "./rag start collector.export --max-files 501",
                "./rag start collector.export --max-files 0", "./rag start collector.export --max-files",
                "./rag start collector.export --outbox /tmp", "./rag step processor.quant-bracket --date 2020-13-01",
                "./rag step processor.quant-bracket --date yesterday", "./rag step processor.quant-bracket --limit 1000",
                "./rag step processor.status --outbox /tmp", "./rag step collector.status --json",
                "./rag step nope.status", "./rag step processor", "./rag step", "./rag start",
                "./rag runs ../../etc/passwd", "./rag runs a b", "./rag status --verbose", "./rag steps x"]:
        assert run(cmd), cmd


def test_every_registered_step_is_covered():
    from unified import registry
    for name, s in registry.steps().items():
        assert (run(f"./rag start {name}") is None) == (s["access"] != "user"), name
        assert (run(f"./rag step {name}") is None) == (s["access"] == "agent"), name
