"""Run collect.py as a detached background job, so agent tools with short command timeouts
(Cline kills shell commands after 30 s) can start it, wait on it in short steps, and read its status.

  ./rag job start --max-queries 8            # any collect.py args; returns immediately
  ./rag job start --harvest-only --max-files 40
  ./rag job wait 1                           # block up to 25 s, then print status;
                                             # repeat with the "next" command it prints (wait 2, 3, …)
  ./rag job status                           # running? progress? final summary?
  ./rag job stop                             # graceful stop (collect.py saves its summary)
(from collector/ without rag-unified: `.venv/bin/python scripts/job.py …`)

Only one job runs at a time, and never next to a collect step (`./rag step collector.collect`, the
deterministic loop): the job holds the same lock, data/metadata/locks/collect.lock, for its whole run. Output goes to logs/jobs/<started>.out; progress comes from the job's
events in logs/events.jsonl. The wait counter matters: Cline stops a session after 5 identical tool
calls in a row (its loop detection), so every wait call must differ from the last.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from common import atomic_write_text, EVENTS, LOGS, METADATA, ROOT, emit, log

JOB_FILE = METADATA / "job.json"
COLLECT_LOCK = METADATA / "locks" / "collect.lock"  # the lock rag_collector.steps takes for its collect stages
JOBS_DIR = LOGS / "jobs"
WAIT_MAX_S = 25


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a finished child we spawned may linger as a zombie until reaped
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def load_job() -> dict | None:
    if not JOB_FILE.exists():
        return None
    return json.loads(JOB_FILE.read_text(encoding="utf-8"))


def tail(path, n: int) -> list[str]:
    try:
        lines = (ROOT / path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return lines[-n:]


def progress(pid: int, n: int = 4) -> list[str]:
    """Last n events of the job's collect.py run (its run_id ends with -<pid>)."""
    try:
        with EVENTS.open("rb") as f:
            start = max(0, f.seek(0, 2) - 200_000)
            f.seek(start)
            lines = f.read().decode("utf-8", errors="ignore").splitlines()[1 if start else 0:]  # skip a cut line
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if str(e.get("run_id", "")).endswith(f"-{pid}") and e.get("msg"):
            out.append(f"[{e['ts'][11:19]}] {e.get('event')}: {e['msg'][:140]}")
            if len(out) == n:
                break
    return out[::-1]


def status() -> dict:
    job = load_job()
    if not job:
        return {"job": None, "running": False}
    running = alive(job["pid"])
    out = tail(job["output"], 40)
    result = None
    for line in reversed(out):
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except ValueError:
                pass
            break
    s = {"running": running, "args": job["args"], "started_at": job["started_at"],
         "elapsed_s": round(time.time() - job["started_ts"]), "output": job["output"],
         "progress": progress(job["pid"])}
    if not running:
        s["result"] = result or {"status": "unknown", "last_output": out[-5:]}
    return s


def start(args: list[str]) -> dict:
    job = load_job()
    if job and alive(job["pid"]):
        return {"started": False, "reason": "a job is already running", **status()}
    COLLECT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(COLLECT_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"started": False, "reason": "another collection run holds the collect lock "
                                                "(./rag loop or ./rag step collector.collect); try again later"}
        return _start_locked(args, fd)
    finally:
        os.close(fd)  # the job keeps its inherited copy, so the lock lasts exactly as long as collect.py


def _start_locked(args: list[str], lock_fd: int) -> dict:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = JOBS_DIR / f"{stamp}.out"
    with out_path.open("w") as out:
        p = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts" / "collect.py"), *args],
                             cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             pass_fds=(lock_fd,),  # holds the collect lock until it exits
                             start_new_session=True)  # survives the caller being killed
    job = {"pid": p.pid, "args": args, "started_at": stamp, "started_ts": time.time(),
           "output": str(out_path.relative_to(ROOT))}
    atomic_write_text(JOB_FILE, json.dumps(job, indent=1))
    log(f"job started pid={p.pid}: collect.py {' '.join(args)}", kind="job_start", pid=p.pid, args=args)
    return {"started": True, "pid": p.pid, "output": job["output"],
            "next": "./rag job wait 1"}


def wait(poll: int) -> dict:
    job = load_job()
    deadline = time.time() + WAIT_MAX_S
    while job and alive(job["pid"]) and time.time() < deadline:
        time.sleep(1)
    s = status()
    if s.get("running"):
        s["next"] = f"./rag job wait {poll + 1}"
    return s


def stop() -> dict:
    job = load_job()
    if not job or not alive(job["pid"]):
        return {"stopped": False, "reason": "no running job"}
    os.killpg(job["pid"], signal.SIGINT)  # collect.py treats SIGINT as a graceful interrupt
    for _ in range(20):
        if not alive(job["pid"]):
            break
        time.sleep(1)
    log(f"job stopped pid={job['pid']}", kind="job_stop", pid=job["pid"])
    return {"stopped": True, **status()}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("start", "status", "wait", "stop"):
        print(__doc__)
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "start":
        emit(start(rest))
    elif cmd == "wait":
        emit(wait(int(rest[0]) if rest and rest[0].isdigit() else 1))
    elif cmd == "stop":
        emit(stop())
    else:
        emit(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
