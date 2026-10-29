# Verification

## Sprint 4 regression verification

The bounded scheduler/admission/observability implementation passed **108 Python cases (41 unit + 67 integration)** and **28 packaged Playwright cases (26 browser workflows + 2 pure scheduler checks)**, with browser retries disabled. A separate complete **20/20** named recovery regression passed against the same application source. The separate hosted target-size run passed **three complete ten-file cohorts (30/30)** on `20f9dbf`, with matching source/worker/download hashes, 1,800 successful HTTP/2 PUTs, unchanged budgets, and clean final inventory. Nine lightweight Node observer/hash-helper tests also passed. Small ten-file rehearsals remain a separate denominator. See [Sprint 4](sprint-4.md) for commands, budget manifest, transport profile, raw results and precise measurement status.

## Sprint 3 implementation verification

The [versioned 20-case recovery matrix](recovery-matrix.json) passed **three complete local passes, 60/60 attempts**, on clean implementation `7569c88`. Separate Linux CI passed 20/20 once on the same SHA. Each case proves its fault and validates exact source/worker/download bytes and SHA-256, unique logical effects, download access gates, and cleanup. Regression verification passed **89 Python cases (39 unit + 50 integration)** and **20 packaged browser workflows** without local retries; CI also passed all 20 workflows in both host and packaged modes. See [Sprint 3](sprint-3.md) for raw evidence, timing definitions, environment, and limitations. Development batches remain separate from the release denominator.

## Sprint 2 verification

The default cap is decimal 5 GB. Sprint 2 regression coverage was 28 unit cases, 50 integration cases, and 20 browser workflows; this browser count is unrelated to the planned 20-case recovery matrix. Reuse the isolated small-fixture commands below for regressions. [Sprint 2](sprint-2.md) documents the separate disk-backed profile, bounded retry behavior, and opt-in target-size runner. The [one-file 5,000,000,000-byte run](evidence/sprint-2/5gb-run-1.json) passed with matching source/worker/download hashes and no confirmed-part resends after reload, on clean implementation revision `b82831e`. The [report](sprint-2.md) defines exact timings, denominator, sampled memory, and limitations. This is separate from ordinary CI.

## Sprint 1

Sprint 1 adds durable deletion, terminal multipart expiry and browser restart, workspace-scoped saved request IDs, and deterministic race/isolation/migration coverage. See [the Sprint 1 report](sprint-1.md). Local execution uses macOS ARM64, Python 3.12.14, Node 26.3.0, Chrome, Docker Engine 29.5.3, PostgreSQL 17, Redis 7, and MinIO. CI uses Ubuntu, Python 3.12, Node 24, and Chromium.

| Local Sprint 1 check                      | Result                                                          |
| ----------------------------------------- | --------------------------------------------------------------- |
| Unit tests                                | 27 passed                                                       |
| Real-service integration tests            | 48 passed                                                       |
| Combined Python run                       | 75 passed in 5.60 s; one visible TestClient deprecation warning |
| Packaged browser workflows                | 11 passed in 1.3 min; no retries                                |
| TypeScript / Vite production build        | Passed                                                          |
| Black / Prettier / diff whitespace checks | Passed                                                          |
| Initial migration checksum                | Unchanged                                                       |

Evidence: [Python output](evidence/sprint-1/python.txt), [JUnit](evidence/sprint-1/python.xml), [packaged browser output](evidence/sprint-1/browser.txt), [application build](evidence/sprint-1/docker-build.txt), [desktop](evidence/sprint-1/workspace.png), and [mobile](evidence/sprint-1/mobile.png). These are local functional results on MinIO, not live AWS, large-file, concurrency, or 20-case recovery benchmark results.

The isolated packaged verification stack has its own network, database `clouddrive_verify_test`, bucket `clouddrive-verify-test`, and disposable service data. Ports are 18090 (API), 15436 (PostgreSQL), 16383 (Redis), and 19010 (S3). Check those ports are free before starting. Override the Compose project name with `-p` if another verification project is already in use, and choose different ports/configuration rather than stopping it.

```bash
# Start infrastructure only; do not start workers during truncating Python tests.
docker compose -f compose.verify.yaml build minio
docker compose -f compose.verify.yaml up -d --wait postgres redis minio
export CLOUDDRIVE_DATABASE_URL=postgresql+psycopg://clouddrive:clouddrive@localhost:15436/clouddrive_verify_test
export CLOUDDRIVE_REDIS_URL=redis://localhost:16383/0
export CLOUDDRIVE_S3_ENDPOINT=http://localhost:19010
export CLOUDDRIVE_S3_PUBLIC_ENDPOINT=http://localhost:19010
export CLOUDDRIVE_S3_BUCKET=clouddrive-verify-test
.venv/bin/python -m pytest --integration -q --junitxml=test-results/python.xml

# Build and verify the actual packaged API and real Celery worker/scheduler.
docker compose -f compose.verify.yaml build api
docker compose -f compose.verify.yaml up -d --wait api worker beat
export CLOUDDRIVE_TEST_PYTHON="$PWD/.venv/bin/python"
PLAYWRIGHT_EXTERNAL_SERVER=1 PLAYWRIGHT_BASE_URL=http://localhost:18090 npm run test:browser
# Add PLAYWRIGHT_CHANNEL=chrome if using installed Chrome instead of Chromium.

docker compose -f compose.verify.yaml down
```

The `expire-test-upload.py` browser helper rejects database/bucket names without `_test`/`-test` suffixes and only aborts the specified UPLOADING test session. Integration cleanup and populated migration schemas are also confined to the isolated test database. These suffix checks supplement isolated configuration; they do not make an arbitrary shared test database safe to truncate.

Ordinary host browser tests remain available with `compose.test.yaml` and `npm run test:browser`. Playwright clears only `test-results/browser`; API/worker logs and Python reports remain alongside that subdirectory. CI saves separate host and packaged HTML reports and logs, and runs all browser workflows in both modes.

## Baseline verification

The initial implementation was verified locally on macOS ARM with Python 3.12, Node.js 26, Google Chrome, and Docker. CI uses Python 3.12, Node.js 24, and Chromium on Ubuntu.

| Check                                                     | Result    |
| --------------------------------------------------------- | --------- |
| Unit tests                                                | 27 passed |
| PostgreSQL + S3 integration tests                         | 21 passed |
| Browser workflows with a real Celery worker and scheduler | 5 passed  |
| TypeScript check and Vite production build                | Passed    |
| Black and Prettier checks                                 | Passed    |
| Browser workflows against the packaged Docker stack       | 5 passed  |
| Actual Redis outage and restart with an 18.9 MB upload    | Passed    |

The multipart API test uploads more than 8 MiB, completes two parts, verifies the entire object, and compares downloaded bytes. The browser resume test uploads a file of 12 MiB plus 17 bytes, pauses with one confirmed part, reloads the page, and checks that only the missing part is sent. Screenshots show actual files uploaded by those browser tests.

The database failure test injects a failure after successful S3 completion and before the processing state update, then verifies recovery on retry. Broker failure coverage checks retained outbox work. Worker fencing coverage replaces a claim or deletes a file during streaming and verifies that the stale worker cannot commit a result.

To repeat the packaged broker outage check against the local Docker stack, activate the development virtual environment, then run:

```bash
docker compose stop redis
python scripts/smoke.py stage --state /tmp/clouddrive-smoke.json
docker compose start redis
python scripts/smoke.py verify --state /tmp/clouddrive-smoke.json
```

The smoke check uploads 18,900,011 bytes while the broker is stopped, confirms that download is blocked, then waits for verification and compares the downloaded size and SHA-256 after restart. The temporary state file contains a workspace access key, is created with restricted permissions, and is removed after successful verification. Use a fresh state path for each run.

These checks validate the implemented workflows with MinIO. They do not establish production availability, large-file performance, live AWS behavior, or a concurrency capacity.
