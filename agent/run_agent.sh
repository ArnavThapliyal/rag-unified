#!/usr/bin/env bash
# Run the rag-unified agent: Cline unattended (auto-approve all tools, no prompts), working directory = the
# repo root, every tool call checked by agent/tool_guard.py (.cline/hooks/PreToolUse.sh). Also: ./rag agent.
#
#   agent/run_agent.sh                           # default prompt: prompts/agent.md
#   agent/run_agent.sh prompts/my_task.md        # custom prompt file
#   CLINE_TIMEOUT=7200 agent/run_agent.sh        # stop each session after 2 h (default: no limit)
#   CLINE_MODEL=google/gemma-4-26b-a4b agent/run_agent.sh   # other LM Studio model (or CLINE_MODEL= in collector/.env)
#   CLINE_MAX_RESTARTS=0 agent/run_agent.sh      # don't auto-restart crashed sessions (default: 5)
#   CLINE_SANDBOX=0 agent/run_agent.sh           # run without the OS sandbox (default on macOS: on)
#
# OS sandbox (macOS sandbox-exec, agent/sandbox.py): Cline and everything it starts cannot read or write your
# home directory outside the project, ~/.cline and the runtimes; ~/.ssh, ~/.config/rag-collector
# (search keys), the .env files and the keychain are denied. Agent sessions therefore search via SearXNG only
# (CLINE_SANDBOX_ALLOW_KEYS=1 lets the sandbox read the keys file). The profile is checked before each session.
#
# Uses LM Studio (localhost:1234) via Cline's lmstudio provider; LM Studio ignores the key.
# Extra Cline flags can be passed with CLINE_EXTRA_ARGS (e.g. "--thinking low").
#
# What happens after a session is decided by collector/scripts/agent_supervisor.py from structured state only: Cline's
# exit status, the finishReason of Cline's own run_result event, and the run summaries (collector/logs/runs/*.json)
# that collect.py wrote during the session. Never from the model's text.
# - Abnormal end (non-zero exit, e.g. Cline's hub daemon restarted: "session not found"; or no "completed"
#   finish, e.g. Cline's loop detection aborting, which still exits 0): a fresh session, up to
#   CLINE_MAX_RESTARTS times, CLINE_RESTART_DELAY s apart (default 15). All progress lives in data/ and logs/.
# - Last collection run stopped for search_engines_suspended: wait until the first engine is usable again
#   (5 min – 12 h), then a fresh session (up to CLINE_MAX_COOLDOWNS times).
# - Completed, Ctrl-C, low_disk_space, network_unavailable, or SearXNG/LM Studio down: the loop stops.
#
# Raw Cline events  -> collector/logs/agent_sessions/<session>.jsonl
# Readable progress -> terminal + collector/logs/agent_run.log (tool calls, errors, completion)
# Every script the agent runs tags its events with the session named in collector/logs/agent_sessions/current.
# Environment variables named like *API_KEY*, *TOKEN*, *SECRET*, *PASSWORD* are removed before Cline starts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COL="$ROOT/collector"
cd "$ROOT"
PROMPT_FILE="${1:-prompts/agent.md}"
TIMEOUT="${CLINE_TIMEOUT:-}"   # empty = no limit (cline rejects --timeout 0)
# CLINE_MODEL: environment, else the CLINE_MODEL line of .env (only that line is read: no secrets), else qwen
if [ -z "${CLINE_MODEL:-}" ] && [ -f "$COL/.env" ]; then
  CLINE_MODEL=$(grep -E '^CLINE_MODEL=' "$COL/.env" | tail -1 | cut -d= -f2- | tr -d "\"' " || true)
fi
MODEL="${CLINE_MODEL:-qwen/qwen3.8-27b}"
MAX_RESTARTS="${CLINE_MAX_RESTARTS:-5}"
MAX_COOLDOWNS="${CLINE_MAX_COOLDOWNS:-12}"  # search-engine suspensions to wait out
RESTART_DELAY="${CLINE_RESTART_DELAY:-15}"
PY="${NSE_PYTHON:-$COL/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
CLINE_BIN="$COL/node_modules/.bin/cline"   # not npx: no npm registry check, and it runs inside the sandbox
SANDBOX_MODE="${CLINE_SANDBOX:-auto}"
SESSIONS="$COL/logs/agent_sessions"   # where the collector's scripts look for the running session

[ -f "$PROMPT_FILE" ] || { echo "prompt file not found: $PROMPT_FILE" >&2; exit 2; }

# Keys and tokens from your shell never reach the agent: Cline talks to LM Studio, which needs none.
for v in $(env | grep -oE '^[A-Za-z_][A-Za-z0-9_]*(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Za-z0-9_]*=' | tr -d = || true); do
  unset "$v"
done
# Cline's hub daemon keeps the environment it was first started with; restart it so it has none of them.
pkill -f -- "--cline-hub-daemon --cwd $ROOT" 2>/dev/null || true

preflight() {
  (cd "$COL" && "$PY" scripts/web_search.py --check >/dev/null) \
    || { echo "No search provider usable (SearXNG down at localhost:8082 and no Serper/Brave key): ./rag web_search --check" >&2; return 1; }
  curl -sf -m 5 "http://localhost:1234/v1/models" >/dev/null \
    || { echo "LM Studio not reachable at localhost:1234 (load the model first)" >&2; return 1; }
  local disk
  disk=$(cd "$COL/scripts" && "$PY" -c "import json, common; print(json.dumps(common.disk_status()))")
  echo "■ disk: $disk"
  case "$disk" in *'"level": "critical"'*) echo "Not enough free disk space — free some up first." >&2; return 1;; esac
}

SANDBOX_CMD=()
if [ "$SANDBOX_MODE" != "0" ]; then
  if [ "$(uname)" = "Darwin" ] && command -v sandbox-exec >/dev/null; then
    mkdir -p "$SESSIONS"
    keys=()
    [ "${CLINE_SANDBOX_ALLOW_KEYS:-0}" = "1" ] && keys=(--allow-keys)
    "$PY" agent/sandbox.py --out "$SESSIONS/sandbox.sb" "${keys[@]+"${keys[@]}"}" \
      || { echo "could not build the sandbox profile (CLINE_SANDBOX=0 runs without it)" >&2; exit 2; }
    if ! "$PY" agent/sandbox.py --check "${keys[@]+"${keys[@]}"}" >/dev/null; then
      echo "sandbox probe failed: refusing to start (CLINE_SANDBOX=0 runs without it)" >&2; exit 2
    fi
    SANDBOX_CMD=(sandbox-exec -f "$SESSIONS/sandbox.sb")
    # the Hugging Face cache is read-only in the sandbox: use cached models only, never try (and fail) to write
    export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
    echo "■ OS sandbox on: home directory, keys, .env and keychain are out of the agent's reach"
  elif [ "$SANDBOX_MODE" = "1" ]; then
    echo "CLINE_SANDBOX=1 but sandbox-exec is not available on this system" >&2; exit 2
  else
    echo "■ OS sandbox unavailable on this system: the tool guard is the only boundary" >&2
  fi
fi

interrupted=0
trap 'interrupted=1' INT TERM
trap 'rm -f "$SESSIONS/current" "$SESSIONS/.current.$$"' EXIT

TIMEOUT_ARGS=()
[ -n "$TIMEOUT" ] && TIMEOUT_ARGS=(--timeout "$TIMEOUT")
mkdir -p "$SESSIONS"
attempt=0
cooldowns=0
status=0
while :; do
  preflight || { status=1; break; }
  SESSION="agent-$(date +%Y%m%d-%H%M%S)"
  OUT="$SESSIONS/$SESSION.jsonl"
  export NSE_AGENT_SESSION="$SESSION"
  printf '%s\n' "$SESSION" > "$SESSIONS/.current.$$" \
    && mv -f "$SESSIONS/.current.$$" "$SESSIONS/current"  # Cline's hub keeps the first session's env; scripts read this
  "$PY" "$COL/scripts/common.py" log "agent session $SESSION started (model: $MODEL, prompt: $PROMPT_FILE, timeout: ${TIMEOUT:-none}, attempt $((attempt + 1)))" >/dev/null
  echo "■ $SESSION — raw log: $OUT"

  set +e
  # --auto-approve true is required: it overrides any approval settings saved in ~/.cline by
  # interactive use, which would otherwise make every tool call fail in this non-interactive mode.
  # shellcheck disable=SC2086
  "${SANDBOX_CMD[@]+"${SANDBOX_CMD[@]}"}" "$CLINE_BIN" --json -c "$ROOT" -P lmstudio -k lm-studio -m "$MODEL" --auto-approve true \
    "${TIMEOUT_ARGS[@]+"${TIMEOUT_ARGS[@]}"}" --retries 5 ${CLINE_EXTRA_ARGS:-} "$(cat "$PROMPT_FILE")" \
    | tee "$OUT" | "$PY" "$COL/scripts/agent_progress.py"
  status=${PIPESTATUS[0]}
  set -e

  "$PY" "$COL/scripts/common.py" log "agent session $SESSION ended (exit $status)" >/dev/null
  intr_flag=()
  [ "$interrupted" -eq 1 ] && intr_flag=(--interrupted)
  decision=$("$PY" "$COL/scripts/agent_supervisor.py" decide --session "$SESSION" --log "$OUT" --exit-code "$status" \
      --attempt "$attempt" --max-restarts "$MAX_RESTARTS" --cooldowns "$cooldowns" --max-cooldowns "$MAX_COOLDOWNS" \
      "${intr_flag[@]+"${intr_flag[@]}"}") \
    || decision='{"action": "stop", "reason": "supervisor failed", "wait_min": 0}'
  IFS=$'\t' read -r action reason mins < <(printf '%s' "$decision" | "$PY" -c \
    'import json,sys; d=json.load(sys.stdin); print(d["action"], d["reason"].replace("\t", " "), d["wait_min"], sep="\t")')
  "$PY" "$COL/scripts/common.py" log "agent supervisor: $action ($reason)" >/dev/null
  case "$action" in
    cooldown)
      cooldowns=$((cooldowns + 1))
      echo "■ search engines benched — waiting $mins min, then a fresh session ($cooldowns/$MAX_COOLDOWNS)"
      for _ in $(seq $((mins * 6))); do sleep 10; [ "$interrupted" -eq 1 ] && break; done
      [ "$interrupted" -eq 1 ] && { echo "■ interrupted — not restarting"; break; }
      ;;
    restart)
      attempt=$((attempt + 1))
      echo "■ session ended abnormally ($reason) — restarting in ${RESTART_DELAY} s ($attempt/$MAX_RESTARTS)"
      sleep "$RESTART_DELAY"
      [ "$interrupted" -eq 1 ] && { echo "■ interrupted — not restarting"; break; }
      ;;
    *)
      echo "■ stopping: $reason"
      case "$reason" in completed) status=0 ;; interrupted) status=130 ;; *) [ "$status" -eq 0 ] && status=1 ;; esac
      break
      ;;
  esac
done

{ (cd "$COL" && "$PY" scripts/status_report.py) | head -16; } || true   # a report problem (or SIGPIPE from head) never changes the exit code
exit "$status"
