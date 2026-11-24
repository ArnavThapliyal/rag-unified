"""Every command the agent-facing files tell the agent to run is accepted by the tool guard, and nothing in them
points at the old project names, the per-repo agent layout or steps the agent may not run.

Sections whose heading mentions the user (e.g. "User commands (blocked for the agent)") document the user's
commands and are skipped by the allow check.
"""
import re
from pathlib import Path

import pytest

import tool_guard as g
from unified import registry

ROOT = Path(g.ROOT)
AGENT_FILES = ["prompts/agent.md", ".cline/rules/01-rag-agent.md", "soul.md",
               *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "skills").glob("*/SKILL.md"))]
PLACEHOLDERS = {"year": "2007", "kind": "other", "path": "data/raw/priority_2007/x.pdf", "host": "example.com",
                "domain": "example.com", "record_id": "abc123", "collect args": "--max-queries 8",
                "what is wrong, with counts": "3 docs gave 0 rows", "what happened": "cycle 1 done",
                "N": "3", "document kind": "orders", "check": "dup", "count": "2", "step": "processor.status",
                "run_id": "20261124-101010-processor.ingest", "path under data/": "data/raw/priority_2007/x.pdf",
                "message": "cycle 1 done", "id": "20261124-000000"}


NO_ARG_COMMANDS = {"status", "steps", "runs", "audit_dataset", "build_metadata"}
USER_COMMANDS = {"loop"}


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
        for cmd in re.findall(r"`(\./rag [^`]+)`", text):
            words = cmd.split()
            if len(words) == 2 and words[1] not in NO_ARG_COMMANDS:
                continue  # a reference to a tool (`./rag report_issue`), not a command to run
            if words[1] in USER_COMMANDS:
                continue  # the user's continuous mode, mentioned so the agent knows it runs it
            yield f, cmd


CMDS = list(agent_commands())


def test_prompts_advertise_commands():
    assert len(CMDS) > 30  # the extraction found the prompt commands
    for must in ("./rag status", "./rag job start --max-queries 8", "./rag start processor.ingest --next",
                 "./rag runs <run_id>"):
        assert any(c.startswith(must) for _, c in CMDS), must


@pytest.mark.parametrize("src,cmd", CMDS, ids=[f"{s}:{c[:60]}" for s, c in CMDS])
def test_every_advertised_command_passes_the_guard(src, cmd):
    filled = fill(cmd)
    assert g.check("run_commands", {"commands": [filled]}) is None, f"{src}: {filled}"


def test_steps_the_prompts_name_exist_with_the_access_they_claim():
    text = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in AGENT_FILES)
    named = set(re.findall(r"\b((?:collector|processor)\.[a-z][a-z-]+[a-z])\b", text))
    assert named, "no step names found"
    for name in named:
        assert name in registry.steps(), f"prompts name {name}, which is not in unified/steps.toml"
    for m in re.finditer(r"`\./rag step ((?:collector|processor)\.[a-z-]+)", text):
        assert registry.get(m.group(1))["access"] == "agent", f"prompts run {m.group(1)} in the foreground"


def test_old_forms_are_gone_from_the_agent_files():
    for f in AGENT_FILES:
        text = agent_sections((ROOT / f).read_text(encoding="utf-8"))
        assert ".venv/bin/python scripts/" not in text, f"{f}: old direct form"
        assert "scripts/steps.py" not in text and "run_continuous.sh" not in text, f
        assert "01-nse-research" not in text and "autonomous_collection" not in text, f


@pytest.mark.parametrize("f", AGENT_FILES + [".clineignore", "README.md", "ARCHITECTURE.md"])
def test_no_stale_names(f):
    text = (ROOT / f).read_text(encoding="utf-8")
    for stale in ("nse-delisted-comp", "nse-unified-pipeline", "company_pipeline", "company-pipeline"):
        assert stale not in text, f"{f} still mentions {stale}"
    if f in AGENT_FILES:
        assert "polite_fetcher" not in agent_sections(text), f"{f} tells the agent to run the fetcher"


def test_cline_reads_the_shared_skills():
    link = ROOT / ".cline" / "skills"
    assert link.is_symlink() and link.resolve() == (ROOT / "skills").resolve()
    assert sorted(p.name for p in link.iterdir()) == sorted(p.name for p in (ROOT / "skills").iterdir())
