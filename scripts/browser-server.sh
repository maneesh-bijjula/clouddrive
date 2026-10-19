#!/usr/bin/env bash
set -euo pipefail
python_bin="${CLOUDDRIVE_TEST_PYTHON:-python}"
mkdir -p test-results
api_pid=""
worker_pid=""
beat_pid=""
cleanup() {
  for process_id in "$api_pid" "$worker_pid" "$beat_pid"; do
    if [ -n "$process_id" ]; then kill "$process_id" 2>/dev/null || true; fi
  done
  wait || true
}
trap cleanup EXIT INT TERM
"$python_bin" -m uvicorn clouddrive.api:app --host 127.0.0.1 --port 8090 > test-results/api.log 2>&1 &
api_pid=$!
"$python_bin" -m celery -A clouddrive.worker:celery worker --pool=solo --loglevel=INFO > test-results/worker.log 2>&1 &
worker_pid=$!
"$python_bin" -m celery -A clouddrive.worker:celery beat --loglevel=INFO --schedule=/tmp/clouddrive-test-beat > test-results/beat.log 2>&1 &
beat_pid=$!
wait "$api_pid"
