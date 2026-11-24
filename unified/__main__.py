"""./rag: one entry point for the whole pipeline (see README.md).

    ./rag status                      both halves in one JSON object (+ background runs, the loop)
    ./rag steps                       the registry: every step, its repo, who may run it, the agent's arguments
    ./rag step <repo>.<step> [args]   run one step in the foreground; prints its JSON result, exits with its code
    ./rag start <repo>.<step> [args]  the same in the background; returns a run id at once
    ./rag runs [<run_id>]             background runs (recent ones, or one with its full result)
    ./rag loop [options]              continuous pipeline (./rag loop --help)
    ./rag agent [prompt file]         agent sessions on their own (agent/run_agent.sh)
    ./rag doctor                      environments, services and the sandbox
    ./rag <tool> [args]               the collector's agent tools (job, web_search, add_queries, …), as the agent
                                      runs them: ./rag job start --max-queries 8  ==  (cd collector && .venv/bin/python scripts/job.py …)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from unified import bg, registry

ROOT = registry.ROOT
TOOLS = registry.TOOLS


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def cmd_status() -> int:
    def one(name):
        return registry.run(name, [], stderr=subprocess.DEVNULL, timeout=120)[1]

    with ThreadPoolExecutor(2) as ex:
        col, proc = ex.map(one, ["collector.status", "processor.status"])
    loop_log = ROOT / "logs" / "loop.jsonl"
    last_loop = None
    try:
        last_loop = json.loads(loop_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        pass
    runs = bg.all_runs()
    nxt = [f"collector.{s}" for s in col.get("next_suggested") or []] + \
          [f"processor.{s}" for s in proc.get("next_suggested") or []]
    out({"step": "status", "status": "ok", "collector": col, "processor": proc,
         "runs": {"running": [bg.summary(s) for s in runs if s["state"] == "running"],
                  "recent": [bg.summary(s) for s in runs[-5:]]},
         "loop": {"last_event": last_loop, "stop_file": (ROOT / "logs" / "STOP").exists()},
         "next_suggested": nxt, "contract_version": 1})
    return 0


def cmd_steps() -> int:
    out({"step": "steps", "status": "ok",
         "steps": [{"name": n, "access": s["access"], "args": s["args"]} for n, s in registry.steps().items()],
         "contract_version": 1})
    return 0


def cmd_step(argv: list[str]) -> int:
    if not argv:
        out(registry.failed("step", "usage: ./rag step <repo>.<step> [args]"))
        return 1
    try:
        code, res = registry.run(argv[0], argv[1:])
    except registry.UnknownStep:
        out(registry.failed("step", f"unknown step {argv[0]!r} (./rag steps lists them)"))
        return 1
    out(res)
    return code


def cmd_start(argv: list[str]) -> int:
    if not argv:
        out(registry.failed("start", "usage: ./rag start <repo>.<step> [args]"))
        return 1
    try:
        res = bg.start(argv[0], argv[1:])
    except registry.UnknownStep:
        out(registry.failed("start", f"unknown step {argv[0]!r} (./rag steps lists them)"))
        return 1
    out(res)
    return registry.EXIT.get(res["status"], 1)


def cmd_runs(argv: list[str]) -> int:
    if argv:
        try:
            state = bg.read(argv[0])
        except ValueError as e:
            out(registry.failed("runs", str(e)))
            return 1
        if not state:
            out(registry.failed("runs", f"no run {argv[0]}"))
            return 1
        out({"step": "runs", "status": "ok", **state})
        return 0
    runs = bg.all_runs()
    out({"step": "runs", "status": "ok", "running": [bg.summary(s) for s in runs if s["state"] == "running"],
         "recent": [bg.summary(s) for s in runs[-10:]]})
    return 0


def cmd_tool(tool: str, argv: list[str]) -> int:
    script = TOOLS[tool]
    args = ["log", *argv] if tool == "log" else argv
    py = registry.python_for("collector")
    return subprocess.call([str(py), str(ROOT / "collector" / "scripts" / script), *args], cwd=ROOT / "collector",
                           env=registry.env_for(), stdin=subprocess.DEVNULL)


def cmd_doctor() -> int:
    checks = {}
    for repo in ("collector", "processor"):
        py = registry.python_for(repo)
        checks[f"{repo} environment"] = "ok" if py.exists() else f"missing: {registry.repo(repo)['setup']}"
    cline = ROOT / "collector" / "node_modules" / ".bin" / "cline"
    checks["cline"] = "ok" if cline.exists() else "missing: (cd collector && npm install)"
    for name, url in (("LM Studio", "http://localhost:1234/v1/models"), ("SearXNG", "http://localhost:8082/")):
        r = subprocess.run(["curl", "-sf", "-m", "5", "-o", "/dev/null", url], capture_output=True)
        checks[name] = "ok" if r.returncode == 0 else f"not reachable at {url}"
    if registry.python_for("processor").exists():
        res = registry.run("processor.health", [], stderr=subprocess.DEVNULL, timeout=300)[1]
        checks["processor health"] = "ok" if res["status"] == "ok" else res.get("reason") or res["status"]
        # agent sessions run offline against a read-only HF cache (run_agent.sh): the model must already be there
        r = subprocess.run([str(registry.python_for("processor")), "-c",
                            "from config.settings import EMBEDDING_MODEL as m; from huggingface_hub import "
                            "snapshot_download as d; d(m, local_files_only=True)"],
                           cwd=ROOT / "processor", env={**registry.env_for(), "HF_HUB_OFFLINE": "1"},
                           capture_output=True, text=True, timeout=120)
        checks["embedding model cached"] = "ok" if r.returncode == 0 else \
            "missing: run `./rag step processor.health` or one ingest outside the agent to download it"
    if sys.platform == "darwin" and registry.python_for("collector").exists():
        r = subprocess.run([str(registry.python_for("collector")), str(ROOT / "agent" / "sandbox.py"), "--check"],
                           capture_output=True, text=True)
        checks["agent sandbox"] = "ok" if r.returncode == 0 else "probe failed: python agent/sandbox.py --check"
    bad = {k: v for k, v in checks.items() if v != "ok"}
    for k, v in checks.items():
        print(f"{k:<24}{v}", file=sys.stderr)
    out({"step": "doctor", "status": "blocked" if bad else "ok", "reason": "; ".join(f"{k}: {v}" for k, v in bad.items()),
         "checks": checks, "contract_version": 1})
    return 2 if bad else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return cmd_status()
    if cmd == "steps":
        return cmd_steps()
    if cmd == "step":
        return cmd_step(rest)
    if cmd == "start":
        return cmd_start(rest)
    if cmd == "runs":
        return cmd_runs(rest)
    if cmd == "_bg":
        if len(rest) != 1:
            print("usage: ./rag _bg <run_id> (internal: started by ./rag start)", file=sys.stderr)
            return 2
        return bg.execute(rest[0])
    if cmd == "loop":
        from unified import loop
        return loop.main(rest)
    if cmd == "agent":
        os.execv("/bin/bash", ["bash", str(ROOT / "agent" / "run_agent.sh"), *rest])
    if cmd == "doctor":
        return cmd_doctor()
    if cmd in TOOLS:
        return cmd_tool(cmd, rest)
    print(f"unknown command {cmd!r}\n\n{__doc__.strip()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
