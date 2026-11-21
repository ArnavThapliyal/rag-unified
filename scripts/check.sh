#!/usr/bin/env bash
# Repo health check: static analysis, unit tests, config validity, data audit, services, disk.
#   scripts/check.sh            # everything
#   scripts/check.sh --offline  # skip the SearXNG / LM Studio checks
# Exit code is non-zero if any check fails.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
fail=0
step() { printf '%-28s' "$1"; }
ok() { echo "ok${1:+  ($1)}"; }
bad() { echo "FAIL${1:+  ($1)}"; fail=1; }

step "pyflakes"
out=$("$PY" -m pyflakes scripts tests 2>&1) && ok || { bad; echo "$out"; }

step "bash syntax"
bash -n scripts/run_agent.sh scripts/check.sh && ok || bad

step "json configs"
"$PY" -c "import json; [json.load(open(p)) for p in ('config/query_plan.json', 'package.json')]" && ok || bad

step "pytest"
out=$("$PY" -m pytest -q tests 2>&1) && ok "$(echo "$out" | tail -1)" || { bad; echo "$out" | tail -30; }

step "dataset audit"
out=$(cd scripts && "$PY" audit_dataset.py) && ok "$out" || bad "$out"

step "disk"
d=$(cd scripts && "$PY" -c "import common; d = common.disk_status(); print(d['level'], d['data_gb'], 'GB data,', d['free_gb'], 'GB free')")
case "$d" in critical*) bad "$d" ;; *) ok "$d" ;; esac

step "no stale references"
hits=$(grep -rIil "roo code\|\.roo/\|rooignore" --exclude-dir={vendor,node_modules,.venv,.git} . 2>/dev/null | grep -v '^./scripts/check.sh$')
[ -z "$hits" ] && ok || bad "$hits"

if [ "${1:-}" != "--offline" ]; then
  step "SearXNG json"
  curl -sf -m 10 "http://localhost:8082/search?q=test&format=json" >/dev/null && ok || bad "localhost:8082"
  step "LM Studio"
  curl -sf -m 5 http://localhost:1234/v1/models >/dev/null && ok || bad "localhost:1234"
  step "cline cli"
  v=$(npx --no-install cline --version 2>/dev/null) && ok "$v" || bad "run npm install"
fi

exit $fail
