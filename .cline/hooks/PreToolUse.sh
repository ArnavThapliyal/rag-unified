#!/usr/bin/env bash
# Cline PreToolUse hook: every tool call of the agent is checked by agent/tool_guard.py.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec "$ROOT/collector/.venv/bin/python" "$ROOT/agent/tool_guard.py"
