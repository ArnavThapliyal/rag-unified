"""Step layer: contract and locks, universe-sync, export batches (immutability, READY, snapshot), loop decisions."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from rag_collector import export, paths, stepkit, universe_sync
from rag_collector.loop import Loop

REPO = Path(__file__).resolve().parent.parent
MASTER_COLS = ["record_id", "company_name", "nse_symbol", "isin", "delisting_type", "order_date",
               "effective_delisting_date", "calendar_year", "financial_year", "is_priority_year", "regulatory_regime",
               "exit_or_fair_value_inr", "source_url", "local_artifact_path", "confidence_status", "source_count",
               "all_source_urls", "all_artifact_paths", "source_conflicts", "review_flags", "date_source"]


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A minimal collector tree: reference lists, a master with two records, a docket with three items."""
    root = tmp_path / "collector"
    monkeypatch.setenv("RAG_COLLECTOR_ROOT", str(root))
    monkeypatch.setenv("RAG_COLLECTOR_OUTBOX", str(tmp_path / "outbox"))
    write_csv(root / "data/reference/nse_equity_listed.csv",
              ["SYMBOL", "NAME OF COMPANY", " SERIES", " DATE OF LISTING", " ISIN NUMBER"],
              [{"SYMBOL": "ACTIVE1", "NAME OF COMPANY": "Active One Limited", " SERIES": "EQ",
                " DATE OF LISTING": "01-JAN-2000", " ISIN NUMBER": "INE000A01011"}])
    write_csv(root / "data/reference/nse_sme_listed.csv", ["SYMBOL", "NAME_OF_COMPANY"], [])
    raw = root / "data/raw/priority_2007"
    raw.mkdir(parents=True)
    (raw / "a.pdf").write_bytes(b"%PDF-1.4 order for Gone Limited")
    (raw / "b.html").write_bytes(b"<html>list of many companies</html>")
    (raw / "c.pdf").write_bytes(b"%PDF-1.4 an irrelevant prospectus")
    write_csv(root / "data/processed/nse_delisted_master.csv", MASTER_COLS, [
        {"record_id": "r1", "company_name": "Gone Limited", "nse_symbol": "GONE", "isin": "INE111A01011",
         "delisting_type": "VOLUNTARY", "order_date": "2007-05-02", "effective_delisting_date": "2007-06-01",
         "local_artifact_path": "data/raw/priority_2007/a.pdf",
         "all_artifact_paths": "data/raw/priority_2007/a.pdf | data/raw/priority_2007/b.html"},
        {"record_id": "r2", "company_name": "Other Limited", "nse_symbol": "UNKNOWN", "isin": "",
         "order_date": "UNKNOWN", "effective_delisting_date": "2008-01-10",
         "local_artifact_path": "data/raw/priority_2007/b.html", "all_artifact_paths": "data/raw/priority_2007/b.html"},
    ])
    con = sqlite3.connect(root / "data/docket.sqlite")
    con.execute("CREATE TABLE items (url TEXT PRIMARY KEY, kind TEXT, status TEXT, year_tag TEXT, local_path TEXT,"
                " relevance TEXT, title TEXT, fetched_at REAL)")
    con.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)", [
        ("https://nsearchives.nseindia.com/content/Delisting_Order-GONE.pdf", "search", "parsed", "2007",
         "data/raw/priority_2007/a.pdf", "content", "Order", 1.7e9),
        ("https://www.bseindia.com/list.html", "harvested", "parsed", "2007", "data/raw/priority_2007/b.html", "url",
         "List", 1.7e9 + 1),
        ("https://example.com/prospectus.pdf", "search", "parsed", "2007", "data/raw/priority_2007/c.pdf",
         "irrelevant:procedural_document", None, 1.7e9 + 2),
    ])
    con.commit()
    con.close()
    return root


# ---------------------------------------------------------------- contract

def test_exit_codes_and_unknown_status():
    assert stepkit.EXIT == {"ok": 0, "noop": 0, "partial": 0, "failed": 1, "blocked": 2, "retry_later": 75}
    with pytest.raises(ValueError):
        stepkit.result("x", "maybe")


def test_lock_busy_is_retry_later_and_errors_are_json(tmp_path):
    def inner():
        return stepkit.run_step("s", lambda: stepkit.result("s", "ok"), tmp_path)
    res = stepkit.run_step("s", inner, tmp_path)   # the outer run holds the lock while the inner one starts
    assert res["status"] == "retry_later"
    assert stepkit.run_step("s", lambda: stepkit.result("s", "ok"), tmp_path)["status"] == "ok"  # released
    with stepkit.step_lock(tmp_path, "s"):
        busy = stepkit.run_step("s", lambda: stepkit.result("s", "ok"), tmp_path)
    assert busy["status"] == "retry_later"
    boom = stepkit.run_step("t", lambda: 1 / 0, tmp_path)
    assert boom["status"] == "failed" and "ZeroDivisionError" in boom["reason"]
    assert {"started_at", "finished_at", "duration_s", "contract_version"} <= set(boom)


def test_steps_cli_prints_one_json_object(repo):
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    r = subprocess.run([sys.executable, "-m", "rag_collector.steps", "list"], capture_output=True, text=True, env=env)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert {s["name"] for s in out["steps"]} >= {"discover", "fetch", "process", "universe-sync", "export", "status"}


# ---------------------------------------------------------------- universe-sync

def test_universe_sync_rebuilds_then_noops(repo):
    first = universe_sync.run()
    assert first["status"] == "ok"
    assert first["counts"]["delisted_2006_2026_rows"] == 2
    assert universe_sync.run()["status"] == "noop"
    # a record removed by the merge disappears from the DB (rebuild, not upsert)
    master = repo / "data/processed/nse_delisted_master.csv"
    rows = list(csv.DictReader(master.open()))[:1]
    write_csv(master, MASTER_COLS, rows)
    again = universe_sync.run()
    assert again["status"] == "ok" and again["counts"]["delisting_events_total"] == 1
    assert not list(paths.data().glob(".nse_unified.sqlite3.*.tmp"))


def test_universe_sync_blocked_without_master(repo):
    (repo / "data/processed/nse_delisted_master.csv").unlink()
    assert universe_sync.run()["status"] == "blocked"


# ---------------------------------------------------------------- export

def batch_dirs(outbox: Path) -> list[Path]:
    return sorted(d for d in outbox.iterdir() if d.is_dir() and not d.name.startswith("."))


def test_export_batch_layout_metadata_and_idempotency(repo):
    universe_sync.run()
    res = export.run()
    assert res["status"] == "ok", res
    assert res["counts"]["exported"] == 2  # the irrelevant item is not exported
    (b,) = batch_dirs(paths.outbox())
    assert (b / "READY").exists()
    ready = json.loads((b / "READY").read_text())
    assert ready["manifest_sha256"] == export.sha256_file(b / "manifest.json")
    man = json.loads((b / "manifest.json").read_text())
    assert man["contract_version"] == 1 and man["ingest_root"] == "files"
    assert {f["path"] for f in man["files"]} == {"files/priority_2007/a.pdf", "files/priority_2007/b.html"}
    for f in man["files"]:
        p = b / f["path"]
        assert export.sha256_file(p) == f["sha256"]
        assert not os.access(p, os.W_OK) or os.geteuid() == 0  # read-only
    a_meta = json.loads((b / "files/priority_2007/a.pdf.meta.json").read_text())
    assert a_meta["doc_type"] == "delisting_order" and a_meta["vendor"] == "NSE"
    assert a_meta["collector_record_ids"] == ["r1"]
    b_meta = json.loads((b / "files/priority_2007/b.html.meta.json").read_text())
    assert b_meta["vendor"] == "BSE" and b_meta["collector_record_ids"] == ["r1", "r2"]
    assert "nse_symbol" not in b_meta and "published_at" not in b_meta  # two companies: no single identity
    # the universe snapshot is a valid, complete database
    con = sqlite3.connect(b / "universe.sqlite3")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM delisting_events").fetchone()[0] == 2
    con.close()
    assert man["universe"]["integrity_check"] == "ok"
    # nothing new: noop, no second batch
    assert export.run()["status"] == "noop"
    assert len(batch_dirs(paths.outbox())) == 1


def test_placeholders_never_become_metadata(repo):
    universe_sync.run()
    # make r2 the only record behind b.html
    master = repo / "data/processed/nse_delisted_master.csv"
    rows = list(csv.DictReader(master.open()))
    rows[0]["all_artifact_paths"] = "data/raw/priority_2007/a.pdf"
    write_csv(master, MASTER_COLS, rows)
    export.run()
    (b,) = batch_dirs(paths.outbox())
    meta = json.loads((b / "files/priority_2007/b.html.meta.json").read_text())
    assert "symbol" not in meta and "nse_symbol" not in meta          # "UNKNOWN" dropped
    assert meta["published_at"] == "2008-01-10"                       # "UNKNOWN" order date skipped


def test_refetched_raw_file_does_not_change_an_exported_batch(repo):
    universe_sync.run()
    export.run()
    (b,) = batch_dirs(paths.outbox())
    before = (b / "files/priority_2007/a.pdf").read_bytes()
    # the collector re-fetches the document in place (new content)
    (repo / "data/raw/priority_2007/a.pdf").write_bytes(b"%PDF-1.4 corrected order")
    assert (b / "files/priority_2007/a.pdf").read_bytes() == before
    man = json.loads((b / "manifest.json").read_text())
    entry = next(f for f in man["files"] if f["path"].endswith("a.pdf"))
    assert export.sha256_file(b / entry["path"]) == entry["sha256"]
    # the new content is a new document in the next batch
    res = export.run()
    assert res["status"] == "ok" and res["counts"]["exported"] == 1


def test_clone_is_never_a_hard_link(tmp_path):
    src, dst = tmp_path / "s.bin", tmp_path / "d" / "s.bin"
    src.write_bytes(b"x" * 10000)
    export.clone_file(src, dst)
    assert os.stat(src).st_ino != os.stat(dst).st_ino
    assert dst.read_bytes() == src.read_bytes()


def test_snapshot_of_a_wal_database_with_open_writer(tmp_path):
    db = tmp_path / "u.sqlite3"
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    for t in ("nse_entities", "delisting_events", "qualitative_filings"):
        w.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, v TEXT)")
    w.executemany("INSERT INTO nse_entities (v) VALUES (?)", [(str(i),) for i in range(500)])
    w.commit()
    w.execute("INSERT INTO nse_entities (v) VALUES ('uncommitted')")  # open write transaction, data still in WAL
    info = export.snapshot_sqlite(db, tmp_path / "snap.sqlite3")
    w.rollback()
    w.close()
    assert info["integrity_check"] == "ok" and info["nse_entities"] == 500
    # the snapshot is self-contained (no -wal sidecar needed)
    assert not (tmp_path / "snap.sqlite3-wal").exists()


def test_crashed_temp_batch_is_cleaned_and_never_ready(repo):
    ob = paths.outbox()
    stale = ob / ".20200101-000000.tmp" / "files"
    stale.mkdir(parents=True)
    (stale / "x.pdf").write_bytes(b"partial")
    (stale / "x.pdf").chmod(0o444)
    universe_sync.run()
    export.run()
    assert not (ob / ".20200101-000000.tmp").exists()
    assert all((d / "READY").exists() for d in batch_dirs(ob))


def test_dry_run_and_status_never_touch_the_outbox(repo):
    universe_sync.run()
    stale = paths.outbox() / ".20200101-000000.tmp"
    stale.mkdir(parents=True)
    res = export.run(dry_run=True)
    assert res["counts"]["would_export"] == 2
    assert stale.exists() and not batch_dirs(paths.outbox())


# ---------------------------------------------------------------- loop decisions

class FakeLoop(Loop):
    def __init__(self, seg: dict, others: dict | None = None):
        import argparse
        super().__init__(argparse.Namespace(mode="deterministic", max_queries=1, export_batch=10, idle_min=30,
                                            busy_pause_min=1, network_retry_min=10, failure_pause_min=5,
                                            max_failures=3, once=True, agent_cmd=None))
        self.seg, self.others, self.calls = seg, others or {}, []
        self.log_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"loop-test-{os.getpid()}.jsonl"

    def segment(self):
        return self.seg

    def step(self, name, *args):
        self.calls.append(name)
        return self.others.get(name, {"step": name, "status": "noop"})

    def cooldown_min(self):
        return 42.0


@pytest.mark.parametrize("seg,expect", [
    ({"status": "ok"}, ("continue", 1)),
    ({"status": "noop"}, ("continue", 30)),
    ({"status": "retry_later", "reason": "search_engines_suspended"}, ("continue", 42.0)),
    ({"status": "blocked", "reason": "network_unavailable"}, ("continue", 10)),
    ({"status": "blocked", "reason": "low_disk_space"}, ("stop", 0)),
    ({"status": "failed", "reason": "boom"}, ("failed", 5)),
])
def test_loop_decisions_come_from_step_status(repo, seg, expect):
    lp = FakeLoop({"step": "collect", **seg})
    action, pause, _ = lp.cycle(1)
    assert (action, pause) == expect
    assert lp.calls[:2] == ["universe-sync", "export"]  # deterministic post-steps always run


def test_loop_exports_until_nothing_waits(repo):
    calls = iter([{"step": "export", "status": "ok", "more_waiting": 5}, {"step": "export", "status": "ok",
                                                                         "more_waiting": 0}])
    lp = FakeLoop({"step": "collect", "status": "ok"})
    lp.step = lambda name, *a: next(calls) if name == "export" else {"step": name, "status": "noop"}
    lp.cycle(1)
    assert next(calls, None) is None


def test_stop_file_stops_the_loop(repo):
    lp = FakeLoop({"step": "collect", "status": "ok"})
    paths.stop_file().parent.mkdir(parents=True, exist_ok=True)
    paths.stop_file().touch()
    action, _, why = lp.cycle(1)
    assert action == "stop" and why == "stop requested"


def test_once_prints_the_decision_for_a_scheduler(repo, capsys, monkeypatch):
    lp = FakeLoop({"step": "collect", "status": "noop"})
    monkeypatch.setattr("signal.signal", lambda *a: None)
    lp.log_path = paths.root() / "logs" / "continuous.jsonl"
    assert lp.main() == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"loop_decision": "continue", "pause_min": 30, "why": "nothing new"}


def test_once_prints_stop_decisions_too(repo, capsys, monkeypatch):
    lp = FakeLoop({"step": "collect", "status": "blocked", "reason": "low_disk_space"})
    monkeypatch.setattr("signal.signal", lambda *a: None)
    lp.log_path = paths.root() / "logs" / "continuous.jsonl"
    assert lp.main() == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["loop_decision"] == "stop"


def test_agent_mode_without_a_runner_is_blocked(repo, tmp_path, monkeypatch):
    lp = FakeLoop({})
    lp.a.mode = "agent"
    monkeypatch.delenv("RAG_AGENT_CMD", raising=False)
    monkeypatch.setattr(paths, "root", lambda: tmp_path / "collector")
    assert Loop.segment(lp)["status"] == "blocked"
    runner = tmp_path / "agent" / "run_agent.sh"
    runner.parent.mkdir()
    runner.write_text("exit 0\n")
    assert lp.agent_cmd() == runner.resolve()             # rag-unified layout: ../agent/run_agent.sh
    lp.a.agent_cmd = str(tmp_path / "missing.sh")
    assert lp.agent_cmd() == runner.resolve()             # a missing --agent-cmd falls through


# ---------------------------------------------------------------- Neon audit regressions

def test_export_skips_in_batch_duplicates(repo):
    """M-2: two docket rows pointing at one file: exported once, no PermissionError on the read-only copy."""
    con = sqlite3.connect(repo / "data/docket.sqlite")
    con.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                ("https://mirror.example.com/Delisting_Order-GONE.pdf", "search", "parsed", "2007",
                 "data/raw/priority_2007/a.pdf", "content", "Order", 1.7e9 + 3))
    con.commit()
    con.close()
    universe_sync.run()
    res = export.run()
    assert res["status"] == "ok", res
    assert res["counts"]["exported"] == 2 and res["counts"]["duplicate_in_batch"] == 1


def test_read_only_steps_take_no_lock(tmp_path):
    """M-7: status runs while another status (or anything) holds its lock."""
    with stepkit.step_lock(tmp_path, "status"):
        assert stepkit.run_step("status", lambda: stepkit.result("status", "ok"), None)["status"] == "ok"


def test_agent_mode_ignores_run_summaries_from_earlier_cycles(repo, tmp_path, monkeypatch):
    """H-2: a stale low_disk_space summary must not block a session that ran no collection."""
    runs = paths.root() / "logs" / "runs"
    runs.mkdir(parents=True)
    old = runs / "20200101-000000-1.json"
    old.write_text(json.dumps({"stop_reason": "low_disk_space", "new_records": 0}))
    os.utime(old, (1e9, 1e9))
    lp = FakeLoop({})
    lp.a.mode = "agent"
    runner = tmp_path / "run_agent.sh"
    runner.write_text("exit 0\n")
    lp.a.agent_cmd = str(runner)
    assert Loop.segment(lp)["status"] == "noop"
    # a summary written during the session counts
    runner.write_text(f"echo '{{\"stop_reason\": \"low_disk_space\"}}' > {runs}/20990101-000000-2.json\n")
    assert Loop.segment(lp)["status"] == "blocked"


def test_export_burst_is_capped(repo):
    """H-6: --export-max-batches bounds the batches one cycle writes."""
    lp = FakeLoop({"step": "collect", "status": "ok"})
    lp.a.export_max_batches = 2
    n = {"export": 0}

    def step(name, *a):
        if name == "export":
            n["export"] += 1
            return {"step": "export", "status": "ok", "more_waiting": 100}
        return {"step": name, "status": "noop"}
    lp.step = step
    lp.cycle(1)
    assert n["export"] == 2


def test_job_holds_the_collect_lock(tmp_path, monkeypatch):
    """H-4: a job and a collect step never run together: the job holds data/metadata/locks/collect.lock."""
    import fcntl

    import job
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "collect.py").write_text("import time; time.sleep(4)\n")
    lock = tmp_path / "metadata" / "locks" / "collect.lock"
    for k, v in {"ROOT": tmp_path, "METADATA": tmp_path / "metadata", "JOBS_DIR": tmp_path / "jobs",
                 "JOB_FILE": tmp_path / "metadata" / "job.json", "COLLECT_LOCK": lock}.items():
        monkeypatch.setattr(job, k, v)
    monkeypatch.setattr(job, "log", lambda *a, **k: None)
    # a collect step holds it: no job
    with stepkit.step_lock(lock.parent, "collect"):
        r = job.start([])
    assert r["started"] is False and "collect lock" in r["reason"]
    # a job holds it for as long as collect.py runs
    r = job.start([])
    assert r["started"] is True and r["next"] == "./rag job wait 1"
    fd = os.open(lock, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert stepkit.run_step("collect", lambda: stepkit.result("collect", "ok"), lock.parent)["status"] == "retry_later"
    finally:
        os.close(fd)
        os.kill(r["pid"], 9)


def test_a_collect_run_blocked_by_another_is_retry_later(monkeypatch):
    """collect.py exits 75 with stop_reason collection_running when another collection holds its lock: the step
    contract's retry_later (the agent and the loop wait), never a failure."""
    from rag_collector import steps as st

    monkeypatch.setattr(st, "_run_json", lambda cmd, timeout=None: (
        75, {"status": "busy", "stop_reason": "collection_running", "error": "another collection run is already active"},
        ""))
    res = st.collect_step(next(iter(st.COLLECT_STAGES)), [])
    assert res["status"] == "retry_later" and res["reason"] == "collection_running"
