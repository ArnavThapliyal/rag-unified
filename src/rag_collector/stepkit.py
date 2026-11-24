"""The step contract shared by rag-collector and rag-processor (and read by rag-unified).

Every step prints exactly one JSON object on stdout:

    {"step": "...", "status": "ok|noop|partial|failed|blocked|retry_later", "counts": {...},
     "reason": "...", "next_suggested": [...], "started_at": "...", "finished_at": "...", "duration_s": N,
     "contract_version": 1, ...step-specific fields}

and exits with:

    0   ok, noop or partial (the step did what it could; "partial" says some items failed)
    1   failed (a bug or an unexpected error: look at reason)
    2   blocked (a precondition is missing: disk, network, config; retrying now will not help)
    75  retry_later (temporarily unavailable: another run holds the lock, search engines benched, ...)

Steps are idempotent: running one twice in a row is safe and the second run reports noop when nothing changed.
Each step holds an exclusive per-step lock for its whole run.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

CONTRACT_VERSION = 1
EXIT = {"ok": 0, "noop": 0, "partial": 0, "failed": 1, "blocked": 2, "retry_later": 75}
STATUSES = frozenset(EXIT)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def result(step: str, status: str, reason: str = "", counts: dict | None = None,
           next_suggested: list[str] | None = None, **extra: Any) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown step status: {status}")
    return {"step": step, "status": status, "reason": reason, "counts": counts or {},
            "next_suggested": next_suggested or [], "contract_version": CONTRACT_VERSION, **extra}


class LockBusy(Exception):
    pass


@contextlib.contextmanager
def step_lock(lock_dir: Path, name: str) -> Iterator[Path]:
    """Exclusive, non-blocking lock for one step (flock: released by the OS if the process dies)."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise LockBusy(name) from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield path
    finally:
        os.close(fd)


def run_step(name: str, fn, lock_dir: Path, *args, **kwargs) -> dict:
    """Run fn under the step's lock, time it, and turn exceptions into a failed result (never a traceback
    instead of JSON). fn returns a result() dict."""
    started, t0 = now_iso(), time.monotonic()
    try:
        with step_lock(lock_dir, name):
            res = fn(*args, **kwargs)
    except LockBusy:
        res = result(name, "retry_later", "another run of this step holds its lock")
    except KeyboardInterrupt:
        res = result(name, "failed", "interrupted")
    except Exception as e:  # the contract: one JSON object, whatever happened
        res = result(name, "failed", f"{type(e).__name__}: {e}")
    res.setdefault("step", name)
    res.update(started_at=started, finished_at=now_iso(), duration_s=round(time.monotonic() - t0, 2))
    return res


def emit(res: dict) -> int:
    sys.stdout.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return EXIT.get(res.get("status"), 1)
