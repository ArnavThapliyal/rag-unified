"""The step registry (unified/steps.toml) and how a step is run: with its own repo's interpreter, in its own
repo's directory, its one JSON result passed through unchanged."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = Path(__file__).with_name("steps.toml")
ACCESS = ("agent", "background", "user")
# the collector's agent tools: ./rag <tool> -> collector/scripts/<script> (./rag log "x" -> common.py log "x")
TOOLS = {"collect": "collect.py", "job": "job.py", "status_report": "status_report.py",
         "report_issue": "report_issue.py", "inspect_artifact": "inspect_artifact.py",
         "audit_dataset": "audit_dataset.py", "build_metadata": "build_metadata.py", "web_search": "web_search.py",
         "docket": "docket.py", "add_queries": "add_queries.py", "log": "common.py"}
EXIT = {"ok": 0, "noop": 0, "partial": 0, "failed": 1, "blocked": 2, "retry_later": 75}


class UnknownStep(KeyError):
    pass


@lru_cache(maxsize=1)
def load() -> dict:
    data = tomllib.loads(REGISTRY.read_text(encoding="utf-8"))
    for name, s in data["steps"].items():
        repo, _, step = name.partition(".")
        if repo not in data["repos"] or not step:
            raise ValueError(f"steps.toml: {name!r} is not <repo>.<step> with a known repo")
        if s.get("access") not in ACCESS:
            raise ValueError(f"steps.toml: {name}: access must be one of {ACCESS}")
        s.setdefault("args", {})
        s["repo"], s["step"] = repo, step
    return data


def steps() -> dict:
    return load()["steps"]


def get(name: str) -> dict:
    try:
        return steps()[name]
    except KeyError:
        raise UnknownStep(name) from None


def repo(name: str) -> dict:
    return load()["repos"][name]


def python_for(repo_name: str) -> Path:
    return ROOT / repo(repo_name)["python"]


# ---- argument specs (what the agent may pass; agent/tool_guard.py enforces them) ------------------------------
def check_value(spec: str, value: str) -> str | None:
    """None if value fits spec ("int:LO:HI" | "date"), else why not."""
    if spec.startswith("int:"):
        lo, hi = (int(x) for x in spec.split(":")[1:])
        if not re.fullmatch(r"\d{1,7}", value) or not lo <= int(value) <= hi:
            return f"{value!r} is not a number from {lo} to {hi}"
        return None
    if spec == "date":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            return f"{value!r} is not a date (YYYY-MM-DD)"
        return None
    return f"unknown spec {spec!r}"


def check_args(name: str, args: list[str]) -> str | None:
    """None if the agent may pass args to step name, else why not. Exact option names, each at most once."""
    spec = get(name)["args"]
    seen: set[str] = set()
    i = 0
    while i < len(args):
        opt, eq, val = args[i].partition("=")
        if opt not in spec:
            allowed = ", ".join(spec) or "none"
            return f"{name}: argument {args[i]!r} is not allowed (allowed: {allowed})"
        if opt in seen:
            return f"{name}: {opt} at most once"
        seen.add(opt)
        if spec[opt] == "flag":
            if eq:
                return f"{name}: {opt} takes no value"
        else:
            if not eq:
                i += 1
                if i >= len(args):
                    return f"{name}: {opt} needs a value"
                val = args[i]
            why = check_value(spec[opt], val)
            if why:
                return f"{name}: {opt}: {why}"
        i += 1
    return None


# ---- running ----------------------------------------------------------------------------------------------------
def failed(name: str, reason: str, status: str = "failed") -> dict:
    return {"step": name, "status": status, "reason": reason, "counts": {}, "next_suggested": [],
            "contract_version": 1}


def env_for() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def parse_result(stdout: str) -> dict | None:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                res = json.loads(line)
            except ValueError:
                continue
            if isinstance(res, dict) and "status" in res:
                return res
    return None


def run(name: str, args: list[str] | None = None, stderr=None, timeout: float | None = None) -> tuple[int, dict]:
    """Run one step; returns (exit code, result). stderr: where the step's progress goes (default: ours)."""
    s = get(name)
    r = repo(s["repo"])
    py = ROOT / r["python"]
    if not py.exists():
        res = failed(name, f"{r['python']} is missing; set it up with: {r['setup']}", "blocked")
        return EXIT["blocked"], res
    cmd = [str(py), "-m", r["module"], s["step"], *(args or [])]
    try:
        p = subprocess.run(cmd, cwd=ROOT / r["cwd"], env=env_for(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=stderr if stderr is not None else sys.stderr, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return EXIT["failed"], failed(name, f"timed out after {timeout:g} s")
    res = parse_result(p.stdout)
    if res is None:
        tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
        return p.returncode or 1, failed(name, f"no JSON result (exit {p.returncode}) {tail[0][:200]}".strip())
    res["step"] = name  # repo-qualified
    return p.returncode, res
