# Sprint 4: ten concurrent uploads and observability

Sprint 4 implements the bounded multi-file scheduler, global part budget, server admission controls, correlated metrics, and reproducible load harness required by the [completion plan](completion-plan.md). The versioned [load budgets](load-budgets.json) are declared before target-size measurement. No Sprint 4 performance or concurrency result is claimed until the scored evidence and regression gates pass.

## Target and denominator

Each complete scored run uploads ten distinct files of **500,000,000 bytes (500 MB)**, for **5,000,000,000 source bytes per run**. Three complete runs require **30/30 file outcomes**. Ten deterministic source files may be generated once and reused, provided their contents and SHA-256 values are distinct and each run creates fresh workspace/file IDs. A failed cohort stays in run history; rerunning one file does not repair that full run's outcome.

`MB = 10^6 bytes`, `GB = 10^9 bytes`, `MiB = 2^20 bytes`, and `GiB = 2^30 bytes`. The default 8 MiB part size produces 60 parts for a 500 MB file. Small smoke fixtures and ordinary browser regressions are separate evidence.

A start barrier, multiple queued files, and overlapping whole-file lifetimes do not establish ten simultaneous transfers. The harness must retain a common intersection of ten actual storage PUT byte-transfer intervals, supported by positive per-file XHR upload progress and protocol/network timing. It must identify the files and intervals rather than infer concurrency from ten successful outcomes.

## Budgets fixed before measurement

| Component                           | Docker hard memory limit |  CPU limit |
| ----------------------------------- | -----------------------: | ---------: |
| API                                 |                  384 MiB |     1 core |
| Worker, including two pool children |                  512 MiB |    2 cores |
| Beat                                |                  128 MiB |  0.25 core |
| MinIO                               |                  768 MiB |    2 cores |
| PostgreSQL                          |                  384 MiB |     1 core |
| Redis                               |                  128 MiB |   0.5 core |
| TLS/HTTP2 gateway                   |                  128 MiB |     1 core |
| Total                               |     2432 MiB (2.375 GiB) | 7.75 cores |

The sampled Chrome process-RSS sum must remain at or below **1.5 GiB**, and the Node runner RSS at or below **512 MiB**. Chrome RSS may count shared pages more than once. These are sampled acceptance ceilings; Docker container limits are enforced hard limits. Sampling cannot establish instantaneous process allocation maxima. The nominal sample period is one second; actual timestamps and any skipped overlapping samples are reported. Sampling errors have a zero budget.

Container OOM state and cgroup OOM counters must both be checked, since a killed Celery child can leave the container's main process alive. The profile is intended for the shared 8 GiB host and roughly 3.83 GiB Docker VM; other running workloads and available capacity must be recorded rather than stopped.

The host needs at least **25 GB free before fixture generation**, with **30 GB recommended**. The storage filesystem needs at least **12 GB free** before a scored run. Download verification is sequential and streaming; only one downloaded 500 MB fixture is retained at a time. The runner's 600-second READY deadline includes browser hashing through READY; a 900-second complete-cohort bound includes downloads and cleanup. These are harness bounds, not latency SLOs. Shared fixture generation is recorded separately.

Budgets may be revised during development before scoring. A revision after an attempted scored run requires a new manifest revision and new complete measurement; failed attempts remain in history. The runner must not weaken thresholds automatically to make a result pass.

## Scheduler and admission configuration

The measured browser configuration admits at most ten active files, two hashing workers, ten global part slots, and three part loops per file. Owner-aware part admission reserves a slot for each active file before lending spare slots, preventing early files from consuming the entire global budget while other files wait. The API publishes these limits so the browser and runner use the same configuration.

The backend admits at most ten active uploads per workspace and one hundred globally; active means `UPLOADING`, `FINALIZING`, or `PROCESSING`. The API allows at most 64 concurrent requests. Its database pool is eight persistent connections plus eight overflow connections with a ten-second pool timeout. Limits must be enforced before side effects, preserve existing data, and return explicit retryable admission outcomes without permitting another workspace to inspect file metadata.

## Network profile and overlap proof

The load profile uses a dedicated streaming TLS/HTTP2 gateway in front of MinIO. A browser using one HTTP/1 origin may cap simultaneous connections below ten, so a measured ten-stream result through this gateway must not be generalized to the default plain-HTTP MinIO profile. The artifact must identify negotiated protocol, proxy image and configuration, certificate fingerprint, and proxy resource/OOM measurements. The local test certificate is trusted only by the isolated browser test context through its explicit certificate-error override; this does not establish production TLS configuration. Signed Host and query values must survive forwarding, and request bodies must stream without whole-file gateway buffering.

If needed, the harness may temporarily shape aggregate upload to 8 MiB/s to observe positive partial progress on all ten first PUTs. It must remove shaping before any of those first PUTs completes, retain the common active interval and shaping timestamps, and include the entire probe in transfer-window throughput. Such a result is a declared browser-concurrency exercise, not an unshaped storage-capacity measurement. A development failure to prove overlap remains a failed attempt; the harness may not silently switch protocol or weaker proof inside a scored run.

## Evidence contract

For every file, the browser/API/storage/worker path must reach READY and an independently streamed downloaded file must match the source byte count and SHA-256. Record hashing, actual network transfer windows, completion, verification/queueing, selection-to-READY, download, and total-run clocks separately. Transfer-only throughput and end-to-end throughput must state their byte and time denominators. API p50/p95 measurements must state which completed requests and route groups are included; no performance SLO has been supplied.

Record browser/runner and API/worker/storage/database/broker CPU/memory; database pool/checkouts and locks/waits; processing outbox and broker backlog; active/expired claims; request correlation; retry/publication/verification counts; admission rejections; and error outcomes. Downloads must remain forbidden before verification and across workspaces. Final inventory must show one logical object/result per source before scoped deletion, then no leaked objects, multipart sessions, outbox rows, or active claims.

Evidence identifies the clean commit, source and budget digests, immutable running images, exact admission/scheduler/worker settings, OS/CPU/RAM, Docker limits, provider, disk capacity, fixture generator and seeds, and shared-workload conditions. Keys and signed URLs must be excluded. MinIO measurements establish only the tested local S3-compatible workflow; they do not establish live AWS capacity, WAN behavior, or production availability.

## Verification status

The complete Python suite passed **108/108 (41 unit + 67 integration)**. The packaged browser run passed **28/28 cases (26 browser workflows + 2 pure scheduler checks)** with retries disabled. The small benchmark rehearsal passed ten files of 16,777,233 bytes, including download integrity and a common active ten-PUT interval; it does not satisfy the target-size requirement. An earlier rehearsal failed in the harness TLS probe before fixture generation or upload; its report is retained. A TLS server-name fix resolved that probe failure.

A separate complete named recovery regression passed **20/20**, including actual process kills and dependency outages. This is one additional regression pass, separate from the historical Sprint 3 60/60 release denominator.

The target-size three-run denominator, scored throughput/latencies, resource peaks, and exact delivery-commit GitHub CI remain pending. Existing Sprint 2 large-file and Sprint 3 recovery evidence remain separate historical measurements.

## Regression gates

Before completion, run the full Python suite and packaged browser workflows, fix failures, and run one complete named 20-case recovery matrix against the final implementation. Preserve the recovery matrix's own denominator. The final code and evidence are delivered directly to `main`, with exact-commit GitHub CI checked after pushing. Existing data, other projects and containers, and the resume remain outside test cleanup.

## Reproduce the isolated benchmark

From the repository root, install the pinned Python/npm dependencies, install Playwright Chromium, and build the host assets used for image comparison. Ensure ports 18093, 15439, 16386, 19013, and 19014 are available. The project uses its own database, Redis database, disk-backed bucket and named volumes; do not substitute normal application endpoints.

```bash
source .venv/bin/activate
npm ci
npm run build
npx playwright install chromium
docker compose -p clouddrive-sprint4-load -f compose.load.yaml build api minio
docker compose -p clouddrive-sprint4-load -f compose.load.yaml pull postgres redis gateway
docker compose -p clouddrive-sprint4-load -f compose.load.yaml up --no-build --pull never -d --wait
# Development smoke, ten small files; this cannot satisfy the 500 MB target.
CLOUDDRIVE_TEST_PYTHON=python npm run bench:concurrent -- --bytes 16777233 --runs 1 --output test-results/load/smoke-1.json
# Commit implementation first. A full scored run requires a clean checkout.
CLOUDDRIVE_TEST_PYTHON=python npm run bench:concurrent -- --output test-results/load/target-1.json
# Only this disposable project's volumes are removed.
docker compose -p clouddrive-sprint4-load -f compose.load.yaml down -v
```

Output paths are exclusive-create: use a new name for every attempt. A report is checkpointed during the run; `status: running` after an interruption is incomplete evidence. Failed cohorts remain in their report and a complete rerun uses a new report. `--channel chrome` selects an installed Chrome; omit it for Playwright Chromium. The runner checks source/asset hashes against the packaged API, requires API/worker/beat to share one immutable image, and verifies frozen source/HEAD at the end. It never installs the gateway CA in the host trust store.

Ordinary CI adds one small ten-file smoke after the full host and packaged regression suites. The manual **Ten concurrent uploads** workflow defaults to three target-size cohorts and retains artifacts for 30 days. These jobs use distinct disposable Compose projects and never publish raw gateway request/error logs, which could contain signed URLs. Reports retain only normalized route templates, file IDs, safe status/timing fields and public certificate fingerprints.

## Observability and operational limits

`GET /api/metrics` requires the workspace bearer key and exposes only that workspace's state counts, processing outbox/claim/verification statistics and API latency observations. Latency histograms are non-cumulative buckets measured through response headers; recent p50/p95 use at most 512 samples. The in-process workspace LRU is bounded to 128 entries, resets on restart/eviction, and does not aggregate across API replicas.

`GET /api/ops` is disabled by default (404). Set a separate private `CLOUDDRIVE_METRICS_KEY` and send it in `X-Metrics-Key` to inspect aggregate database work and the current API process's pool/inflight state. A workspace bearer key cannot authorize this endpoint. Its pool statistics cover this API process, not Celery's separate pools. The benchmark separately samples PostgreSQL connection/lock statistics across its dedicated database; those counts include the observer connection. Redis observations include the default Celery priority-list queue and Kombu unacknowledged hash/index; they are sampled broker delivery state, not a guarantee that no worker has prefetched a task.

Every API response carries a validated or generated `X-Request-ID`. Structured logs connect request/route/status/duration to durable file IDs; worker events include file/task IDs, task outcomes, actual verification commits and processing duration. Outbox-publication totals are scoped to the isolated stack and cohort time window; unlike task/verification events they contain no file IDs. These logs exclude credentials, signed queries, raw exception text and filenames. The packaged Uvicorn command disables its separate raw access log.

Admission counts `UPLOADING`, `FINALIZING` and `PROCESSING` across the workspace and globally under a PostgreSQL transaction advisory lock. Existing request-ID replays remain available at capacity. Capacity responses are 429 with `Retry-After`; database-pool exhaustion is retryable 503. Browser initiation/completion capacity retries stop after eight attempts and retain a selectable paused upload with its original request ID. Part retries retain the existing four-attempt bound. Slots are released before backoff; pausing or switching workspace cancels queued acquisition and active work. The target benchmark is deliberately stricter than these recovery semantics: it requires exactly the expected number of successful PUTs and reports zero PUT retries to pass.

Machine-readable exports use the standard-library exporter:

```bash
python scripts/summarize-load.py test-results/load/target-1.json test-results/load/export-1
```

It exclusively creates cohort, per-file and per-service resource CSVs with LF line endings plus a summary JSON. It preserves missing measurements as null/blank and separates verified file integrity from accepted complete cohorts. Raw report and exporter hashes are included.

Initial regression evidence: [Python](evidence/sprint-4/python.txt), [JUnit](evidence/sprint-4/python.xml), [packaged browser](evidence/sprint-4/browser.txt), [named recovery](evidence/sprint-4/recovery.json), [final small smoke](evidence/sprint-4/smoke-3.json), [earlier probe failure](evidence/sprint-4/smoke-1.json), and [attempt history](evidence/sprint-4/run-history.json).
