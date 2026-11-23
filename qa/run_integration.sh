#!/usr/bin/env bash
# Isolated end-to-end test: search result -> docket -> download -> extraction -> normalization -> dedupe -> audit,
# then failure / recovery scenarios. Everything runs in a throwaway copy ($1); no external network:
# http:// "official" URLs go to the local mock (as an HTTP proxy), HTTPS CONNECT is refused.
#
#   qa/run_integration.sh /tmp/nse_qa_int        # takes ~8 min (one 4-min slow-URL scenario)
set -uo pipefail
QA="$(cd "$(dirname "$0")" && pwd)"
SB="${1:?usage: run_integration.sh SANDBOX_DIR}"
PORT="${QA_PORT:-18080}"
"$QA/make_sandbox.sh" "$SB" "$PORT"
mkdir -p "$SB/qa_logs"
"$SB/.venv/bin/python" -I - "$SB" <<'PY'
import json, sys
p = f"{sys.argv[1]}/config/query_plan.json"; c = json.load(open(p))
c["phases"] = [{"name": "master", "year_tag": "master", "pages": 2, "queries": ["nse delisted companies list", "sebi delisting orders"]}]
json.dump(c, open(p, "w"), indent=1)
PY
(cd "$QA" && exec "$SB/.venv/bin/python" -I mock_server.py "$PORT" "$SB/qa_logs") > "$SB/qa_logs/mock.out" 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null' EXIT
sleep 1.5
cd "$SB"
export HOME="$SB/home" HTTP_PROXY="http://127.0.0.1:$PORT" HTTPS_PROXY="http://127.0.0.1:$PORT" \
       http_proxy="http://127.0.0.1:$PORT" https_proxy="http://127.0.0.1:$PORT" NO_PROXY= no_proxy=
scenario() { curl -s --noproxy '*' "http://127.0.0.1:$PORT/scenario?name=$1" >/dev/null; }
summary() { tail -1 "$1" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in sys.argv[1:]})" "${@:2}"; }

echo "== 1. full pipeline"
.venv/bin/python scripts/collect.py --max-queries 2 --unlimited > qa_logs/run1.out 2>&1; echo "exit $?"
summary qa_logs/run1.out status stop_reason fetched_ok fetch_failed fetch_dead parsed_rows records_after
sqlite3 data/docket.sqlite "select status, count(*) from items group by status"
.venv/bin/python -c "import csv; [print(' ', r['company_name'][:40], r['effective_delisting_date'], r['delisting_type'], r['confidence_status']) for r in csv.DictReader(open('data/processed/nse_delisted_master.csv'))]"

echo "== 2. server sends Retry-After as an HTTP date -> fetch worker crash, run still 'completed'"
scenario retrydate
.venv/bin/python scripts/collect.py --max-queries 1 --unlimited > qa_logs/run_crash.out 2>&1; echo "exit $?"
grep -c ValueError qa_logs/run_crash.out; summary qa_logs/run_crash.out status stop_reason fetch_stop_reason
sqlite3 data/docket.sqlite "select status, attempts from items where url like '%retry-date%'"
sqlite3 data/docket.sqlite "update items set status='dead' where url like '%retry-date%'"   # neutralise for the next steps

echo "== 3. SIGKILL mid-download, then resume"
scenario slow
.venv/bin/python - <<'PY'
import os, signal, subprocess, time
p = subprocess.Popen([".venv/bin/python", "scripts/collect.py", "--max-queries", "1", "--unlimited"],
                     stdout=open("qa_logs/run_kill.out", "w"), stderr=subprocess.STDOUT, start_new_session=True)
time.sleep(12); os.killpg(p.pid, signal.SIGKILL); p.wait()
PY
sqlite3 data/docket.sqlite "select status, attempts from items where url like '%slow-order%'"
.venv/bin/python scripts/collect.py --harvest-only > qa_logs/run_resume1.out 2>&1
summary qa_logs/run_resume1.out status stop_reason fetched_ok
sqlite3 data/docket.sqlite "update items set lease_until=0 where status='fetching'"     # lease expiry
.venv/bin/python scripts/collect.py --harvest-only > qa_logs/run_resume2.out 2>&1       # ~4 min: 45 s timeout x4
summary qa_logs/run_resume2.out status stop_reason fetched_ok records_after

echo "== 4. graceful stop through job.py"
sqlite3 data/docket.sqlite "update items set next_attempt_at=0 where url like '%slow-order%'"
.venv/bin/python scripts/job.py start --harvest-only >/dev/null; sleep 8
.venv/bin/python scripts/job.py stop | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d.get('result'))"

echo "== 5. local network outage -> domain blocked permanently"
kill $MOCK; sleep 1
.venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts'); import docket
docket.Docket().file([{'url': f'http://www.nseindia.com/content/delisting/order_{i}.pdf', 'kind': 'search', 'year_tag': '2017'} for i in range(4)], ['nseindia.com'])"
.venv/bin/python scripts/collect.py --max-queries 1 > qa_logs/run_outage.out 2>&1; echo "exit $?"
summary qa_logs/run_outage.out status stop_reason newly_blocked_domains
sqlite3 data/docket.sqlite "select domain, reason from domains where blocked=1"
echo "integrity: $(sqlite3 data/docket.sqlite 'pragma integrity_check')"
