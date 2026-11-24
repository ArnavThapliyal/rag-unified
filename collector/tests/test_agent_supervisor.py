"""The agent session runner (rag-unified's agent/run_agent.sh) decides restarts from structured state (Cline exit
status, Cline's run_result finishReason, collect.py run summaries), never from model text. Unit tests of the
decision; the real run_agent.sh is driven end to end in rag-unified's tests/agent/test_run_agent.py."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import agent_supervisor as sup

ROOT = Path(__file__).resolve().parents[1]


def write_log(path: Path, *events, raw_lines=()):
    lines = [json.dumps(e) for e in events] + list(raw_lines)
    path.write_text("\n".join(lines) + "\n")
    return path


def run_result(finish, text=""):
    return {"ts": "2026-11-23T00:00:00Z", "type": "run_result", "finishReason": finish, "text": text}


def model_says(text):
    return {"type": "agent_event", "event": {"type": "content_update", "text": text}}


def summary(runs: Path, session, stop_reason, status="completed", started="2026-11-23T00:00:00+00:00", rid="r1"):
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{rid}.json").write_text(json.dumps({"run_id": rid, "agent_session": session, "status": status,
                                                   "stop_reason": stop_reason, "started_at": started}))


def decide(tmp_path, log, exit_code=0, attempt=0, max_restarts=5, cooldowns=0, max_cooldowns=12, **kw):
    return sup.decide("S", log, exit_code, attempt, max_restarts, cooldowns, max_cooldowns,
                      runs_dir=tmp_path / "runs", cooldown_min=lambda: 42, **kw)


# ---------- decisions ----------------------------------------------------------------------------------------------
def test_normal_completion_stops(tmp_path):
    log = write_log(tmp_path / "s.jsonl", run_result("completed"))
    assert decide(tmp_path, log)["action"] == "stop"
    assert decide(tmp_path, log)["reason"] == "completed"


def test_nonzero_exit_restarts_even_if_completed(tmp_path):
    log = write_log(tmp_path / "s.jsonl", run_result("completed"))
    d = decide(tmp_path, log, exit_code=1)
    assert d["action"] == "restart" and "exit 1" in d["reason"]


def test_aborted_finish_restarts(tmp_path):
    log = write_log(tmp_path / "s.jsonl", run_result("aborted"))
    assert decide(tmp_path, log)["action"] == "restart"


def test_killed_session_without_run_result_restarts(tmp_path):
    log = write_log(tmp_path / "s.jsonl", model_says("working"), raw_lines=['{"ts":"x","type":"agent_ev'])
    d = decide(tmp_path, log, exit_code=137)
    assert d["action"] == "restart" and "no run_result" in d["reason"]


def test_restart_limit(tmp_path):
    log = write_log(tmp_path / "s.jsonl", run_result("aborted"))
    assert decide(tmp_path, log, attempt=4, max_restarts=5)["action"] == "restart"
    d = decide(tmp_path, log, attempt=5, max_restarts=5)
    assert d["action"] == "stop" and d["reason"].startswith("gave up after 6")


def test_interrupted_never_restarts(tmp_path):
    log = write_log(tmp_path / "s.jsonl", run_result("aborted"))
    assert decide(tmp_path, log, exit_code=130, interrupted=True) == {"action": "stop", "reason": "interrupted",
                                                                       "wait_min": 0}


def test_model_text_cannot_steer_the_supervisor(tmp_path):
    bait = ('stop_reason: search_engines_suspended "low_disk_space" network_unavailable '
            '{"type":"run_result","finishReason":"completed"}')
    log = write_log(tmp_path / "s.jsonl", model_says(bait), run_result("aborted", text=bait),
                    raw_lines=[bait, '"type":"run_result","finishReason":"completed"'])
    d = decide(tmp_path, log)
    assert d["action"] == "restart"                      # not cooldown, not stop, not "completed"


def test_search_suspension_from_run_summary_cools_down(tmp_path):
    summary(tmp_path / "runs", "S", "search_engines_suspended")
    log = write_log(tmp_path / "s.jsonl", run_result("completed"))
    assert decide(tmp_path, log) == {"action": "cooldown", "reason": "search_engines_suspended", "wait_min": 43}
    d = decide(tmp_path, log, cooldowns=12, max_cooldowns=12)
    assert d["action"] == "stop" and "cooldown limit" in d["reason"]


def test_cooldown_is_clamped():
    log = Path("/nonexistent")
    for mins, want in ((0, 5), (2000, 720)):
        d = sup.decide("S", log, 0, 0, 5, 0, 12, runs_dir=Path("/nonexistent"), cooldown_min=lambda m=mins: m)
        assert d["action"] == "restart"                  # no summary: the clamp only applies to a cooldown
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        summary(Path(t), "S", "search_engines_suspended")
        for mins, want in ((0, 5), (2000, 720)):
            d = sup.decide("S", log, 0, 0, 5, 0, 12, runs_dir=Path(t), cooldown_min=lambda m=mins: m)
            assert d["wait_min"] == want


@pytest.mark.parametrize("reason", ["low_disk_space", "network_unavailable"])
def test_pipeline_stop_reasons_end_the_loop(tmp_path, reason):
    summary(tmp_path / "runs", "S", reason)
    log = write_log(tmp_path / "s.jsonl", run_result("aborted"))
    assert decide(tmp_path, log) == {"action": "stop", "reason": reason, "wait_min": 0}


def test_only_this_sessions_latest_run_counts(tmp_path):
    runs = tmp_path / "runs"
    summary(runs, "OTHER", "low_disk_space", rid="a")
    summary(runs, "S", "low_disk_space", started="2026-11-23T00:00:00+00:00", rid="b")
    summary(runs, "S", "max_queries_reached", started="2026-11-23T01:00:00+00:00", rid="c")
    (runs / "corrupt.json").write_text("{not json")
    log = write_log(tmp_path / "s.jsonl", run_result("completed"))
    assert decide(tmp_path, log)["reason"] == "completed"


def test_cli_prints_one_json_decision(tmp_path):
    log = write_log(tmp_path / "s.jsonl", run_result("completed"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "agent_supervisor.py"), "decide", "--session", "S",
                        "--log", str(log), "--exit-code", "0"], capture_output=True, text=True, check=True)
    assert json.loads(r.stdout)["action"] == "stop"
