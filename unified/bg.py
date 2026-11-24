"""Background step runs: `./rag start <step>` returns at once (Cline kills commands after 30 s); `./rag runs`
shows them. One run per step at a time, at most MAX_RUNNING in all. State: logs/bg/<run_id>.json, the step's
progress: logs/bg/<run_id>.log. A run survives the command that started it (own session)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from unified import registry

BG_DIR = Path(os.environ.get("RAG_BG_DIR") or registry.ROOT / "logs" / "bg")
MAX_RUNNING = 3
STARTUP_GRACE_S = 30  # a run whose process has not recorded its pid yet
RUN_ID = re.compile(r"\d{8}-\d{6}-[a-z]+\.[a-z-]+(?:-\d+)?")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _path(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"{run_id!r} is not a run id")
    return BG_DIR / f"{run_id}.json"


def _write(state: dict) -> None:
    p = _path(state["run_id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _age_s(ts: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(run_id: str) -> dict | None:
    try:
        state = json.loads(_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if state.get("state") == "running":
        pid = state.get("pid")
        starting = pid is None and _age_s(state.get("started_at")) < STARTUP_GRACE_S
        if not starting and not _alive(pid):
            state["state"] = "lost"  # the runner died without writing a result (machine slept, killed, …)
    return state


def all_runs() -> list[dict]:
    if not BG_DIR.is_dir():
        return []
    out = [read(p.stem) for p in sorted(BG_DIR.glob("*.json")) if RUN_ID.fullmatch(p.stem)]
    return [s for s in out if s]


def running() -> list[dict]:
    return [s for s in all_runs() if s["state"] == "running"]


def start(name: str, args: list[str]) -> dict:
    registry.get(name)
    busy = running()
    same = [s for s in busy if s["step"] == name]
    if same:
        return {**registry.failed("start", f"{name} is already running as {same[0]['run_id']}", "retry_later"),
                "run_id": same[0]["run_id"], "next": f"./rag runs {same[0]['run_id']}"}
    if len(busy) >= MAX_RUNNING:
        return registry.failed("start", f"{len(busy)} runs in progress (max {MAX_RUNNING}): "
                                        + ", ".join(s["run_id"] for s in busy), "retry_later")
    BG_DIR.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + name
    run_id, n = base, 1
    while _path(run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    state = {"run_id": run_id, "step": name, "args": args, "state": "running", "started_at": now(), "pid": None}
    _write(state)
    log = open(BG_DIR / f"{run_id}.log", "ab")
    subprocess.Popen([sys.executable, "-m", "unified", "_bg", run_id], cwd=registry.ROOT,
                         env={**registry.env_for(), "PYTHONPATH": str(registry.ROOT)}, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    log.close()
    # the child records its own pid (same process) as soon as it starts; only it writes the state from now on
    return {"step": "start", "status": "ok", "reason": f"{name} started in the background", "run_id": run_id,
            "counts": {}, "next_suggested": [], "contract_version": 1, "next": f"./rag runs {run_id}"}


def execute(run_id: str) -> int:
    """The background process: run the step, store its result."""
    state = read(run_id)
    if not state:
        return 1
    state.update(pid=os.getpid(), state="running")
    _write(state)
    try:
        code, res = registry.run(state["step"], state["args"], stderr=sys.stderr)
    except Exception as e:  # never leave a run "running" forever
        code, res = 1, registry.failed(state["step"], f"{type(e).__name__}: {e}")
    state.update(state="finished", finished_at=now(), exit_code=code, result=res)
    _write(state)
    return 0


def summary(state: dict) -> dict:
    res = state.get("result") or {}
    return {k: v for k, v in {"run_id": state["run_id"], "step": state["step"], "args": state.get("args"),
                              "state": state["state"], "started_at": state.get("started_at"),
                              "finished_at": state.get("finished_at"), "status": res.get("status"),
                              "reason": res.get("reason")}.items() if v not in (None, [], "")}
