# Sprint 3: named recovery matrix

Sprint 3 implements the versioned [20-case recovery manifest](recovery-matrix.json). This suite is separate from the Python regression count, the browser workflow count, and Sprint 2's single 5 GB measurement. Each complete pass must attempt all 20 distinct cases once; the release gate is three independent complete passes, or **60/60 attempts**. Failed full passes remain in history; individual reruns do not replace their outcomes.

## Recovery contract

Each case creates a distinct workspace and known source fixture inside a dedicated disposable Compose project. Success requires direct evidence that the intended fault occurred, eventual `READY`, exact source/worker/download byte counts and SHA-256, one logical file and final storage object, and no remaining multipart upload. Downloads must be rejected before verification and for a different workspace. Corrupt content and intentional deletion have different expected outcomes and remain outside the 20-case denominator.

Browser recovery cases R01-R09 have a 120-second recovery deadline. Backend cases R10-R20 have a 180-second deadline, allowing the application's real 90-second worker lease and 30-second lost-work reconciliation threshold to elapse. Backend clocks start before requesting restoration, including startup/readiness; the worker-kill clock starts after confirmed death and includes natural lease expiry. Browser clocks start at the observed transport fault, at same-file reselection for reload/pause, or at the first actual expired-URL 403. Total case elapsed time is reported separately from recovery time; fixture preparation and deliberate fault holds are outside the recovery clock. No case may pass by changing database timestamps to bypass these production delays.

Browser transport injection, genuinely expired signatures, process `SIGKILL`, broker-message deletion, service outages, and function-boundary injection are labelled separately. A browser route abortion is a controlled browser transport fault; it must not be described as a physical network outage. A controlled exception at a storage/database boundary proves recovery at that boundary without establishing real provider failover.

## Isolation and controls

Only the dedicated recovery project may be stopped, restarted, or faulted. Fault instrumentation is disabled by default and requires explicit opt-in plus isolated test database/bucket checks. There are no public fault-control API routes. Test controls and reports must exclude workspace keys, storage credentials, and signed URLs. Normal CloudDrive data, the existing deployment, other projects, and the resume remain outside this work.

## Verification status

The separate development rehearsals passed **9/9 browser cases** and **11/11 backend cases**. These are partial batches, not a complete 20-case pass. The full Python regression suite passed **89 cases (39 unit + 50 integration)**, and all **20 packaged browser workflows** passed without retries. The three-pass release gate and exact-commit CI are pending.

## Reproduce

Install Python development requirements, Node dependencies, and Playwright Chromium. Use this repository's root directory. The three-pass runner defaults to the exact 60-attempt release denominator; `--passes 1` runs one complete 20-case pass. A `--cases R01,R02` selection is explicitly a partial development run and cannot satisfy the release gate. Every output path must be new; previous results are never overwritten.

```bash
python -m pip install -r requirements-dev.txt
npm ci
npx playwright install chromium
mkdir -p test-results/recovery-control
chmod 777 test-results/recovery-control
docker compose -p clouddrive-sprint3-recovery -f compose.recovery.yaml up --build -d --wait
npm run test:recovery -- --passes 3 --output test-results/recovery/release-1.json
# With an existing Chrome installation: prefix the command with PLAYWRIGHT_CHANNEL=chrome.
# With a virtualenv: also set CLOUDDRIVE_TEST_PYTHON="$PWD/.venv/bin/python".

# After reviewing results, remove only this disposable project and its volumes.
docker compose -p clouddrive-sprint3-recovery -f compose.recovery.yaml down -v --remove-orphans
```

The runner checks Compose ownership, all published loopback ports, the test database and bucket, and the mounted control directory before testing. It uses API 18092, PostgreSQL 15438, Redis 16385, and MinIO 19012. The profile's three named volumes belong only to this project and survive injected dependency stops. Controls must remain inside this repository's ignored `test-results/` directory. The runner supplies explicit public test credentials and endpoints; it does not edit the normal `.env`.

Reports record the manifest digest, commit and dirty state, source digests, immutable running image IDs, hardware, case outcomes, fault proof, recovery/elapsed clocks, part/task attempts, hashes, logical/object/result counts, and scoped cleanup. Backend child reports are saved alongside the aggregate. Keys and signed URLs are excluded. A failure remains in its original full pass; application retries are counted as recovery behavior, while the harness never reruns a failed case within that pass.

The `Named recovery matrix` GitHub workflow runs one complete pass on push/pull request. Manual dispatch supports three passes. Recovery artifacts are retained for 30 days. Ordinary CI separately verifies Python regressions and the host/packaged browser workflows.
