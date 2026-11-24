#!/usr/bin/env bash
# rag-unified health check: both halves' own checks, the agent boundary, the unified layer, and a smoke run.
#   scripts/check.sh             # everything, incl. the services (SearXNG, LM Studio, Cline)
#   scripts/check.sh --offline   # skip the services
#   scripts/check.sh --e2e       # also: export 3 real documents to a temporary outbox and ingest them into a
#                                #   temporary LanceDB with the real models (a few minutes; nothing real is touched)
# Exit code is non-zero if any check fails.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CPY="$ROOT/collector/.venv/bin/python"
PPY="$ROOT/processor/.venv/bin/python"
OFFLINE=0; E2E=0
for a in "$@"; do case "$a" in --offline) OFFLINE=1 ;; --e2e) E2E=1 ;; *) echo "unknown option $a" >&2; exit 2 ;; esac; done
fail=0
step() { printf '%-30s' "$1"; }
ok() { echo "ok${1:+  ($1)}"; }
bad() { echo "FAIL${1:+  ($1)}"; fail=1; }

for py in "$CPY" "$PPY"; do [ -x "$py" ] || { echo "missing $py: see README.md (Install)" >&2; exit 2; }; done

echo "── collector (collector/scripts/check.sh) ──"
collector/scripts/check.sh --offline | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] || fail=1

echo "── processor ──"
step "  pytest"
out=$(cd processor && VIRTUAL_ENV= "$PPY" -m pytest -q 2>&1) && ok "$(echo "$out" | tail -1)" || { bad; echo "$out" | tail -30; }
step "  ruff"
out=$(cd processor && VIRTUAL_ENV= "$PPY" -m ruff check . 2>&1) && ok || { bad; echo "$out" | tail -20; }

echo "── unified + agent ──"
step "  pyflakes"
out=$("$CPY" -m pyflakes unified agent tests 2>&1) && ok || { bad; echo "$out"; }
step "  bash syntax"
bash -n rag agent/run_agent.sh .cline/hooks/PreToolUse.sh scripts/check.sh && ok || bad
step "  stepkit is one file"
cmp -s collector/src/rag_collector/stepkit.py processor/pipeline/stepkit.py && ok || bad "the two stepkit.py differ"
step "  pytest"
out=$("$CPY" -m pytest -q tests 2>&1) && ok "$(echo "$out" | tail -1)" || { bad; echo "$out" | tail -30; }
if [ "$(uname)" = "Darwin" ]; then
  step "  agent sandbox probe"
  out=$("$CPY" agent/sandbox.py --check 2>&1) && ok || { bad; echo "$out"; }
fi
step "  no stale references"
hits=$(git grep -Il "nse-delisted-comp\|nse-unified-pipeline\|company_pipeline\|01-nse-research\|autonomous_collection\|scripts/run_agent.sh\|scripts/tool_guard.py\|scripts/agent_sandbox.py" \
  -- ':!collector/data' ':!collector/logs' ':!collector/docs' ':!scripts/check.sh' ':!collector/scripts/check.sh' \
  ':!AUDIT.md' ':!tests/agent/test_agent_prompts.py' ':!processor/pipeline/quant' ':!processor/tests' 2>/dev/null)
[ -z "$hits" ] && ok || bad "$(echo $hits)"

echo "── smoke ──"
step "  ./rag status"
out=$(./rag status 2>/dev/null) && echo "$out" | "$CPY" -c "import json,sys; d=json.load(sys.stdin); assert d['collector']['status'] in ('ok','noop') and d['processor']['status'] in ('ok','noop'), d" 2>/dev/null \
  && ok || bad "$(echo "$out" | head -c 300)"
step "  export --dry-run"
out=$(./rag step collector.export --dry-run 2>/dev/null) && ok "$(echo "$out" | "$CPY" -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('reason',''))")" || bad "$out"
step "  processor health"
out=$(./rag step processor.health 2>/dev/null) && ok || bad "$(echo "$out" | head -c 300)"

if [ "$E2E" = 1 ]; then
  echo "── end to end (temporary outbox + LanceDB) ──"
  T=$(mktemp -d "${TMPDIR:-/tmp}/rag_e2e.XXXXXX")
  export RAG_COLLECTOR_OUTBOX="$T/outbox" COLLECTOR_OUTBOX="$T/outbox" DATA_DIR="$T/processor_data" RAG_BG_DIR="$T/bg"
  step "  collector.export (3 docs)"
  out=$(./rag step collector.export --max-files 3 2>/dev/null) && ok "$(echo "$out" | "$CPY" -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('batch_id'), d['counts'])")" || bad "$out"
  step "  processor.ingest --next"
  out=$(./rag step processor.ingest --next 2>/dev/null) \
    && echo "$out" | "$CPY" -c "import json,sys; d=json.load(sys.stdin); c=d['counts']; assert d['status']=='ok' and c.get('chunks_written',0)>0, d; print(d['status'], c)" \
    || bad "$(echo "$out" | head -c 400)"
  step "  ingest again is a noop"
  out=$(./rag step processor.ingest --next 2>/dev/null); echo "$out" | grep -q '"status": "noop"' && ok || bad "$(echo "$out" | head -c 300)"
  step "  quant-sync"
  out=$(./rag step processor.quant-sync 2>/dev/null) && ok "$(echo "$out" | "$CPY" -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('reason',''))")" || bad "$(echo "$out" | head -c 300)"
  rm -rf "$T"
fi

if [ "$OFFLINE" = 0 ]; then
  echo "── services ──"
  step "  ./rag doctor"
  out=$(./rag doctor 2>&1 >/dev/null) && ok || { bad; echo "$out"; }
fi

exit $fail
