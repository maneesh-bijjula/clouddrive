# Sprint 3: named recovery matrix

Sprint 3 implements the versioned [20-case recovery manifest](recovery-matrix.json). This suite is separate from the Python regression count, the browser workflow count, and Sprint 2's single 5 GB measurement. Each complete pass must attempt all 20 distinct cases once; the release gate is three independent complete passes, or **60/60 attempts**. Failed full passes remain in history; individual reruns do not replace their outcomes.

## Recovery contract

Each case creates a distinct workspace and known source fixture inside a dedicated disposable Compose project. Success requires direct evidence that the intended fault occurred, eventual `READY`, exact source/worker/download byte counts and SHA-256, one logical file and final storage object, and no remaining multipart upload. Downloads must be rejected before verification and for a different workspace. Corrupt content and intentional deletion have different expected outcomes and remain outside the 20-case denominator.

Browser recovery cases R01-R09 have a 120-second recovery deadline. Backend cases R10-R20 have a 180-second deadline, allowing the application's real 90-second worker lease and 30-second lost-work reconciliation threshold to elapse. Backend clocks start before requesting restoration, including startup/readiness; the worker-kill clock starts after confirmed death and includes natural lease expiry. Browser clocks start at the observed transport fault, at same-file reselection for reload/pause, or at the first actual expired-URL 403. Total case elapsed time is reported separately from recovery time; fixture preparation and deliberate fault holds are outside the recovery clock. No case may pass by changing database timestamps to bypass these production delays.

Browser transport injection, genuinely expired signatures, process `SIGKILL`, broker-message deletion, service outages, and function-boundary injection are labelled separately. A browser route abortion is a controlled browser transport fault; it must not be described as a physical network outage. A controlled exception at a storage/database boundary proves recovery at that boundary without establishing real provider failover.

## Isolation and controls

Only the dedicated recovery project may be stopped, restarted, or faulted. Fault instrumentation is disabled by default and requires explicit opt-in plus isolated test database/bucket checks. There are no public fault-control API routes. Test controls and reports must exclude workspace keys, storage credentials, and signed URLs. Normal CloudDrive data, the existing deployment, other projects, and the resume remain outside this work.

## Verification status

**Passed October 27, 2026 (America/Los_Angeles): 20/20 distinct named recovery scenarios in each of three complete local passes, or 60/60 attempts.** The clean measured implementation was [`7569c8891eca8361027308fb2f9139d582d96931`](https://github.com/maneesh-bijjula/clouddrive/commit/7569c8891eca8361027308fb2f9139d582d96931). Source digests, the manifest, and HEAD remained unchanged throughout the run; packaged Python source matched the checkout. All 60 file IDs were unique. The evidence commit changes reports/documentation only.

The full Python regression suite passed **89 cases (39 unit + 50 integration)**, and all **20 packaged browser workflows** passed without local retries. [Normal implementation CI](https://github.com/maneesh-bijjula/clouddrive/actions/runs/40608605139) also passed 89 Python cases plus 20 host and 20 packaged browser workflows. The separate [Linux recovery CI](https://github.com/maneesh-bijjula/clouddrive/actions/runs/40608605113) passed **20/20 cases once** on the same implementation SHA. That CI pass is additional verification and is not combined with the 60-attempt local release denominator.

## Reproduce

Install Python development requirements, Node dependencies, and Playwright Chromium. Use this repository's root directory. The three-pass runner defaults to the exact 60-attempt release denominator; `--passes 1` runs one complete 20-case pass. A `--cases R01,R02` selection is explicitly a partial development run and cannot satisfy the release gate. The three-pass release run requires a clean committed checkout. Every output path must be new; previous results are never overwritten.

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

## Measured release evidence

The local run used macOS ARM64 on an Apple M3 with 8 logical CPUs and 8 GiB host RAM, Node 26.3.0, and Chrome. Docker Engine 29.5.3 exposed 8 CPUs and 4,108,632,064 bytes of VM memory. The host was shared; the JSON records the other-container count at run start. PostgreSQL 17, Redis 7, and pinned-source MinIO ran in the isolated packaged profile. Exact running image IDs and source hashes are in the aggregate. Linux CI used x64 and Node 24. These are functional recovery measurements on MinIO, not live AWS, concurrency-capacity, or production-availability evidence.

Fixtures ranged from **262,161 bytes** to **67,108,881 bytes (64 MiB + 17 bytes)**. They are intentionally bounded correctness fixtures; Sprint 2 separately measured one actual 5 GB file. Every attempt directly observed its fault, reached READY, and matched source/worker/download byte counts and SHA-256, with one logical metadata row, object, and committed result. Downloads were denied before verification and across workspaces. No cases were skipped or rerun inside a full pass.

| Full pass | Passed / attempted | Full pass duration |
| --------- | -----------------: | -----------------: |
| 1         |            20 / 20 |           447.04 s |
| 2         |            20 / 20 |           446.85 s |
| 3         |            20 / 20 |           467.68 s |

The complete three-pass runner took **1363.36 seconds** (22 minutes 43.36 seconds), including preparation, deliberate fault holds, recovery waits, downloads, checks, and case cleanup. Docker build/startup happened before that runner clock.

Recovery clocks end at browser Verified or API READY observation; downloads and cleanup are checked afterward. Browser cases measure observed fault-to-Verified, except reload/pause measure reselection-to-Verified. Backend clocks begin before restoration is requested and include startup/readiness. The worker-kill clock includes natural lease expiry. These differing observations and small fixtures are not latency SLOs or p95 benchmarks. The CSV includes total case time as well.

| Case | Pass 1 recovery | Pass 2 recovery | Pass 3 recovery |
| ---- | --------------: | --------------: | --------------: |
| R01  |          3.39 s |          5.94 s |          6.43 s |
| R02  |          6.33 s |          3.28 s |          6.35 s |
| R03  |          6.76 s |          6.17 s |          6.77 s |
| R04  |          3.28 s |          3.25 s |          3.29 s |
| R05  |          5.88 s |          5.81 s |          5.88 s |
| R06  |          3.32 s |          3.24 s |          3.27 s |
| R07  |          5.80 s |          5.76 s |          5.75 s |
| R08  |          7.82 s |          7.37 s |          7.81 s |
| R09  |          2.71 s |          2.66 s |          2.72 s |
| R10  |          2.75 s |          6.93 s |          2.78 s |
| R11  |         33.74 s |         33.28 s |         33.59 s |
| R12  |          3.70 s |          4.23 s |          3.66 s |
| R13  |          3.69 s |          4.19 s |          2.62 s |
| R14  |          6.38 s |          6.38 s |          7.98 s |
| R15  |          5.46 s |          5.98 s |          3.99 s |
| R16  |          4.69 s |          3.31 s |          5.23 s |
| R17  |         32.31 s |         32.28 s |         30.15 s |
| R18  |         94.91 s |         94.95 s |         90.94 s |
| R19  |         94.45 s |         94.40 s |         92.14 s |
| R20  |          1.48 s |          2.96 s |          5.80 s |

R20 intentionally waits for a real read failure before restoring storage. Its full case times were **36.98 / 38.36 / 43.21 seconds**, including that outage; the shorter recovery values above begin at restoration request. Each local R20 attempt recorded seven outbox publications, eight to eleven task receipts, two stream attempts, and one committed result. Delivery is at least once; idempotent results do not imply exactly-once delivery.

The [final inventory](evidence/sprint-3/final-inventory.json) confirmed **60 DELETED test file rows, zero stored objects, zero multipart uploads, zero outbox rows, and zero active claims**. Only this disposable project and its volumes were removed. Normal CloudDrive data, other projects, and the resume were left unchanged.

Evidence: [full local JSON](evidence/sprint-3/release-1.json), [CSV](evidence/sprint-3/case-results.csv), [local transcript](evidence/sprint-3/release-1.txt), [Linux CI JSON](evidence/sprint-3/ci-recovery-implementation.json), [CI regression summary](evidence/sprint-3/ci-regression-summary.txt), [Python output](evidence/sprint-3/python.txt), [packaged browser output](evidence/sprint-3/packaged-browser.txt), and [complete run history](evidence/sprint-3/run-history.json). The earlier 9-case and 11-case development batches remain in history with dirty-source/reporting limitations; neither counts toward the release result. No failed recovery-case attempts were omitted.

Sprint 4's ten concurrent 500 MB uploads and Sprint 5's backup/restore evidence remain pending.
