#!/usr/bin/env bash
# Boot the built image in mock mode and drive one run to pipeline.end via the
# job API. Fully offline — mock issues, sealed flags.
set -euo pipefail
BASE="${1:-http://localhost:18000}"

for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || { echo "FAIL: /health=$code"; exit 1; }

# Issue #8001 in MockGitHubIssuesFetcher (src/tvastr/ingestion/github_issues.py)
# has a "ModuleNotFoundError: ..." repro in its body, so issue_to_events
# yields at least one event and the run proceeds through the pipeline to
# pipeline.end. Other mock issues either duplicate this signature (#8002),
# use a different one (#8010), or carry no error signature at all (#8050,
# a feature request) — the latter short-circuits to a single `error` event
# with no pipeline.end. #8001 is also what
# test_post_run_returns_run_id_promptly (tests/test_run_lifecycle.py) uses.
run_id=$(curl -sf -X POST -H 'content-type: application/json' \
  -d '{"repo":"run-llama/llama_index","issue_number":8001,"dry_run":true}' \
  "$BASE/api/run" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "run_id=$run_id"

deadline=$((SECONDS + 60))
while [ $SECONDS -lt $deadline ]; do
  if curl -sf --max-time 30 "$BASE/api/runs/$run_id/stream" | grep -q "event: pipeline.end"; then
    echo "OK: run $run_id reached pipeline.end"
    exit 0
  fi
  sleep 2
done
echo "FAIL: run $run_id did not reach pipeline.end within 60s"
exit 1
