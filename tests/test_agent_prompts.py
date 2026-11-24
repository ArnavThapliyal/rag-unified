"""Every command the agent-facing files tell the agent to run is accepted by the tool guard, and nothing in them
points at the old project names, the removed company_pipeline package or steps the agent may not run.

Sections whose heading mentions the user (e.g. "User commands (blocked for the agent)", "the user's debugging
tools") document the user's commands and are skipped by the allow check.
"""
import re
from pathlib import Path

import pytest

import tool_guard as g

ROOT = Path(g.ROOT)
AGENT_FILES = ["prompts/autonomous_collection.md", ".cline/rules/01-nse-research.md", "soul.md",
               *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "skills").glob("*/SKILL.md"))]
PLACEHOLDERS = {"year": "2007", "kind": "other", "path": "data/raw/priority_2007/x.pdf", "host": "example.com",
                "domain": "example.com", "record_id": "abc123", "collect.py args": "--max-queries 8",
                "what is wrong, with counts": "3 docs gave 0 rows", "what happened": "cycle 1 done",
                "N": "3", "document kind": "orders", "check": "dup", "count": "2"}


def agent_sections(text: str) -> str:
    """The file without the sections that document the user's own commands."""
    out, skip, level = [], False, 0
    for line in text.splitlines():
        m = re.match(r"(#+)\s+(.*)", line)
        if m:
            if skip and len(m.group(1)) <= level:
                skip = False
            if not skip and re.search(r"\buser", m.group(2), re.I):
                skip, level = True, len(m.group(1))
        if not skip:
            out.append(line)
    return "\n".join(out)


def fill(cmd: str) -> str:
    def sub(m):
        return PLACEHOLDERS.get(m.group(1), "x")
    cmd = re.sub(r"<([^<>]+)>", sub, cmd)
    cmd = cmd.replace('"..."', '"delisted companies 2007"').replace("…", "").strip()
    return re.sub(r"\s+", " ", cmd)


def agent_commands():
    for f in AGENT_FILES:
        text = agent_sections((ROOT / f).read_text(encoding="utf-8"))
        for cmd in re.findall(r"`(\.venv/bin/python scripts/[^`]+)`", text):
            # descriptions, not commands: the generic `scripts/<script>.py …` and the guard's own rewrite
            if "<script>" not in cmd and "agent_exec.py" not in cmd:
                yield f, cmd


CMDS = list(agent_commands())


def test_prompts_advertise_commands():
    assert len(CMDS) > 15  # the extraction found the prompt commands
    assert any("scripts/steps.py status" in c for _, c in CMDS)


@pytest.mark.parametrize("src,cmd", CMDS, ids=[f"{s}:{c[:50]}" for s, c in CMDS])
def test_every_advertised_command_passes_the_guard(src, cmd):
    filled = fill(cmd)
    assert g.check("run_commands", {"commands": [filled]}) is None, f"{src}: {filled}"


def test_step_cli_for_the_agent():
    ok = [".venv/bin/python scripts/steps.py status", ".venv/bin/python scripts/steps.py list",
          ".venv/bin/python scripts/steps.py audit", ".venv/bin/python scripts/steps.py status | head -40"]
    for c in ok:
        assert g.check("run_commands", {"commands": [c]}) is None, c
    for c in [".venv/bin/python scripts/steps.py export", ".venv/bin/python scripts/steps.py universe-sync",
              ".venv/bin/python scripts/steps.py collect --max-queries 5", ".venv/bin/python scripts/steps.py fetch",
              ".venv/bin/python scripts/steps.py status --json", ".venv/bin/python -m rag_collector.steps status"]:
        assert g.check("run_commands", {"commands": [c]}), c
    for p in ["src/rag_collector/steps.py", "data/export/20260101-000000/manifest.json"]:
        assert g.check("read_files", {"files": [{"path": p}]}), p


@pytest.mark.parametrize("f", AGENT_FILES + [".clineignore", "instructions.md", "README.md"])
def test_no_stale_names(f):
    text = (ROOT / f).read_text(encoding="utf-8")
    for stale in ("nse-delisted-comp", "nse-unified-pipeline", "company_pipeline", "company-pipeline"):
        assert stale not in text, f"{f} still mentions {stale}"
    if f in AGENT_FILES:
        assert "polite_fetcher.py --" not in agent_sections(text), f"{f} tells the agent to run the fetcher"
