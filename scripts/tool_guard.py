"""Allowlist for the collecting agent's tool calls. Cline runs it before every tool call
(.cline/hooks/PreToolUse.sh) with the call as JSON on stdin.

Allowed without asking:
- run_commands: `.venv/bin/python scripts/<agent script>.py …` (see SCRIPTS), `git status`,
  optionally after `cd <project root> &&`, piped into head/tail/wc/grep/sort/uniq/cut.
- read_files outside source code and raw data; editor/apply_patch only on files in config/.
- skill lookups, handoff notes and finishing the task.
Everything else (curl, rm, ls, python -c, background commands, web fetches, edits outside config/,
…) needs the user's approval. Nobody approves an unattended run, so such a call is not executed:
it is rewritten into a harmless one that tells the agent it was blocked, and is logged
(`tool_denied` event) and filed in logs/issues.jsonl for the user to review.

A Cline hook may not simply cancel a call (cancel ends the whole run), hence the rewrite.

  echo '{"tool_call": {"name": "run_commands", "input": {"commands": ["curl x"]}}}' | python scripts/tool_guard.py
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

NOTICE = ROOT / ".cline" / "BLOCKED_READ.txt"
PYTHONS = {".venv/bin/python", str(ROOT / ".venv/bin/python")}


def _collect(args):
    return None if "--dry-run" in args else "collect.py runs only with --dry-run; start runs with scripts/job.py start"


def _job(args):
    return None if args[:1] and args[0] in ("start", "wait", "status", "stop") else "job.py takes start|wait|status|stop"


def _report(args):
    return "closing issues is for the user" if "--close" in args else None


def _searxng(args):
    return None if args and set(args) <= {"--engines", "--list-cache"} else \
        "searxng_client.py only with --engines or --list-cache; searches go through collect.py"


def _common(args):
    return None if args[:1] == ["log"] else "common.py only as: common.py log \"<message>\""


# agent-facing scripts -> argument check (None = any arguments)
SCRIPTS = {
    "collect.py": _collect, "job.py": _job, "status_report.py": None, "report_issue.py": _report,
    "inspect_artifact.py": None, "audit_dataset.py": None, "build_metadata.py": None,
    "searxng_client.py": _searxng, "common.py": _common,
}
FILTERS = {"head", "tail", "wc", "grep", "sort", "uniq", "cut"}
SEPARATORS = {"&&", "||", ";", "|"}
NO_READ = ("scripts/", "tests/", "vendor/", ".venv/", "node_modules/", "data/raw/", "data/quarantine/",
           "data/search_cache/", "data/processed/parsed/", "data/processed/ocr/", "logs/agent_sessions/",
           ".git/", ".cline/hooks/")


def rel(path: str) -> str | None:
    """Path relative to the project root, or None if it is outside the project."""
    p = Path(path).expanduser()
    p = (p if p.is_absolute() else ROOT / p).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else None


def check_script(words: list[str]) -> str | None:
    if words[0] in PYTHONS:
        if len(words) < 2 or words[1].startswith("-"):
            return "python only runs project scripts (no -c, -m or other flags)"
        r = rel(words[1])
        name = Path(words[1]).name
        if r != f"scripts/{name}" or name not in SCRIPTS:
            return f"not an agent script: {words[1]} (allowed: {', '.join(sorted(SCRIPTS))})"
        rule = SCRIPTS[name]
        return rule(words[2:]) if rule else None
    if words[:2] == ["git", "status"] and set(words[2:]) <= {"-s", "--short", "--porcelain"}:
        return None
    if words[0] == "cd" and len(words) == 2 and rel(words[1]) == ".":
        return None
    return f"command not allowed: {words[0]}"


def check_filter(words: list[str]) -> str | None:
    if words[0] not in FILTERS:
        return f"only {', '.join(sorted(FILTERS))} may follow a pipe"
    positional = [w for w in words[1:] if not w.startswith("-") and not w.isdigit()]
    if words[0] == "grep":
        positional = positional[1:]  # the pattern
    if words[0] == "cut":
        positional = []
    return f"{words[0]} may only filter piped output, not read files" if positional else None


def check_command(cmd: str) -> str | None:
    if re.search(r"`|\$\(|\$\{|<\(|>\(", cmd):
        return "command substitution is not allowed"
    cmd = re.sub(r"\s2>&1\b", " ", cmd).replace("\n", " ; ")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:
        return f"cannot parse command: {e}"
    segments, cur, piped = [], [], False
    for t in tokens + [";"]:
        if t in SEPARATORS:
            if not cur:
                return "empty command segment"
            segments.append((cur, piped))
            cur, piped = [], t == "|"
        elif set(t) <= set("&|;<>()"):
            return f"'{t}' (redirection/background) is not allowed"
        else:
            cur.append(t)
    for words, after_pipe in segments:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            return "environment assignments are not allowed"
        why = check_filter(words) if after_pipe else check_script(words)
        if why:
            return why
    return None


def patch_paths(patch: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M)]


def edit_ok(path: str) -> str | None:
    r = rel(path)
    return None if r and r.startswith("config/") else f"edits are only allowed in config/ (not {path})"


def check(tool: str, inp: dict) -> str | None:
    """None if the call may run, else the reason it needs approval."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "run_commands":
        cmds = inp.get("commands") or []
        if not isinstance(cmds, list) or not cmds:
            return "no commands given"
        for c in cmds:
            if isinstance(c, dict):
                c = shlex.join([str(c.get("command", ""))] + [str(x) for x in c.get("args") or []])
            why = check_command(str(c))
            if why:
                return why
        return None
    if tool == "read_files":
        files = inp.get("files") or inp.get("paths") or []
        for f in files if isinstance(files, list) else [files]:
            path = f.get("path", "") if isinstance(f, dict) else str(f)
            r = rel(path)
            if r is None or r.startswith(NO_READ) or r == ".env":
                return f"reading {path} is not allowed (use the scripts' JSON output or inspect_artifact.py)"
        return None
    if tool == "editor":
        return edit_ok(str(inp.get("path", "")))
    if tool == "apply_patch":
        paths = patch_paths(str(inp.get("input", "")))
        if not paths:
            return "patch names no files"
        return next((w for w in map(edit_ok, paths) if w), None)
    if tool in ("get_skill", "list_skills", "skills", "save_handoff", "list_agent_presets", "submit_and_exit",
                "get_background_command"):
        return None
    if tool == "ask_question":
        return "nobody can answer: decide yourself and log the decision"
    return f"tool {tool} needs the user's approval"


def blocked_input(tool: str, inp: dict, reason: str) -> dict:
    """A harmless replacement call that tells the agent why the original was not run."""
    msg = f"BLOCKED by tool guard (needs the user's approval): {reason}. Allowed: .venv/bin/python scripts/<agent script>.py, git status."
    if tool == "run_commands":
        return {"commands": [f"printf '%s\\n' {shlex.quote(msg)}"]}
    if tool == "read_files":
        return {"files": [{"path": str(NOTICE)}]}
    if tool in ("editor", "apply_patch"):
        return {"path": "/dev/null/blocked-by-tool-guard", "old_text": "x", "new_text": "x"} if tool == "editor" \
            else {"input": "*** Begin Patch\n*** Update File: /dev/null/blocked-by-tool-guard\n*** End Patch"}
    return {"blocked_by_tool_guard": msg}


def record(tool: str, inp: dict, reason: str) -> None:
    try:
        import report_issue
        from common import log
        log(f"tool guard blocked {tool}: {reason}", level="WARN", kind="tool_denied", tool=tool,
            reason=reason, input=json.dumps(inp)[:500])
        report_issue.report("other", f"tool guard blocked {tool}: {reason}"[:200], [json.dumps(inp)[:300]])
    except Exception:  # logging must never break the hook
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        call = payload.get("tool_call") or {}
        tool = call.get("name") or (payload.get("preToolUse") or {}).get("toolName") or ""
        inp = call.get("input")
        if inp is None:
            inp = (payload.get("preToolUse") or {}).get("parameters") or {}
        reason = check(tool, inp)
    except Exception as e:  # fail closed
        tool, inp, reason = "unknown", {}, f"tool guard error: {type(e).__name__}: {e}"
    if reason is None:
        print(json.dumps({}))
        return 0
    record(tool, inp, reason)
    print(json.dumps({"overrideInput": blocked_input(tool, inp, reason),
                      "context": f"Your {tool} call was NOT executed: {reason}. Don't retry it; carry on with "
                                 "the allowed scripts, or report the problem with scripts/report_issue.py."}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
