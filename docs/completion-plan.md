# CloudDrive audit and sprint completion plan

Audit date: October 24, 2026 (America/Los_Angeles). Baseline: `6f439d97b62d25b16ef22d9ab91208955bc97983` on `main`.

**Progress: Sprints 1 and 2 are complete; Sprint 3 establishes the recovery matrix next.** Extend the existing service. The core architecture works; the remaining work is closing specific failure gaps, enabling and measuring the resume targets, and adding operational recovery evidence.

This document preserves the baseline audit and proposed sprint sequence. Sprints 1 and 2 were subsequently authorized for direct delivery to `main`; see [the Sprint 1 report](sprint-1.md) for implementation and current verification. See [Sprint 2](sprint-2.md) for the large-file implementation and measurement procedure. Baseline fault probes assert the original defects and apply only to baseline `6f439d9`, not to the corrected implementation. The resume and other projects remain outside the implementation scope.

## Evidence and current status

Read the ancestor instruction locations and searched this repository for `AGENTS.md`; none were present. Reviewed README, verification notes, all backend modules and migration, frontend upload/access logic, Python and browser tests, scripts, Dockerfiles, Compose configuration, dependency definitions, and GitHub Actions. Read the CloudDrive section of the supplied resume without changing it.

| Check rerun during this audit | Result and measurement scope                                                                                                                                                                                   |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Checkout                      | Clean at the supplied commit before adding audit artifacts                                                                                                                                                     |
| Unit-only run                 | 27 passed, 21 integration cases skipped                                                                                                                                                                        |
| Full Python suite             | 48 passed = 27 unit + 21 integration, 2.91 s; real isolated PostgreSQL and MinIO; worker processing mostly called directly by integration tests                                                                |
| Packaged browser workflows    | 5/5 passed, 1.2 min; freshly built application image, Chrome, real Celery worker and beat, isolated Redis/PostgreSQL/MinIO                                                                                     |
| Frontend                      | TypeScript check and Vite production build passed                                                                                                                                                              |
| Formatting                    | Existing Black and Prettier checks passed                                                                                                                                                                      |
| Recovery gap probes           | Four Python assertions confirmed two failure gaps and two size-limit rejections; these assert existing undesired behavior and are NOT recovery success tests                                                   |
| Browser gap probe             | Interrupt, delete, then reselect identical file reproduced HTTP 409 caused by reuse of a deleted upload request ID                                                                                             |
| Broker outage smoke           | 18,900,011 bytes staged with audit Redis stopped; processing recovered after restart and downloaded size/SHA-256 matched; smoke file deleted                                                                   |
| Existing main deployment      | API restarting/unhealthy; startup log cannot resolve `postgres`. Left untouched. Its worker and beat containers were running; required PostgreSQL/Redis/MinIO were absent from the running-container inventory |
| Fresh audit deployment        | API health passed; real browser upload, verification, preview, download, delete, access transfer, mobile, and reload/resume passed                                                                             |
| Baseline GitHub CI            | Successful run for the exact baseline SHA, verified through GitHub API                                                                                                                                         |

The existing dependency set emits a Starlette/httpx deprecation warning during Python tests. It does not fail the baseline; address compatible dependency cleanup when changing the test/tooling gate.

Baseline CI: [run 39181025868](https://github.com/maneesh-bijjula/clouddrive/actions/runs/39181025868). That workflow runs browser tests against host processes and builds the application image afterward; it does not currently execute browser tests against that image in CI.

Evidence: [Python log](audit/2026-10-24/python.txt), [JUnit](audit/2026-10-24/python.xml), [browser log](audit/2026-10-24/browser.txt), [fault probes](audit/2026-10-24/probe.py), [probe results](audit/2026-10-24/probe.txt), [browser reproduction](audit/2026-10-24/browser-probe.mjs), [browser probe result](audit/2026-10-24/browser-probe.txt), [desktop](audit/2026-10-24/workspace.png), [mobile](audit/2026-10-24/mobile.png), [broker stage](audit/2026-10-24/broker-stage.txt), [broker recovery](audit/2026-10-24/broker-verify.txt), [CI result](audit/2026-10-24/github-ci.json), and [image provenance](audit/2026-10-24/images.txt).

Environment: macOS ARM64, Python 3.12.14, Node 26.3.0, Docker Engine 29.5.3. Infrastructure used an existing locally built MinIO image; the application image was rebuilt from the unchanged baseline. This is local functional evidence, not a hardware-isolated performance benchmark or an AWS verification.

## What is already implemented

- FastAPI metadata API and packaged TypeScript browser interface.
- PostgreSQL migrations with advisory locking and migration checksum validation; `bigint` file sizes.
- Direct signed multipart transfers, persisted per-part checksums/receipts, remote reconciliation after lost acknowledgements, and idempotent upload initiation/completion.
- Full-file streaming SHA-256 verification, downloads gated on verification, safe text previews, and private object access.
- Transactional processing outbox, Celery/Redis delivery, renewable claims, stale-worker fencing, reconciliation, retries, and visible failure states.
- Hashed workspace bearer keys, access-key export/import, workspace-scoped metadata operations, and initial isolation tests.
- Docker packaging, automated local tests, and passing baseline GitHub CI.

Preserve these mechanisms and extend their coverage. No framework rewrite or public deployment is needed to satisfy the stated targets.

## Requirements and units

| Requirement                            | Exact planned meaning                                                                                                                             | Baseline status                                                                                                                                     |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Upload up to 5 GB                      | Inclusive limit **5,000,000,000 bytes**, decimal GB                                                                                               | Default is 268,435,456 bytes (256 MiB), so target is rejected. Settings can be raised as high as 5 GiB, but no target-size transfer evidence exists |
| Ten concurrent 500 MB uploads          | Ten distinct files, **500,000,000 bytes each**, with overlapping actual transfer intervals; 5,000,000,000 source bytes per run                    | Default rejects each file. Browser multi-select/drop loops await files sequentially. Separate clients could overlap, but capacity is unmeasured     |
| 20/20 recovery tests without data loss | Twenty named, distinct recoverable fault scenarios, all executed and passing; original file size and source/download SHA-256 equal after recovery | Existing 21 integration cases are a mixed suite, not a defined 20-case recovery denominator                                                         |

Use `MB = 10^6 B`, `GB = 10^9 B`, `MiB = 2^20 B`, and `GiB = 2^30 B` throughout. The current browser divides by 1024 but labels sizes MB/GB; correct the labels. Retaining the current 8 MiB part size would produce 597 parts for 5 GB and 60 parts for 500 MB. Support above 5 GB, including 5 GiB = 5,368,709,120 bytes, is outside the required acceptance scope unless separately selected.

The resume says AWS S3; existing execution evidence uses MinIO. Label provider scope explicitly. A MinIO result establishes the tested S3-compatible workflow, not execution against live AWS. A live AWS validation can be added with an authorized account and budget; public hosting is independent and optional.

## Remaining work and risks, in priority order

| Priority | Finding and evidence                                                                                                                                                                                                                                                                                            | Required action                                                                                                                                        |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| P1       | **Deletion lacks durable intent.** `service.delete_file` deletes the object before committing `DELETED`. Injecting a DB failure afterward leaves `READY` metadata and a successful download-signing response whose URL returns 404. A repeated DELETE repairs it; no scheduler repairs the intent automatically | Add durable deletion intent and reconciliation with worker fencing. Prove completion/delete/worker races and interruption at each boundary             |
| P1       | **Interrupted upload cannot be re-uploaded after deletion through the same browser.** The persisted fingerprint request ID survives UI deletion; identical file selection gets `This upload was deleted; use a new request ID`                                                                                  | Correct request-ID lifecycle and workspace scoping; test interrupted delete/re-upload, reload, duplicate selection, and workspace switching            |
| P1       | **Expired upload can remain `FINALIZING`.** Aborting the storage multipart session in a probe leaves request and scheduler retries stuck in that state                                                                                                                                                          | Explicitly distinguish permanent expiry from transient errors; expose a supported restart flow and stop endless recovery loops                         |
| P1       | **Size targets are disabled and unmeasured.** Both 500,000,000 and 5,000,000,000 byte initiation requests returned 413                                                                                                                                                                                          | Define the limit once across Compose/config/API/UI; execute real large-file integrity and resume tests                                                 |
| P1       | **Recovery target is undefined.** Most existing recovery tests inject Python failures or directly change timestamps/claims; real process crashes are not a named 20-case suite                                                                                                                                  | Add a deterministic fault harness with per-case outcomes and actual process/service failure tests                                                      |
| P1       | **Concurrency target is unimplemented in the selection flow and unmeasured across clients**                                                                                                                                                                                                                     | Add a bounded file scheduler and shared part-transfer budget; benchmark ten overlapping transfers, including full processing and download verification |
| P2       | **Resource safeguards are incomplete.** No workspace storage/session quotas, upload admission/rate limits, or Compose resource budgets; synchronous S3 calls occur under row locks                                                                                                                              | Bound resource use, configure timeouts/pools, measure lock contention, and enforce admission atomically before heavy load                              |
| P2       | **Visibility is limited.** Health checks DB and S3, not worker/beat progress; no dedicated metrics, correlation IDs, backlog-age alerts, or recovery timings                                                                                                                                                    | Add phase timing and recovery/backlog metrics, structured secret-safe logs, and actionable health/runbooks                                             |
| P2       | **No backup/restore verification.** Persistent volumes are not a tested backup system                                                                                                                                                                                                                           | Back up metadata and objects consistently, restore into a fresh isolated stack, and verify inventories, hashes, access, and resumed processing         |
| P2       | **Browsing stops at 200 files.** Search/resume discovery only sees that subset; list responses also fetch every part receipt per file                                                                                                                                                                           | Add cursor pagination and compact listing summaries before larger sustained usage                                                                      |
| P2       | **Storage cleanup is only documented.** A crash after multipart creation but before DB commit can leave unreferenced uploads; application expiry/cleanup automation is absent                                                                                                                                   | Add a measured lifecycle/cleanup policy that cannot remove active valid sessions; document provider-specific controls                                  |
| P2       | **Isolation coverage is partial.** Code scopes file lookup by workspace, but cross-workspace sign/ack and concurrent access paths lack dedicated tests                                                                                                                                                          | Exercise every file operation and guessed IDs under the wrong key; keep objects private and keys/signed URLs out of evidence/logs                      |

Existing row locks and worker claim checks already address several races. Missing concurrent tests are a coverage gap, not proof those paths currently fail. The deletion probe concerns an explicitly requested deletion; it does not demonstrate spontaneous loss of unrelated files.

## Sprint plan

Estimates are focused engineering days for one engineer, including implementation, failure diagnosis, tests, packaged verification, docs, and release checks. Plan **16-26 working days total**, approximately 4-6 weeks with review and reruns. These are effort ranges, not promises of unattended runtime. Large transfer time, shared host contention, or optional AWS access can extend elapsed time.

| Sprint                                          | Effort       | Deliverables                                                                                                                                                                                                                                | Acceptance gate                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Dependencies                                                                                                            |
| ----------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| **1. Correctness and recovery states**          | **3-5 days** | Additive migration for durable deletion intent; reconciled cleanup; expiry/restart behavior; browser request-ID lifecycle fix; deterministic completion/delete/worker race tests; complete access-isolation matrix; packaged-image CI smoke | Every reproduced gap has a regression test for the desired behavior. Crash after physical deletion recovers to a terminal deletion without presenting READY metadata. Concurrent completion/delete cannot resurrect a deleted file. Wrong workspace cannot sign, acknowledge, finish, inspect, retry, delete, or download. Existing data migrates without reset. Existing and new packaged/API tests pass                                                                                                                                                                           | Baseline audit; no dependence on large-file benchmarks                                                                  |
| **2. Measured 5 GB upload and resume**          | **3-5 days** | Decimal 5 GB product limit and accurate UI units; bounded hashing/transfer buffers; recoverable URL expiry and transient-part retries; generated file fixtures; large-file runner and evidence                                              | Actual 5,000,000,000 byte file uploads through packaged browser/API/storage/worker flow, becomes READY, and downloads with exact byte count and SHA-256. A target-size interruption/reload resumes without resending acknowledged parts. 5 GB + 1 byte is rejected before storage allocation. Record hashing, transfer, verification, download durations and memory; no whole-file in-memory fixture                                                                                                                                                                                | Sprint 1; sufficient host disk and a stable test window                                                                 |
| **3. Twenty-case recovery suite**               | **4-6 days** | Versioned 20-case manifest below; deterministic synchronization hooks/harness; real API/worker/service failure injection; lease/outbox retry fixes as discovered; recovery reports and CI artifacts                                         | One complete run is 20/20 named cases, no skips or hidden retries, exact bytes/SHA-256 recovered in each case, no duplicate logical objects/results or forbidden downloads. Repeat the full matrix three times for release and report both 20 distinct cases and 60/60 attempts. Keep negative-control/race tests in a separate denominator                                                                                                                                                                                                                                         | Sprint 1; use Sprint 2 large-file support for selected slow-stream/lease cases; smaller fixtures remain normal CI tests |
| **4. Ten concurrent uploads and observability** | **3-5 days** | Bounded multi-file scheduler; global part budget; admission limits; DB/list/pool tuning as measurements require; correlation IDs and timing/backlog/claim metrics; machine-readable load harness                                            | Ten distinct 500,000,000 byte files actually transfer concurrently through packaged workflow. Ten of ten reach READY and ten downloads match source size/SHA-256; demonstrate overlapping network intervals. Run three times, report 30/30 file outcomes plus per-run failures/retries. Record aggregate transfer throughput, time to READY, per-file latency, API p50/p95, browser/API/worker/storage CPU/RSS, pool/lock/backlog metrics, and environment. No OOM, unhandled 5xx, leaked multipart sessions, or cross-workspace access. Set resource budgets before the scored run | Sprints 1-3; admission and metrics land before the scored load run; separate perf profile from ordinary CI              |
| **5. Backup/restore and release evidence**      | **3-5 days** | Metadata/object backup and restore scripts; consistency and reconciliation policy; cleanup policy; pagination/compact listings; operational runbooks; README evidence links; final CI gate                                                  | Restore a known inventory into a new Compose project and verify all committed READY object sizes/SHA-256, workspace key access, and documented in-progress recovery policy. Record backup window, measured restore duration, and actual recovery point; do not invent RPO/RTO guarantees. Clean up stale test uploads without touching active ones. All baseline/regression/browser gates pass; final SHA pushed and its GitHub CI verified                                                                                                                                         | Sprints 1-4; backup destination and test inventory; no public deployment required                                       |

Suggested first working sequence after selecting Sprint 1:

1. Convert the three reproduced gaps into desired-behavior regression tests. Add deterministic completion/delete and stale-worker overlap cases.
2. Add the durable deletion transition and reconciler using a new migration; keep existing migration checksums unchanged.
3. Fix terminal upload expiry and browser request-ID restart behavior; test real UI interruption/delete/re-upload.
4. Expand wrong-workspace operations, run the packaged browser/API suite, and wire packaged-image smoke verification into CI.
5. Update docs, create the implementation commit(s), push, and verify CI for the pushed SHA. Report the exact changes, results, remaining limitations, and next sprint.

## Planned recovery matrix: exactly 20 distinct scenarios

These are proposed acceptance cases, not audit results. Each case uses a known source file and an isolated workspace/database/bucket. Its recovery must finish within a documented per-case deadline after dependency restoration. Record injection boundary, file bytes/hash, process/service identity, elapsed recovery time, retried parts/tasks, final object count, status, and downloaded bytes/hash.

| ID  | Injected recoverable fault                                                    | Expected recovery                                                         |
| --- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| R01 | Upload initiation commits, response is lost                                   | Same request ID returns the same logical upload                           |
| R02 | Part-signing transaction commits, response is lost                            | Re-signing preserves checksum and session                                 |
| R03 | Storage PUT interrupted before accepting the complete part                    | Retry transfers that part; confirmed other parts remain                   |
| R04 | Storage accepts PUT, its response is lost                                     | Storage reconciliation finds accepted bytes without unnecessary re-upload |
| R05 | Acknowledgement request is lost before reaching API                           | Re-sign/reconciliation confirms the stored part                           |
| R06 | Acknowledgement commits, its response is lost                                 | Repeated receipt/resume reuses confirmed part                             |
| R07 | Browser reload after several acknowledged parts                               | Same-file reselection sends only missing parts                            |
| R08 | User pause while parts are in flight                                          | Resume reconciles completed parts and continues safely                    |
| R09 | Part URL expires before PUT                                                   | Fresh signed URL continues the existing session                           |
| R10 | API process killed during an incomplete multipart session                     | Restart API; persisted session and confirmed parts survive                |
| R11 | API killed after durable FINALIZING intent, before storage completion         | Reconciler completes the recorded manifest                                |
| R12 | Storage completion succeeds, storage response is lost                         | Object identity/length check continues completion safely                  |
| R13 | DB transaction fails after storage completed, before processing/outbox commit | Retry reaches PROCESSING with durable outbox work                         |
| R14 | Completion/outbox commit succeeds, API reply is lost                          | Repeated completion returns existing state without duplicate effects      |
| R15 | Redis unavailable before outbox publication                                   | Outbox persists; processing resumes after Redis restoration               |
| R16 | Dispatcher killed after publication but before outbox deletion                | Redelivery is harmless and work completes once logically                  |
| R17 | Published broker message is lost before worker delivery                       | DB reconciliation republishes missing work                                |
| R18 | Worker hard-killed during streaming verification                              | Expired claim is reclaimed; restarted worker verifies original bytes      |
| R19 | PostgreSQL unavailable during worker final-result transaction                 | Durable processing state survives; retry commits the correct result       |
| R20 | Object storage unavailable/disconnected during worker streaming               | Retry reopens the source stream and verifies the entire object            |

The harness must confirm the intended fault actually occurred; merely reaching READY is insufficient. Label function-level injection separately from hard process kills and service outages. Do not count the 27 parameterized unit cases, existing 21 integration cases, repeat runs, or unrelated happy paths as distinct recovery scenarios.

For release, run all 20 scenarios in each of three independent full passes: denominator 60 attempts. Keep failed attempts in published history; an individually rerun flaky case does not turn a failed full pass into a clean run. Safety tests for corrupt content, mismatched file, forged receipt, unauthorized access, expired storage that discarded the only uploaded bytes, and intentional deletion have different expected outcomes and are reported separately.

## Benchmark and evidence contract

For Sprint 2 and 4, generate deterministic files on disk in bounded chunks; hash before transfer and stream-hash downloaded data independently. Avoid a 5 GB Python/Node byte buffer. Use a dedicated persistent test-storage profile with adequate disk rather than the audit stack's tmpfs. Allow at least 25-30 GB of free test storage for source files, stored copies, downloads, and temporary data, and check provider/Docker capacity separately.

Capture immutable run configuration and JSON/CSV results: commit and dirty state, UTC timestamp plus displayed local date, OS/architecture, CPU/RAM, Docker limits, disk type/free space, provider/version, bucket isolation identifier, part size, active-file/part limits, worker count, pool settings, fixture seeds/bytes/SHA-256, and commands. Never include bearer keys, storage secrets, or full signed URLs.

Use distinct clocks for browser hashing, actual upload network intervals, multipart completion, time until READY, and download verification. Report transfer-only throughput separately from total end-to-end completion time. A start barrier alone does not prove concurrent transfer; preserve the overlap evidence. No throughput or latency threshold was supplied, so publish observed values and agreed resource ceilings rather than inventing a performance achievement.

Ordinary CI should run bounded correctness/recovery cases and packaged smoke workflows. Target-size, repeated full recovery, and load runs should have explicit opt-in/manual jobs with artifact retention long enough for review. Benchmark results on this shared laptop must list shared workload conditions; ordinary shared GitHub runners are not a stable performance baseline.

README highlights must link to the exact run artifact, command, file-size definition, denominator, environment, provider, and limitations. Until those gates pass, retain the current honest statement that 5 GB and concurrency performance are unverified. Do not claim production readiness or live AWS behavior from local MinIO tests.

## Historical baseline isolation and reproduction

The commands and probes below describe the original baseline audit. Run the old fault probes only against baseline `6f439d9`; they intentionally assert defects now corrected. For current code use `compose.verify.yaml` and the [current verification commands](verification.md).

Audit Compose project: `clouddrive-audit-20261024`; database: `clouddrive_audit_test`; bucket: `clouddrive-audit-20261024-test`. Ports: API 18090, PostgreSQL 15436, Redis 16383, storage 19010. Infrastructure storage is disposable and the supplied MinIO image must exist locally, or be built from `Dockerfile.minio`. Never point these cleanup fixtures at the normal workspace database or bucket.

From the repository root, with its existing virtualenv and npm dependencies:

```bash
# Infrastructure only. Check these audit-specific ports are unused first.
docker compose -f docs/audit/2026-10-24/compose.yaml up -d --wait postgres redis minio
export CLOUDDRIVE_DATABASE_URL=postgresql+psycopg://clouddrive:clouddrive@localhost:15436/clouddrive_audit_test
export CLOUDDRIVE_REDIS_URL=redis://localhost:16383/0
export CLOUDDRIVE_S3_ENDPOINT=http://localhost:19010
export CLOUDDRIVE_S3_PUBLIC_ENDPOINT=http://localhost:19010
export CLOUDDRIVE_S3_BUCKET=clouddrive-audit-20261024-test
.venv/bin/python -m pytest --integration -q
PYTHONPATH="$PWD:$PWD/tests" .venv/bin/python -m pytest -p conftest docs/audit/2026-10-24/probe.py -q -s

# Start workers only AFTER Python tests/probes; their fixtures truncate audit data.
docker compose -f docs/audit/2026-10-24/compose.yaml build api
docker compose -f docs/audit/2026-10-24/compose.yaml up -d --wait api worker beat
PLAYWRIGHT_EXTERNAL_SERVER=1 PLAYWRIGHT_BASE_URL=http://localhost:18090 PLAYWRIGHT_CHANNEL=chrome npm run test:browser
node docs/audit/2026-10-24/browser-probe.mjs

# Stop only this disposable project after review/testing.
docker compose -f docs/audit/2026-10-24/compose.yaml down
```

Use the existing smoke script's `--base-url http://localhost:18090` override and a unique state-file path to repeat the broker outage check, stopping/starting only this audit project's Redis. The state file contains a secret and must not be published. Do not overlap Python cleanup fixtures and packaged/browser work against the same audit database.

Audit cleanup: the disposable audit Compose project was stopped and removed after verification. Existing CloudDrive, Projects 1 and 2 containers and data were left as found.

All future sprint gates include preserving existing data, scoped test cleanup, packaged browser/API verification, documentation, commit/push, and checking GitHub CI against the exact pushed SHA. Sprints 1 and 2 were authorized and completed; later sprints begin when the user chooses them.
