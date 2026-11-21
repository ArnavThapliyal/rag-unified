#!/usr/bin/env bash
# Run Cline unattended (auto-approve all tools, no prompts) on the collection task.
#
#   scripts/run_agent.sh                         # default prompt: prompts/autonomous_collection.md
#   scripts/run_agent.sh prompts/my_task.md      # custom prompt file
#   CLINE_TIMEOUT=7200 scripts/run_agent.sh      # stop after 2 h (default: no limit)
#   CLINE_MODEL=google/gemma-4-26b-a4b scripts/run_agent.sh   # other LM Studio model
#
# Uses LM Studio (localhost:1234) via Cline's lmstudio provider; LM Studio ignores the key.
# Extra Cline flags can be passed with CLINE_EXTRA_ARGS (e.g. "--thinking low").
#
# Raw Cline events  -> logs/agent_sessions/<session>.jsonl
# Readable progress -> terminal + logs/agent_run.log (tool calls, errors, completion)
# Every script the agent runs tags its events with NSE_AGENT_SESSION=<session>.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROMPT_FILE="${1:-prompts/autonomous_collection.md}"
TIMEOUT="${CLINE_TIMEOUT:-}"   # empty = no limit (cline rejects --timeout 0)
MODEL="${CLINE_MODEL:-qwen/qwen3.8-27b}"
SESSION="agent-$(date +%Y%m%d-%H%M%S)"
OUT="logs/agent_sessions/$SESSION.jsonl"
PY="$ROOT/.venv/bin/python"

[ -f "$PROMPT_FILE" ] || { echo "prompt file not found: $PROMPT_FILE" >&2; exit 2; }
curl -sf -m 10 "http://localhost:8082/search?q=nse&format=json" >/dev/null \
  || { echo "SearXNG not reachable with JSON at localhost:8082" >&2; exit 1; }
curl -sf -m 5 "http://localhost:1234/v1/models" >/dev/null \
  || { echo "LM Studio not reachable at localhost:1234 (load the model first)" >&2; exit 1; }
disk=$(cd scripts && "$PY" -c "import json, common; print(json.dumps(common.disk_status()))")
echo "■ disk: $disk"
case "$disk" in *'"level": "critical"'*) echo "Not enough free disk space — free some up first." >&2; exit 1;; esac

mkdir -p logs/agent_sessions
export NSE_AGENT_SESSION="$SESSION"
"$PY" scripts/common.py log "agent session $SESSION started (model: $MODEL, prompt: $PROMPT_FILE, timeout: ${TIMEOUT:-none})" >/dev/null
echo "■ $SESSION — raw log: $OUT"

set +e
TIMEOUT_ARGS=()
[ -n "$TIMEOUT" ] && TIMEOUT_ARGS=(--timeout "$TIMEOUT")
# shellcheck disable=SC2086
# --auto-approve true is required: it overrides any approval settings saved in ~/.cline by
# interactive use, which would otherwise make every tool call fail in this non-interactive mode.
npx --no-install cline --json -c "$ROOT" -P lmstudio -k lm-studio -m "$MODEL" --auto-approve true \
  "${TIMEOUT_ARGS[@]+"${TIMEOUT_ARGS[@]}"}" --retries 5 ${CLINE_EXTRA_ARGS:-} "$(cat "$PROMPT_FILE")" \
  | tee "$OUT" | "$PY" scripts/agent_progress.py
status=${PIPESTATUS[0]}
set -e

"$PY" scripts/common.py log "agent session $SESSION ended (exit $status)" >/dev/null
"$PY" scripts/status_report.py | head -14
exit "$status"
