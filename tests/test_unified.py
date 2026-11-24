"""unified/: the step registry matches both repos, steps run with their own interpreter and pass their JSON
through, background runs, and the scheduler's decisions (from step results only)."""
import json
import os
import subprocess
import sys
import time
from argparse import Namespace

import pytest

from unified import bg, loop, registry

ROOT = registry.ROOT
RAG = str(ROOT / "rag")
needs = {repo: pytest.mark.skipif(not registry.python_for(repo).exists(), reason=f"{repo}/.venv not set up")
         for repo in ("collector", "processor")}


def rag(*args, env=None, timeout=300):
    r = subprocess.run([RAG, *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None, r.stderr


# ---- registry ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("repo", ["collector", "processor"])
def test_registry_matches_each_repos_own_step_list(repo):
    if not registry.python_for(repo).exists():
        pytest.skip(f"{repo}/.venv not set up")
    code, res = registry.run(f"{repo}.list", [], stderr=subprocess.DEVNULL, timeout=120)
    assert code == 0, res
    theirs = {s["name"] for s in res["steps"]} | {"list"}
    ours = {n.split(".", 1)[1] for n in registry.steps() if n.startswith(repo + ".")}
    assert ours == theirs, f"{repo}: steps.toml {sorted(ours ^ theirs)} differ from the repo's own list"


def test_registry_shape():
    for name, s in registry.steps().items():
        assert s["access"] in registry.ACCESS
        for spec in s["args"].values():
            assert spec in ("flag", "date") or spec.startswith("int:"), (name, spec)
    # what may never be the agent's: arbitrary paths and uncapped collection
    for name in ("processor.ingest-dir", "collector.collect", "collector.discover", "collector.fetch"):
        assert registry.get(name)["access"] == "user"
    assert "--batch" not in registry.get("processor.ingest")["args"]
    assert all("--outbox" not in s["args"] for s in registry.steps().values())


@pytest.mark.parametrize("name,args,ok", [
    ("processor.ingest", ["--next"], True), ("processor.ingest", ["--all"], True),
    ("processor.ingest", ["--batch", "/etc"], False), ("processor.ingest", ["--next", "--next"], False),
    ("collector.export", ["--max-files", "500"], True), ("collector.export", ["--max-files=10"], True),
    ("collector.export", ["--max-files", "501"], False), ("collector.export", ["--max-files", "-1"], False),
    ("processor.quant-bracket", ["--date", "2007-06-30", "--limit", "5"], True),
    ("processor.quant-bracket", ["--date", "2007-02-30"], False), ("processor.status", ["--outbox", "/"], False),
])
def test_check_args(name, args, ok):
    assert (registry.check_args(name, args) is None) == ok


def test_parse_result_takes_the_last_json_object():
    out = 'noise\n{"not": "a result"}\n{"step": "x", "status": "ok"}\n'
    assert registry.parse_result(out) == {"step": "x", "status": "ok"}
    assert registry.parse_result("Traceback ...") is None


def test_missing_environment_is_blocked_not_a_crash(monkeypatch, tmp_path):
    data = {"repos": {"r": {"python": str(tmp_path / "nope" / "python"), "module": "m", "cwd": str(tmp_path),
                            "setup": "make it"}},
            "steps": {"r.s": {"access": "agent", "args": {}, "repo": "r", "step": "s"}}}
    monkeypatch.setattr(registry, "load", lambda: data)
    code, res = registry.run("r.s")
    assert code == 2 and res["status"] == "blocked" and "make it" in res["reason"]


def test_step_without_json_is_failed(monkeypatch, tmp_path):
    (tmp_path / "m.py").write_text("import sys; print('boom'); sys.exit(3)\n")
    data = {"repos": {"r": {"python": sys.executable, "module": "m", "cwd": str(tmp_path), "setup": "-"}},
            "steps": {"r.s": {"access": "agent", "args": {}, "repo": "r", "step": "s"}}}
    monkeypatch.setattr(registry, "load", lambda: data)
    code, res = registry.run("r.s", stderr=subprocess.DEVNULL)
    assert code == 3 and res["status"] == "failed" and "boom" in res["reason"]


# ---- ./rag -------------------------------------------------------------------------------------------------------
@needs["collector"]
def test_rag_step_passes_json_and_exit_code_through():
    code, res, _ = rag("step", "collector.audit")
    assert code == 0 and res["step"] == "collector.audit" and res["contract_version"] == 1
    assert res["counts"]["errors"] == 0
    code, res, _ = rag("step", "nope.x")
    assert code == 1 and res["status"] == "failed"


@needs["collector"]
@needs["processor"]
def test_rag_status_has_both_halves():
    code, res, _ = rag("status")
    assert code == 0
    assert res["collector"]["step"] == "collector.status" and res["processor"]["step"] == "processor.status"
    assert {"running", "recent"} <= set(res["runs"])
    # in the monorepo the processor reads the collector's outbox
    assert res["processor"]["outbox"]["path"] == str(ROOT / "collector" / "data" / "export")


def test_rag_tool_runs_in_the_collector(tmp_path):
    code, res, _ = rag("job", "status")
    assert code == 0 and "running" in res


# ---- background runs ---------------------------------------------------------------------------------------------
@needs["collector"]
def test_background_run_lifecycle(tmp_path, monkeypatch):
    env = {"RAG_BG_DIR": str(tmp_path)}
    monkeypatch.setattr(bg, "BG_DIR", tmp_path)
    code, res, _ = rag("start", "collector.list", env=env)
    assert code == 0 and res["status"] == "ok", res
    run_id = res["run_id"]
    assert bg.RUN_ID.fullmatch(run_id) and res["next"] == f"./rag runs {run_id}"
    deadline = time.time() + 60
    while time.time() < deadline and (bg.read(run_id) or {}).get("state") != "finished":
        time.sleep(0.2)
    state = bg.read(run_id)
    assert state["state"] == "finished" and state["exit_code"] == 0
    assert state["result"]["step"] == "collector.list" and state["result"]["status"] == "ok"
    code, res, _ = rag("runs", run_id, env=env)
    assert res["state"] == "finished" and res["result"]["status"] == "ok"
    code, res, _ = rag("runs", env=env)
    assert [r["run_id"] for r in res["recent"]] == [run_id]


def test_one_run_per_step_and_a_global_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "BG_DIR", tmp_path)
    for i, step in enumerate(["processor.ingest", "processor.quant-sync", "collector.export"]):
        bg._write({"run_id": f"20261124-00000{i}-{step}", "step": step, "args": [], "state": "running",
                   "started_at": bg.now(), "pid": os.getpid()})
    res = bg.start("processor.ingest", [])
    assert res["status"] == "retry_later" and "already running" in res["reason"]
    res = bg.start("processor.quant-zfactor", [])
    assert res["status"] == "retry_later" and "max 3" in res["reason"]


def test_a_dead_runner_is_lost_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "BG_DIR", tmp_path)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    bg._write({"run_id": "20261124-000000-processor.ingest", "step": "processor.ingest", "args": [],
               "state": "running", "started_at": "2026-11-24T00:00:00+00:00", "pid": p.pid})
    assert bg.read("20261124-000000-processor.ingest")["state"] == "lost"
    assert bg.running() == []


def test_run_ids_cannot_escape_the_runs_dir():
    for bad in ("../x", "20261124-000000-processor.ingest/../../x", "x.json", ""):
        with pytest.raises(ValueError):
            bg._path(bad)


# ---- the scheduler -----------------------------------------------------------------------------------------------
def make_loop(monkeypatch, tmp_path, decisions, pending=0, **kw):
    a = Namespace(mode="deterministic", max_queries=2, export_batch=10, max_pending=3, poll=0, max_failures=3,
                  failure_pause_min=0, max_restarts=1, stop_timeout=1, once=False)
    for k, v in kw.items():
        setattr(a, k, v)
    monkeypatch.setattr(loop, "LOG", tmp_path / "loop.jsonl")
    monkeypatch.setattr(loop, "STOP", tmp_path / "STOP")
    lp = loop.Loop(a)
    seq = iter(decisions)
    calls = {"cycles": 0, "pending": 0, "export_batches": []}

    def cycle(export_batches=20):
        calls["cycles"] += 1
        calls["export_batches"].append(export_batches)
        return next(seq)

    def pend():
        calls["pending"] += 1
        return pending(calls["pending"]) if callable(pending) else pending

    monkeypatch.setattr(lp, "collector_cycle", cycle)
    monkeypatch.setattr(lp, "pending", pend)
    monkeypatch.setattr(lp, "start_watch", lambda: None)
    monkeypatch.setattr(lp, "stop_watch", lambda: None)
    monkeypatch.setattr(lp, "sleep", lambda s: None)
    return lp, calls


def events(tmp_path):
    return [json.loads(x) for x in (tmp_path / "loop.jsonl").read_text().splitlines()]


def test_loop_stops_when_the_collector_says_so(monkeypatch, tmp_path):
    lp, calls = make_loop(monkeypatch, tmp_path, [
        {"loop_decision": "continue", "pause_min": 1, "why": "collected new data"},
        {"loop_decision": "stop", "pause_min": 0, "why": "blocked: low_disk_space"}])
    assert lp.main() == 1
    assert calls["cycles"] == 2
    assert [e["event"] for e in events(tmp_path)] == ["collector_cycle", "collector_cycle", "stopped"]


def test_loop_backpressure_holds_collection(monkeypatch, tmp_path):
    lp, calls = make_loop(monkeypatch, tmp_path, [{"loop_decision": "stop", "pause_min": 0, "why": "x"}],
                          pending=lambda n: 5 if n < 3 else 0)
    lp.main()
    assert calls["cycles"] == 1 and calls["pending"] == 3
    assert [e["event"] for e in events(tmp_path)][:2] == ["backpressure", "backpressure"]


def test_loop_gives_up_after_consecutive_failures(monkeypatch, tmp_path):
    fail = {"loop_decision": "failed", "pause_min": 0, "why": "export: boom"}
    ok = {"loop_decision": "continue", "pause_min": 0, "why": "nothing new"}
    lp, calls = make_loop(monkeypatch, tmp_path, [fail, ok, fail, fail, fail, ok])
    assert lp.main() == 1
    assert calls["cycles"] == 5   # the ok cycle reset the count


def test_loop_stop_file(monkeypatch, tmp_path):
    ok = {"loop_decision": "continue", "pause_min": 0, "why": "nothing new"}
    lp, calls = make_loop(monkeypatch, tmp_path, [ok] * 10)
    orig = lp.collector_cycle

    def cycle_then_stop(export_batches=20):
        (tmp_path / "STOP").touch()
        return orig(export_batches)
    monkeypatch.setattr(lp, "collector_cycle", cycle_then_stop)
    assert lp.main() == 0 and calls["cycles"] == 1


def test_loop_commands():
    a = Namespace(mode="agent", max_queries=2, export_batch=10, poll=30)
    lp = loop.Loop(a)
    cmd = lp.collector_cmd()
    assert cmd[1:4] == ["-m", "rag_collector.loop", "--once"] and cmd[cmd.index("--agent-cmd") + 1] == str(loop.AGENT_CMD)
    lp.a.mode = "deterministic"
    assert "--agent-cmd" not in lp.collector_cmd() and "--max-queries" in lp.collector_cmd()
    cmd = lp.collector_cmd(0)
    assert cmd[cmd.index("--export-max-batches") + 1] == "1"   # never 0: a cycle may always export one batch
    assert lp.processor_cmd(True)[1:] == ["main.py", "--watch", "--poll", "30", "--once"]


# ---- Neon audit regressions ----------------------------------------------------------------------------------------
def test_pending_reads_the_real_processor_status_shape(monkeypatch):
    """C-1: processor.status puts pending_count under "outbox" (counts is empty)."""
    status = {"step": "status", "status": "ok", "counts": {},
              "outbox": {"path": "x", "exists": True, "ready": 5, "pending": ["a", "b", "c", "d"], "pending_count": 4}}
    monkeypatch.setattr(registry, "run", lambda *a, **k: (0, status))
    lp = loop.Loop(Namespace(once=False))
    assert lp.pending() == 4
    monkeypatch.setattr(registry, "run", lambda *a, **k: (75, {"status": "retry_later", "counts": {}}))
    assert lp.pending() is None


def test_backpressure_triggers_from_a_real_status_payload(monkeypatch, tmp_path):
    lp, calls = make_loop(monkeypatch, tmp_path, [{"loop_decision": "stop", "pause_min": 0, "why": "x"}])
    n = {"i": 0}

    def fake_run(*a, **k):
        n["i"] += 1
        return 0, {"status": "ok", "counts": {}, "outbox": {"pending_count": 5 if n["i"] == 1 else 1}}
    monkeypatch.setattr(registry, "run", fake_run)
    monkeypatch.setattr(lp, "pending", loop.Loop.pending.__get__(lp))
    lp.main()
    assert [e["event"] for e in events(tmp_path)][0] == "backpressure"
    assert calls["export_batches"] == [2]   # H-6: export only into the room left under --max-pending (3 - 1)


def test_unknown_pending_is_logged_not_silent(monkeypatch, tmp_path):
    lp, calls = make_loop(monkeypatch, tmp_path, [{"loop_decision": "stop", "pause_min": 0, "why": "x"}],
                          pending=None)
    lp.main()
    assert calls["cycles"] == 1 and events(tmp_path)[0]["event"] == "pending_unknown"


def test_processor_stop_follows_its_settings(monkeypatch, tmp_path):
    """M-3: the STOP file the watcher reads, also with DATA_DIR / STOP_FILE overridden."""
    for k in ("STOP_FILE", "DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    assert loop.processor_stop() == ROOT / "processor" / "data" / "STOP"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    assert loop.processor_stop() == tmp_path / "d" / "STOP"
    monkeypatch.setenv("STOP_FILE", "rel/STOP")
    assert loop.processor_stop() == ROOT / "processor" / "rel" / "STOP"


def test_collector_cycle_keeps_the_watcher_alive(monkeypatch, tmp_path):
    """M-3: a long cycle still checks the processor watcher, and its decision line is read."""
    lp = loop.Loop(Namespace(once=False, failure_pause_min=5))
    script = "import time, json; print('progress', flush=True); time.sleep(6); print(json.dumps({'loop_decision': 'continue'}))"
    monkeypatch.setattr(lp, "collector_cmd", lambda n=20: [sys.executable, "-c", script])
    checks = []
    monkeypatch.setattr(lp, "check_watch", lambda: checks.append(1))
    d = lp.collector_cycle()
    assert d == {"loop_decision": "continue"} and checks


def test_bg_start_is_atomic(monkeypatch, tmp_path):
    """M-1: concurrent starts of one step: exactly one is started."""
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(bg, "BG_DIR", tmp_path)
    monkeypatch.setattr(bg.subprocess, "Popen", lambda *a, **k: None)
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: bg.start("processor.quant-sync", []), range(8)))
    assert sorted(r["status"] for r in res) == ["ok"] + ["retry_later"] * 7


def test_internal_bg_without_a_run_id_is_a_usage_error():
    r = subprocess.run([RAG, "_bg"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "usage" in r.stderr


# ---- ultrareview fixes ---------------------------------------------------------------------------------------------
def test_stop_file_reaches_a_running_collector_cycle(monkeypatch, tmp_path):
    """`touch logs/STOP` mid-cycle sends the collector SIGTERM (it ends its current step) instead of waiting."""
    monkeypatch.setattr(loop, "STOP", tmp_path / "STOP")
    lp = loop.Loop(Namespace(once=False, failure_pause_min=5))
    script = ("import signal, sys, time, json\n"
              "signal.signal(signal.SIGTERM, lambda *a: (print(json.dumps({'loop_decision': 'stop', 'why': 'stop requested'}),"
              " flush=True), sys.exit(0)))\n"
              "print('progress', flush=True)\n"
              "time.sleep(120)\n")
    monkeypatch.setattr(lp, "collector_cmd", lambda n=20: [sys.executable, "-c", script])
    monkeypatch.setattr(lp, "check_watch", lambda: None)
    (tmp_path / "STOP").touch()
    t0 = time.monotonic()
    d = lp.collector_cycle()
    assert time.monotonic() - t0 < 30
    assert d["loop_decision"] == "stop" and lp.stopping


def test_a_reused_pid_does_not_keep_a_dead_run_running(tmp_path, monkeypatch):
    """The runner's pid now belongs to another live process (here: this one); the free lock says it is gone."""
    monkeypatch.setattr(bg, "BG_DIR", tmp_path)
    rid = "20261124-000000-processor.ingest"
    bg._write({"run_id": rid, "step": "processor.ingest", "args": [], "state": "running",
               "started_at": "2026-11-24T00:00:00+00:00", "pid": os.getpid()})
    (tmp_path / f"{rid}.lock").touch()
    assert bg.read(rid)["state"] == "lost"


def test_a_live_runner_holding_its_lock_is_running(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(bg, "BG_DIR", tmp_path)
    rid = "20261124-000000-processor.ingest"
    bg._write({"run_id": rid, "step": "processor.ingest", "args": [], "state": "running",
               "started_at": "2026-11-24T00:00:00+00:00", "pid": 999999})
    fd = os.open(tmp_path / f"{rid}.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert bg.read(rid)["state"] == "running"
    finally:
        os.close(fd)
    assert bg.read(rid)["state"] == "lost"


def test_watcher_restarts_reset_after_a_healthy_run(monkeypatch, tmp_path):
    lp = loop.Loop(Namespace(once=False, max_restarts=1))
    monkeypatch.setattr(loop, "LOG", tmp_path / "loop.jsonl")
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    started = []
    monkeypatch.setattr(lp, "start_watch", lambda: started.append(1))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lp.watch, lp.watch_restarts = dead, 1
    lp.watch_started = time.monotonic() - loop.WATCH_HEALTHY_S - 1   # ran for over an hour: not a crash loop
    lp.check_watch()
    assert not lp.stopping and started == [1] and lp.watch_restarts == 1
    lp.watch_started = time.monotonic()                              # crashed right away again: give up
    lp.check_watch()
    assert lp.stopping


def test_watcher_runs_in_its_own_session(monkeypatch, tmp_path):
    lp = loop.Loop(Namespace(once=False, poll=1))
    seen = {}

    class P:
        pid = 1

    monkeypatch.setattr(loop, "LOG", tmp_path / "loop.jsonl")
    monkeypatch.setattr(loop, "processor_stop", lambda: tmp_path / "STOP")
    monkeypatch.setattr(loop.subprocess, "Popen", lambda *a, **k: (seen.update(k), P())[1])
    lp.start_watch()
    assert seen.get("start_new_session") is True


def test_launcher_refuses_an_old_python(tmp_path):
    """No collector/.venv and only an old python3 on PATH: a setup hint, not a tomllib traceback."""
    import shutil
    root = tmp_path / "r"
    root.mkdir()
    shutil.copy(ROOT / "rag", root / "rag")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "python3").write_text("#!/bin/sh\nexit 1\n")   # fails the version check like a 3.9 would
    (fake / "python3").chmod(0o755)
    r = subprocess.run(["/bin/bash", str(root / "rag"), "help"], capture_output=True, text=True,
                       env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 2 and "Python 3.11+" in r.stderr and "Traceback" not in r.stderr
