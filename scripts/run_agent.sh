#!/usr/bin/env bash
# Run Cline unattended (auto-approve all tools, no prompts) on the collection task.
#
#   scripts/run_agent.sh                         # default prompt: prompts/autonomous_collection.md
#   scripts/run_agent.sh prompts/my_task.md      # custom prompt file
#   CLINE_TIMEOUT=7200 scripts/run_agent.sh      # stop each session after 2 h (default: no limit)
#   CLINE_MODEL=google/gemma-4-26b-a4b scripts/run_agent.sh   # other LM Studio model
#   CLINE_MAX_RESTARTS=0 scripts/run_agent.sh    # don't auto-restart crashed sessions (default: 5)
#
# Uses LM Studio (localhost:1234) via Cline's lmstudio provider; LM Studio ignores the key.
# Extra Cline flags can be passed with CLINE_EXTRA_ARGS (e.g. "--thinking low").
#
# A session that ends abnormally is restarted with a fresh session; all progress lives in data/ and
# logs/, so the new session resumes the plan. Abnormal = non-zero exit (e.g. Cline's hub daemon
# restarted: "session not found") or a run that did not finish "completed" (e.g. Cline's loop
# detection aborts after 5 identical tool calls, which still exits 0).
# A completed run, Ctrl-C, low disk, or SearXNG/LM Studio being down stops the loop. If the session stopped
# because the search engines suspended SearXNG (search_engines_suspended), it waits 31 min and starts a new
# session (up to CLINE_MAX_COOLDOWNS times).
#
# Raw Cline events  -> logs/agent_sessions/<session>.jsonl
# Readable progress -> terminal + logs/agent_run.log (tool calls, errors, completion)
# Every script the agent runs tags its events with the session named in logs/agent_sessions/current.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROMPT_FILE="${1:-prompts/autonomous_collection.md}"
TIMEOUT="${CLINE_TIMEOUT:-}"   # empty = no limit (cline rejects --timeout 0)
MODEL="${CLINE_MODEL:-qwen/qwen3.8-27b}"
MAX_RESTARTS="${CLINE_MAX_RESTARTS:-5}"
MAX_COOLDOWNS="${CLINE_MAX_COOLDOWNS:-8}"   # search-engine suspensions to wait out (30 min each)
PY="$ROOT/.venv/bin/python"

[ -f "$PROMPT_FILE" ] || { echo "prompt file not found: $PROMPT_FILE" >&2; exit 2; }

preflight() {
  curl -sf -m 10 "http://localhost:8082/search?q=nse&format=json" >/dev/null \
    || { echo "SearXNG not reachable with JSON at localhost:8082" >&2; return 1; }
  curl -sf -m 5 "http://localhost:1234/v1/models" >/dev/null \
    || { echo "LM Studio not reachable at localhost:1234 (load the model first)" >&2; return 1; }
  local disk
  disk=$(cd scripts && "$PY" -c "import json, common; print(json.dumps(common.disk_status()))")
  echo "■ disk: $disk"
  case "$disk" in *'"level": "critical"'*) echo "Not enough free disk space — free some up first." >&2; return 1;; esac
}

interrupted=0
trap 'interrupted=1' INT
trap 'rm -f logs/agent_sessions/current' EXIT

TIMEOUT_ARGS=()
[ -n "$TIMEOUT" ] && TIMEOUT_ARGS=(--timeout "$TIMEOUT")
mkdir -p logs/agent_sessions
attempt=0
cooldowns=0
status=0
while :; do
  preflight || { status=1; break; }
  SESSION="agent-$(date +%Y%m%d-%H%M%S)"
  OUT="logs/agent_sessions/$SESSION.jsonl"
  export NSE_AGENT_SESSION="$SESSION"
  echo "$SESSION" > logs/agent_sessions/current  # Cline's hub keeps the first session's env; scripts read this
  "$PY" scripts/common.py log "agent session $SESSION started (model: $MODEL, prompt: $PROMPT_FILE, timeout: ${TIMEOUT:-none}, attempt $((attempt + 1)))" >/dev/null
  echo "■ $SESSION — raw log: $OUT"

  set +e
  # --auto-approve true is required: it overrides any approval settings saved in ~/.cline by
  # interactive use, which would otherwise make every tool call fail in this non-interactive mode.
  # shellcheck disable=SC2086
  npx --no-install cline --json -c "$ROOT" -P lmstudio -k lm-studio -m "$MODEL" --auto-approve true \
    "${TIMEOUT_ARGS[@]+"${TIMEOUT_ARGS[@]}"}" --retries 5 ${CLINE_EXTRA_ARGS:-} "$(cat "$PROMPT_FILE")" \
    | tee "$OUT" | "$PY" scripts/agent_progress.py
  status=${PIPESTATUS[0]}
  set -e

  finish=$(grep -o '"type":"run_result","finishReason":"[a-z_]*' "$OUT" | tail -1 | sed 's/.*"finishReason":"//' || true)
  "$PY" scripts/common.py log "agent session $SESSION ended (exit $status, finish: ${finish:-none})" >/dev/null
  [ "$interrupted" -eq 1 ] && { echo "■ interrupted — not restarting"; break; }
  if grep -q 'stop_reason[^a-z]*search_engines_suspended' "$OUT" && [ "$cooldowns" -lt "$MAX_COOLDOWNS" ]; then
    cooldowns=$((cooldowns + 1))
    echo "■ search engines suspended SearXNG — waiting 31 min, then a fresh session ($cooldowns/$MAX_COOLDOWNS)"
    "$PY" scripts/common.py log "search engines suspended: waiting 31 min before session restart ($cooldowns/$MAX_COOLDOWNS)" >/dev/null
    for _ in $(seq 186); do sleep 10; [ "$interrupted" -eq 1 ] && break; done
    [ "$interrupted" -eq 1 ] && { echo "■ interrupted — not restarting"; break; }
    continue
  fi
  [ "$status" -eq 0 ] && [ "$finish" = "completed" ] && break
  grep -q '"low_disk_space"' "$OUT" && { echo "■ stopped for low disk space — not restarting"; break; }
  attempt=$((attempt + 1))
  if [ "$attempt" -gt "$MAX_RESTARTS" ]; then
    echo "■ session failed $attempt times — giving up (see $OUT)"
    break
  fi
  reason=$(grep -o '"type":"error","message":"[^"]*' "$OUT" | tail -1 | sed 's/.*"message":"//' || true)
  [ -z "$reason" ] && [ -n "$finish" ] && reason="finish: $finish"
  echo "■ session ended abnormally (exit $status${reason:+: $reason}) — restarting in 15 s ($attempt/$MAX_RESTARTS)"
  "$PY" scripts/common.py log "auto-restart $attempt/$MAX_RESTARTS after exit $status${reason:+: $reason}" >/dev/null
  sleep 15
  [ "$interrupted" -eq 1 ] && { echo "■ interrupted — not restarting"; break; }
done

"$PY" scripts/status_report.py | head -16
exit "$status"
