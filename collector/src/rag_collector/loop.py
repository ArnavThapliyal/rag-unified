"""Continuous collection: cycle after cycle until stopped.

Each cycle:
  1. collection segment
       --mode agent          an agent session (rag-unified's agent/run_agent.sh, or --agent-cmd): the Cline agent
                             chooses queries/gaps and drives collect.py (its supervisor restarts crashed sessions
                             and waits out search cooldowns)
       --mode deterministic  `steps collect --max-queries N` (no LLM; follows config/query_plan.json)
  2. universe-sync   (deterministic, never the agent's decision)
  3. export          (deterministic; repeated while more documents are waiting)
  4. pause, decided from the step results only (never from model text):
       ok/partial -> short pause; noop -> idle pause; retry_later -> search cooldown;
       blocked: network_unavailable -> retry after a pause, low disk or anything else -> stop;
       failed -> pause, stop after --max-failures consecutive failed cycles.

Stop it with Ctrl-C, SIGTERM, or `touch data/metadata/STOP` (checked between steps and during pauses; the
running step finishes first). Every cycle is logged to logs/continuous.jsonl.

    scripts/run_continuous.sh                          # agent mode, forever
    scripts/run_continuous.sh --mode deterministic --max-queries 10 --idle-min 60
    scripts/run_continuous.sh --once                   # one cycle (tests, smoke runs)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from rag_collector import paths
from rag_collector.stepkit import now_iso


class Loop:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self.log_path = paths.root() / "logs" / "continuous.jsonl"

    # --- control -------------------------------------------------------
    def on_signal(self, signum, _frame):
        self.stopping = True
        if signum == signal.SIGTERM and self.child and self.child.poll() is None:
            self.child.terminate()  # pipeline scripts stop gracefully on SIGTERM (common.graceful_sigterm)

    def stop_requested(self) -> bool:
        return self.stopping or paths.stop_file().exists()

    def say(self, msg: str) -> None:
        print(f"■ {msg}", flush=True)

    def record(self, entry: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_iso(), **entry}, default=str) + "\n")

    def pause(self, minutes: float, why: str) -> None:
        if minutes <= 0:
            return
        self.say(f"pausing {minutes:g} min ({why}); stop with Ctrl-C or touch {paths.stop_file()}")
        end = time.monotonic() + minutes * 60
        while time.monotonic() < end and not self.stop_requested():
            time.sleep(min(5, max(0.0, end - time.monotonic())))

    # --- running -------------------------------------------------------
    def run(self, cmd: list[str], capture: bool) -> tuple[int, dict | None]:
        self.child = subprocess.Popen(cmd, cwd=paths.root(), stdout=subprocess.PIPE if capture else None, text=True)
        out, _ = self.child.communicate()
        code, self.child = self.child.returncode, None
        if not capture:
            return code, None
        for line in reversed((out or "").strip().splitlines()):
            if line.startswith("{"):
                try:
                    return code, json.loads(line)
                except ValueError:
                    pass
        return code, None

    def step(self, name: str, *args: str) -> dict:
        code, res = self.run([paths.python(), "-m", "rag_collector.steps", name, *args], capture=True)
        res = res or {"step": name, "status": "failed", "reason": f"no JSON result (exit {code})"}
        self.say(f"{name}: {res.get('status')} {res.get('reason', '')}".rstrip())
        return res

    def segment(self) -> dict:
        if self.a.mode == "agent":
            agent = self.agent_cmd()
            if agent is None:
                return {"step": "agent", "status": "blocked",
                        "reason": "no agent runner (rag-unified's agent/run_agent.sh); use --agent-cmd or --mode deterministic"}
            self.say(f"collection segment: agent session ({agent})")
            code, _ = self.run(["bash", str(agent)], capture=False)
            # run_agent.sh exits 0 when the agent completed its plan, 130 when interrupted, else 1;
            # the collector's own run summaries say what happened
            if code == 130 or self.stopping:
                return {"step": "agent", "status": "failed", "reason": "interrupted"}
            last = self.last_run_summary()
            stop = (last or {}).get("stop_reason") or ""
            if stop in ("low_disk_space", "network_unavailable"):
                return {"step": "agent", "status": "blocked", "reason": stop}
            if stop == "search_engines_suspended":
                return {"step": "agent", "status": "retry_later", "reason": stop}
            new = (last or {}).get("new_records") or 0
            return {"step": "agent", "status": ("ok" if new else "noop") if code == 0 else "failed",
                    "reason": f"run_agent.sh exit {code}", "counts": {"new_records": new}}
        args = ["--max-queries", str(self.a.max_queries)] if self.a.max_queries else []
        return self.step("collect", *args)

    def agent_cmd(self) -> Path | None:
        """The agent session runner: --agent-cmd, else $RAG_AGENT_CMD, else rag-unified's agent/run_agent.sh."""
        for c in (self.a.agent_cmd, os.environ.get("RAG_AGENT_CMD"), paths.root().parent / "agent" / "run_agent.sh"):
            if c and Path(c).is_file():
                return Path(c).resolve()
        return None

    @staticmethod
    def last_run_summary() -> dict | None:
        runs = sorted((paths.root() / "logs" / "runs").glob("*.json"))
        for p in reversed(runs):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        return None

    def cooldown_min(self) -> float:
        code, res = self.run([paths.python(), str(paths.root() / "scripts" / "collect.py"), "--dry-run"], capture=True)
        try:
            return float(max(5, min(720, int(res["search_cooldown_min_left"]) + 1)))
        except (TypeError, KeyError, ValueError):
            return 30.0

    def cycle(self, n: int) -> tuple[str, float, str]:
        """Run one cycle; return (action, pause_min, why). action: continue | stop."""
        seg = self.segment()
        results = [seg]
        if not self.stop_requested():
            results.append(self.step("universe-sync"))
        for _ in range(20):  # export in batches while documents are waiting
            if self.stop_requested():
                break
            ex = self.step("export", "--max-files", str(self.a.export_batch))
            results.append(ex)
            if not ex.get("more_waiting") or ex.get("status") != "ok":
                break
        self.record({"cycle": n, "mode": self.a.mode,
                     "results": [{k: r.get(k) for k in ("step", "status", "reason", "counts", "batch_id")}
                                 for r in results]})
        if self.stop_requested():
            return "stop", 0, "stop requested"
        failed = [r for r in results if r.get("status") == "failed"]
        st, why = seg.get("status"), seg.get("reason") or ""
        if st == "blocked":
            if why == "network_unavailable":
                return "continue", self.a.network_retry_min, why
            return "stop", 0, f"blocked: {why}"
        if failed:
            return "failed", self.a.failure_pause_min, "; ".join(f"{r['step']}: {r.get('reason')}" for r in failed)
        if st == "retry_later":
            return "continue", self.cooldown_min(), why or "retry later"
        if st in ("ok", "partial"):
            return "continue", self.a.busy_pause_min, "collected new data"
        return "continue", self.a.idle_min, "nothing new"

    @staticmethod
    def decision(action: str, pause_min: float, why: str) -> None:
        """--once: the cycle's decision as the last stdout line, for a scheduler (rag-unified's ./rag loop)."""
        print(json.dumps({"loop_decision": action, "pause_min": pause_min, "why": why}), flush=True)

    def main(self) -> int:
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGTERM, self.on_signal)
        paths.stop_file().unlink(missing_ok=True)
        self.say(f"continuous collection, mode={self.a.mode} (log: {self.log_path.relative_to(paths.root())})")
        n, failures = 0, 0
        while not self.stop_requested():
            n += 1
            self.say(f"cycle {n} start")
            action, pause_min, why = self.cycle(n)
            failures = failures + 1 if action == "failed" else 0
            if action == "stop":
                self.say(f"stopping: {why}")
                if self.a.once:
                    self.decision(action, 0, why)
                return 0 if why == "stop requested" else 1
            if failures >= self.a.max_failures:
                self.say(f"stopping: {failures} failed cycles in a row ({why})")
                return 1
            if self.a.once:
                self.say(f"--once: done ({why})")
                self.decision(action, pause_min, why)
                return 1 if action == "failed" else 0
            self.pause(pause_min, why)
        self.say("stopped")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag-collector-loop", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("agent", "deterministic"), default="agent")
    ap.add_argument("--max-queries", type=int, default=10, help="deterministic mode: queries per cycle")
    ap.add_argument("--export-batch", type=int, default=500, help="documents per outbox batch")
    ap.add_argument("--idle-min", type=float, default=30, help="pause after a cycle that found nothing new")
    ap.add_argument("--busy-pause-min", type=float, default=1, help="pause after a productive cycle")
    ap.add_argument("--network-retry-min", type=float, default=10)
    ap.add_argument("--failure-pause-min", type=float, default=5)
    ap.add_argument("--max-failures", type=int, default=3)
    ap.add_argument("--once", action="store_true", help="run one cycle and exit (last line: the decision as JSON)")
    ap.add_argument("--agent-cmd", help="agent session runner (default: $RAG_AGENT_CMD or ../agent/run_agent.sh)")
    return Loop(ap.parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
