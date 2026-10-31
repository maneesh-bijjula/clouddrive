# Post-sprint completeness audit

Status: complete. All fixes and fresh target measurements passed on implementation
`4513d0337ed53bdc0a374e10cd196035b2f34b9d`. The final publication changes only
documentation/evidence; its exact-SHA workflows are checked after pushing.
Audit baseline: `96ed7dc26be687189cd627622129006a76dc3119` on main.

The original five sprint deliverables and three size/recovery/concurrency targets
have permanent evidence in the sprint reports. This follow-up checks edge cases
across those features; it does not add account management, public hosting or
claim execution against live AWS. Existing data, other projects, containers and
the resume remain unchanged. Heavy work runs only on GitHub-hosted runners.

## Gaps corrected

- Session counts alone did not bound total declared storage. Workspace/global
  byte reservations now enforce configurable budgets before allocation, retaining
  READY/FAILED/DELETING reservations until cleanup completes. The standard Compose
  file forwards documented settings, so `.env` changes actually reach containers.
- Backup completion was written last but without durable file/directory flushes.
  Copied data is now fsynced, and the completion marker is atomically published
  without replacing an existing marker; flush failures prevent completion.
- Restore checked visible objects and MPUs only, overlooking historical versions
  and delete markers. It now inspects versions and rechecks bucket emptiness
  after acquiring the metadata lock, preventing a second restore from silently
  reusing a partial copy left while it waited.
- Cleanup could repeatedly stop at the same protected uploads. Pages now bound
  all scanned sessions and expose a bucket/grace-bound continuation cursor so
  protected, young and foreign entries cannot starve later orphan cleanup.
- File sorting was limited to loaded rows. Newest/name/size ordering now applies
  to all matching files before stable keyset pagination, with additive indexes.
  Cursors bind the sort and support long Unicode names.
- An older listing could overwrite a forced newer refresh of the same query.
  Revision fencing now discards stale results/errors, and workspace epochs fence
  late details/initialization after an access-key switch.
- Reconciliation and Celery retry diagnostics could render raw provider text.
  These paths now emit correlated IDs/error types with sanitized task errors.
- The 5 GB harness did not check packaged source/assets or source changes through
  the entire run. It now checks immutable app images, packaged Python/SQL/assets,
  clean target-size source, and frozen HEAD/source. Migration digests are also
  included in restore and concurrency measurement provenance.

## Requirement coverage

| Agreed requirement                           | Implementation and current proof                                                                                                                                          |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FastAPI/PostgreSQL/private browser workspace | Packaged API, additive/checksummed migrations, hashed workspace keys and complete wrong-workspace operation tests                                                         |
| Multipart up to 5 GB                         | Inclusive **5,000,000,000 bytes**, 8 MiB parts, bigint metadata, size rejection before allocation; fresh real transfer/download passed                                    |
| Resumable browser uploads and integrity      | Whole-file identity, persisted/reconciled part receipts, bounded workers/buffers, pause/reload/reselection, independent worker/download SHA-256                           |
| Idempotent completion/deletion races         | Durable FINALIZING/DELETING intent, row locks, outbox recovery, worker-claim fencing and deterministic race/process-fault cases                                           |
| 20/20 recovery without data loss             | One fresh complete **20/20 distinct named cases** on this implementation; each records its fault and exact recovered bytes/SHA-256                                        |
| Ten concurrent 500 MB files                  | Three fresh complete **10 × 500,000,000-byte** cohorts, **30/30**, with ten-way byte-transfer overlap and independently verified downloads                                |
| Isolation and resource limits                | Private objects/keys, workspace/global atomic session and byte budgets, bounded API/DB pools and browser slots; unchanged declared load-profile CPU/RAM/disk/time budgets |
| Observability                                | Safe request/file/task correlation, bounded workspace metrics, private aggregate ops metrics, timing/backlog/claim/resource evidence                                      |
| Backup/restore and cleanup                   | Fsynced/atomic private archives, empty/version-aware restore, fresh-project recovery, conservative resumable cleanup protecting live references                           |
| Browsing, packaging and delivery             | Whole-workspace search/sort, compact cursor pagination, Docker workflow/config validation, packaged browser checks, main-only delivery and exact-commit CI                |

## Current regression verification

[Ordinary CI](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41145781625),
[recovery CI](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41145781631), and
[restore verification](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41145781609)
all passed on `4513d03`:

- **140/140 Python cases = 52 unit + 88 integration**, zero skips/failures/errors.
  One existing Starlette/TestClient deprecation warning remains documented.
- **31/31 host and 31/31 packaged Playwright cases** per mode, consisting of
  29 browser workflows and two pure scheduler checks, with zero runner retries.
- **9/9** Node observer/hash-helper tests, TypeScript/Vite build, Black/Prettier
  and standard Compose configuration validation passed.
- **20/20 distinct recovery cases**, one complete additional regression pass;
  this is separate from Sprint 3's historical three-pass 60-attempt denominator.
- Fresh restore recovered **3/3 committed objects, 16,804,502 bytes total**,
  preserved both original keys, reverified pending processing, and restarted the
  separate unfinished 12,500,000-byte file through the packaged browser.
  Source inventory was unchanged. Recorded backup/restore clocks were
  **0.2798/0.4035 s** for those small fixtures; these exclude infrastructure
  startup/failover and are not RPO/RTO guarantees. The backup clock reaches
  member-data flush, immediately before final completion-marker publication.
  The locked offline recovery point was **2026-10-30 23:43:25.120294 UTC**;
  backup ran 23:43:24.896734–23:43:25.176537 UTC and restore ran
  23:43:32.597234–23:43:33.000728 UTC. No writes were accepted during the snapshot.

Proof: [JUnit](evidence/final-audit/python.xml),
[regression summary](evidence/final-audit/regression-summary.txt),
[recovery matrix](evidence/final-audit/recovery-matrix.json),
[restore report](evidence/final-audit/restore-41145781609/results.json),
[independent validation](evidence/final-audit/independent-validation.json), and
[attempt history](evidence/final-audit/run-history.json). Earlier formatting and
legacy-exception-assertion failures are retained separately from passing outcomes.

## Fresh 5 GB measurement

[Hosted run 41146257083](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41146257083)
passed once on `4513d03`, October 30, 2026, **16:48:03–16:54:04
America/Los_Angeles**. One actual **5,000,000,000-byte** file reached READY,
with source/worker/download SHA-256
`3d38c1add12fbf81b468f29b946c4a30d092b8c2f5310eb89d1c8cbe62711817`.
All **597** parts were acknowledged. Parts **1, 2, 3** were acknowledged before
pause/reload and none was resent. The first phase opened six PUT requests; three
were gate-held/canceled for the forced interruption. Resume issued parts 4–597;
there were **597 successful storage PUT responses** overall. API and browser
rejected **5 GB + 1 byte**. The test object was deleted afterward.

| Clock                             | Measured scope                                                                                                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fixture generation                | 6.7446 s, streamed deterministic AES-CTR fixture on disk                                                                                                            |
| Initial/resume browser hashing    | 87.7583 / 87.2200 s to initiation/session lookup                                                                                                                    |
| Active transfer windows           | **125.4654 s** total = 0.8095 + 124.6559 s; includes inter-part hashing/signing/ack gaps, excludes both whole-file prehashes, pause and later verification/download |
| Worker stream verification        | 6.2377 s                                                                                                                                                            |
| Download / independent local hash | 19.0816 / 12.1192 s                                                                                                                                                 |
| Complete runner                   | **361.6497 s**, fixture through cleanup/final checks                                                                                                                |

This used a separate GitHub-hosted Linux x64 runner with **four allocated vCPUs**,
AMD EPYC 7763 processor family, **16,760,344,576 bytes RAM**, Docker 28.0.4,
Node 24.22.1 and Chromium 156.0.8078.4. MinIO used the pinned source build;
PostgreSQL/Redis/MinIO and immutable application image identities are recorded.
Browser process-RSS sum peaked at **851.09 MiB**, Node RSS at **418.88 MiB**,
with zero sampling errors. These are sampled observations, not instantaneous
maxima. This is one MinIO/HTTP browser transfer, not live AWS/WAN bandwidth,
concurrency evidence or a production performance guarantee. All 32 recorded source
hashes, packaged Python/SQL/assets and application image roles matched; source/HEAD
remained frozen and the checkout stayed clean.

[Raw report](evidence/final-audit/large-41146257083/run.json),
[verified-file CSV](evidence/final-audit/large-41146257083/verified-file.csv),
[transcript](evidence/final-audit/large-41146257083/run.txt), and
[workflow/attempt](evidence/final-audit/large-workflow-attempt.json).

## Fresh concurrency measurement

[Hosted run 41146254418](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41146254418)
passed three complete cohorts on `4513d03`, October 30, 2026,
**16:47:25–16:55:53 America/Los_Angeles**. Each cohort used ten distinct
500,000,000-byte sources and fresh workspace/file IDs; the ten deterministic
contents were reused between cohorts. All **30/30** source/worker/download byte
counts and hashes matched, with one committed result/object per file.

| Cohort | Verified files | Common ten-PUT byte overlap | Transfer window | Aggregate window MB/s | Selection → all READY | Native API p50 / p95 |
| ------ | -------------: | --------------------------: | --------------: | --------------------: | --------------------: | -------------------: |
| 1      |          10/10 |                    0.3995 s |       68.2911 s |               73.2160 |            117.1976 s |  32.007 / 144.484 ms |
| 2      |          10/10 |                    0.5054 s |       70.3746 s |               71.0484 |            117.7464 s |  32.518 / 142.204 ms |
| 3      |          10/10 |                    0.3989 s |       73.3189 s |               68.1952 |            123.2172 s |  37.529 / 137.642 ms |

Each rate divides **5,000,000,000 source bytes** by the barrier-release-to-last-XHR
upload.loadend window and by 1,000,000. It includes inter-part hashing/sign/ack
gaps and the declared 8 MiB/s overlap probe; actual shaped byte-transfer probes
lasted **0.211848 / 0.213422 / 0.229912 s**, removed before any first PUT ended.
The shaping setting was configured earlier during preparation; that idle/prehash
time is not shaped byte-transfer time. Selection-to-READY includes preparation,
the barrier, controls, transfer and processing; READY is a polling observation.
Rates are not unshaped storage bandwidth. The same TLS/HTTP2 gateway profile and
scope described in [Sprint 4](sprint-4.md) apply. Default HTTP/1 and WAN capacity
are not established by this result.

There were **1,800 successful HTTP/2 PUTs**, zero part retries, 30 processing
attempts, zero reported task retry ordinals, and 30 outbox publications in the
isolated cohort windows. Owner downloads before verification returned 409 and
wrong-workspace downloads before/after verification returned 404 for all 30
files. Native API populations were 1,316 / 1,317 / 1,318 completed requests;
p50/p95 are nearest-rank response-end timings including statuses and excluding
Node observer probes. No native 5xx/transport failures or browser errors occurred.

This runner had **four allocated vCPUs**, AMD EPYC **9V45** processor family,
**16,760,344,576 bytes RAM**, Docker 28.0.4, Node 24.22.1 and Chromium 156.0.8078.4.
The CPU brand's 96-core label is not the allocated core count. No other containers
were running in this job. Underlying physical storage hardware was not isolated;
test volumes were disk-backed. Source/provider/image IDs and effective settings
are in the raw report. Different hardware/shared-runner conditions prevent
attributing differences from earlier measurements to a particular code change.

| Service  | Peak sampled CPU | Peak Docker memory | Peak process-RSS sum |
| -------- | ---------------: | -----------------: | -------------------: |
| api      |           54.41% |         101.60 MiB |           138.47 MiB |
| beat     |            3.32% |          71.09 MiB |            87.82 MiB |
| gateway  |           90.04% |          50.16 MiB |            63.36 MiB |
| minio    |           39.64% |         435.20 MiB |           485.23 MiB |
| postgres |           12.83% |          55.73 MiB |           290.02 MiB |
| redis    |            5.07% |           7.91 MiB |            20.20 MiB |
| worker   |           35.50% |         194.80 MiB |           305.78 MiB |

Browser sampled RSS sum peaked at **739.18 MiB** (ceiling 1536 MiB), Node at
**389.82 MiB** (ceiling 512 MiB). The unchanged CPU/memory/disk/time budget manifest
SHA-256 is `6f569bfdadd524c30d371bf3e1211024c55dec633355233f19f610a68e4d8e26`.
The new metadata byte quotas, fixed before scoring, were 10 GB/workspace and
50 GB/global, alongside ten active files/two hashes/ten global parts/three part
loops per file. All 38 recorded source hashes matched throughout.

There were **205 resource collections**, zero collection errors and 284 explicitly
skipped overlapping ticks. Actual median start spacing was **2.0012 s** despite
the nominal 1 s timer; longest collection was **3.2392 s**. Sampling covers cohort
preparation through download checks; fixture generation and teardown are timed
separately. The runner took **507.6627 s**, including **12.8017 s** shared fixture
generation/independent hashing. Per-file clocks and all samples are exported.
RSS sums can double-count shared pages; per-component peaks need not coincide.

Pool checkouts peaked at **5/16**, overflow 0, PostgreSQL connections 11 including
the observer, lock waiters/ungranted locks/deadlocks 0, outbox backlog 10 with oldest
age 1.8867 s, broker queued/unacked 8/2, active claims 2 and expired claims 0.
Read-only connection cleanup contributes to rollback counters; these are not an
error denominator. All seven services had zero cgroup OOM/oom_kill increments.
After scoped cleanup, global objects, MPUs, outbox work, processing/claims and
broker deliveries were zero; metadata tombstones remain intentionally.

[Raw report](evidence/final-audit/concurrent-41146254418/results.json),
[summary](evidence/final-audit/concurrent-41146254418/export/summary.json),
[cohorts CSV](evidence/final-audit/concurrent-41146254418/export/cohorts.csv),
[30-file CSV](evidence/final-audit/concurrent-41146254418/export/files.csv),
[resource CSV](evidence/final-audit/concurrent-41146254418/export/resources.csv), and
[workflow/attempt](evidence/final-audit/concurrent-workflow-attempt.json).
Both target workflows were attempt 1; no hidden full-file/cohort reruns were used.

## Reproduction and scope

Use the existing hosted workflows **Large upload verification** with
`bytes=5000000000`, **Ten concurrent uploads** with `bytes=500000000,runs=3`,
and the ordinary recovery/restore gates. Manual large-file measurements now
require `npm run build` before the benchmark so packaged frontend assets can be
compared. See [operations](operations.md) for quota/configuration, durable backup,
version-aware restore and cleanup cursor commands. CSVs are LF direct exports of
raw outcomes; the concurrency exporter is `scripts/summarize-load.py`.

Private archives, workspace keys, signed URLs and raw request logs were excluded
from published evidence. All execution used isolated projects, databases and
buckets. No heavy local tests ran, and no existing data, containers, other project
or resume was changed. Original sprint measurements remain historical records.

Live AWS execution, public hosting, user accounts/role management, malware
scanning, deployment TLS/credential rotation, off-site encryption/retention and
external alert delivery are outside the agreed implementation scope or require
operator configuration. The project does not claim blanket production readiness
or contractual availability/RPO/RTO from these results.
