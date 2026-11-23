#!/usr/bin/env bash
# Build an isolated copy of the pipeline: scripts + config only, empty data/ and logs/.
# ROOT in every script is derived from __file__, so all writes land inside $1.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"   # the repo this qa/ folder lives in
DST="$1"
rm -rf "$DST"; mkdir -p "$DST"
cp -R "$SRC/scripts" "$SRC/config" "$DST/"
rm -rf "$DST/scripts/__pycache__" "$DST/scripts/search_providers/__pycache__"
mkdir -p "$DST"/data/{search_cache,raw,quarantine,processed/parsed,metadata} "$DST"/logs/{runs,agent_sessions} "$DST/home"
: > "$DST/logs/fetch_manifest.jsonl"
ln -s "${QA_VENV:-$SRC/.venv}" "$DST/.venv"
# test config: SearXNG only (no paid APIs, no key lookups), no politeness delays, mock SearXNG URL
"$DST/.venv/bin/python" - "$DST" "${2:-18080}" <<'PY'
import json, sys
root, port = sys.argv[1], sys.argv[2]
p = f"{root}/config/query_plan.json"
c = json.load(open(p))
c["search"]["providers"] = ["searxng"]
c["search"]["provider_settings"] = {"searxng": {"url": f"http://127.0.0.1:{port}", "engines": ["e1", "e2", "e3"], "engines_per_query": 2}}
c["search"]["query_delay_s"] = [0, 0]
c["domain_limits"] = {k: {**v, "delay_s": [0, 0]} for k, v in c["domain_limits"].items()}
c["disk"] = {"warn_data_gb": 5, "warn_step_gb": 5, "min_free_gb": 1}
json.dump(c, open(p, "w"), indent=1)
PY
cd "$DST" && git init -q . 2>/dev/null || true   # credentials.tracked_by_git needs a repo; nothing is committed
echo "sandbox ready: $DST"
