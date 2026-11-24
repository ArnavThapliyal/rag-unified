#!/usr/bin/env bash
# Continuous collection: agent (or deterministic) collection segments, then universe-sync and export to the
# outbox for rag-processor, cycle after cycle. Options: see `scripts/run_continuous.sh --help`
# (src/rag_collector/loop.py). Stop: Ctrl-C, SIGTERM, or `touch data/metadata/STOP`.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo ".venv missing: run ./install.sh first" >&2; exit 2; }
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m rag_collector.loop "$@"
