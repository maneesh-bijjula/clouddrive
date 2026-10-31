# Sprint 5: backup/restore and release evidence

Subsequent hardening adds durable archive publication, version-aware restore, cleanup continuation and whole-workspace sorting; see the [current completeness audit](final-audit.md). Measurements below retain their original source/scope.

Sprint 5 is complete. The implementation passed ordinary CI, the named recovery
regression, and the isolated restore exercise.
Implementation: `d723e4edca9e6767c6c43a41b73a885bc9a2747f`, delivered directly to
`main`. No normal deployment, other project, resume, or existing data was changed.
Heavy tests ran on GitHub-hosted runners, not the shared laptop.

## Delivered behavior

| Deliverable              | Implementation and acceptance evidence                                                                                                                                                                               |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Metadata/object backup   | Private format-1 JSONL archive of all four tables, migration/metadata/object checksums and counts, bounded streaming copies, offline write fencing, completion manifest written last                                 |
| Empty-target restore     | Exact destination-name confirmations, archive validation before destination writes, populated DB/bucket refusal, streamed read-back verification, atomic metadata commit; no truncation or automatic bucket clearing |
| In-progress recovery     | Committed pending objects are reverified through regenerated outbox work with cleared claims; non-portable unfinished multipart sessions become EXPIRED and can be restarted from original contents                  |
| Cleanup policy           | Dry run by default, minimum 24-hour grace, bounded provider-paginated orphan MPU cleanup, live-reference protection under a DB lock, foreign key formats skipped; completed objects are never deleted by this tool   |
| Pagination/compact lists | Stable timestamp/UUID keyset cursor bound to workspace/filter; maximum 200 per page; browser pages of 50 with Load more; whole-workspace literal search; details fetch full receipts/previews separately             |
| Operations/release       | Backup, archive verification, restore, cleanup, monitoring and incident runbooks; dedicated hosted restore workflow; packaged browser and regression gates; permanent public evidence with private archives excluded |

Migration 003 adds a listing index. Migrations 001/002 retain their original
checksums, and populated-baseline upgrade tests verify metadata/object retention.
An idempotent replay of a restored READY file cannot allocate an unnecessary new
multipart session. Long Unicode searches use bounded cursors. Name/size sorting
applies to loaded rows; pagination is not a frozen inventory snapshot.

See the [operations runbook](operations.md) for exact commands, consistency and
reconciliation rules, failure handling, and deployment boundaries. The CLI is:

```bash
python -m clouddrive.operations --help
python -m clouddrive.operations backup NEW_DIRECTORY --offline-confirmed
python -m clouddrive.operations verify DIRECTORY
python -m clouddrive.operations restore DIRECTORY --offline-confirmed \
  --confirm-database EXACT_EMPTY_DATABASE --confirm-bucket EXACT_EMPTY_BUCKET
python -m clouddrive.operations cleanup
# Review the dry run before adding --apply.
```

Stop every writer and ingress for backup/restore. The offline-confirmation flag
records an operator precondition; it does not stop another process for you. Table
locks provide additional fencing. Restore requires a new, migrated, offline
database and an already-created empty bucket. A failed cross-system restore may
leave copied objects while its DB transaction rolls back; preserve diagnostics
and use another empty target. The tool never destroys the partial destination or
source automatically. Schema versions must match the archive.

## Measured restore exercise

[Hosted run 41020473181](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41020473181)
passed on source `d723e4e`. The exercise ran October 30, 2026,
**00:30:50–00:34:17 America/Los_Angeles** (07:30:50–07:34:17 UTC), on Linux x64
with four allocated CPUs, MinIO, PostgreSQL 17 and Redis 7. All application,
worker, storage and database image identities and 24 source hashes are recorded.
Application containers use the bounded [operations profile](../compose.operations.yaml).
This is small-fixture functional evidence, not a storage-capacity benchmark,
provider-independent performance result, live AWS test, or RPO/RTO guarantee.

The source project contained two workspaces and five file records: two READY,
one PROCESSING with a committed object, one unfinished UPLOADING session with an
acknowledged part, and one explicitly DELETED file. The three committed source
objects totaled **16,804,502 bytes**: 30, 16,777,472 and 27,000 bytes. Restoring
into a separate new Compose project/database/bucket verified **3/3 committed
objects** by exact downloaded byte count and source/worker/download SHA-256.
Both original workspace keys authorized their original workspace IDs; the other
workspace was denied access to the two already-READY downloads. Repeating restore
against the now-populated target was refused.

| Recorded clock/outcome            | Result and scope                                                                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Last source write acknowledgement | `2026-10-30T07:33:29.712222Z`                                                                                                                                |
| Source API/worker/beat stopped    | `2026-10-30T07:33:32.123730Z`                                                                                                                                |
| Backup window                     | `07:33:33.445160Z` through `07:33:33.684231Z`, **0.2391 s**                                                                                                  |
| Actual recovery point             | Locked offline metadata at `2026-10-30T07:33:33.646317Z`                                                                                                     |
| Core restore                      | **0.4511 s**, including archive checks, metadata loading, object copying/read-back and DB commit                                                             |
| Whole exercise                    | **206.3426 s**, including image build, source fixtures, fresh services, backup/restore and browser checks; excludes earlier workflow dependency installation |
| Observed committed-object loss    | **0 of 3 objects** at the recorded recovery point; no accepted source writes during snapshot                                                                 |
| Pending processing                | Committed object restored as PROCESSING, renewed verification completed, download matched                                                                    |
| Unfinished multipart              | Restored as EXPIRED with zero obsolete receipts; original 12,500,000-byte source was reselected in the packaged browser and verified under a fresh file ID   |
| Source preservation               | Source DB/storage inventory remained unchanged after destination verification                                                                                |
| Final destination                 | Four READY objects, two DELETED metadata rows, zero MPUs, active claims or outbox work                                                                       |

The unfinished file is a **separate restart outcome**, not a fourth recovered
committed object. The browser's initial anonymous workspace accounts for the
third final destination workspace; both restored workspace IDs/keys remained
intact. Core restore timing excludes image/provisioning time, worker completion,
browser checks and operator failover. There was no simulated loss of the source
host, no continuous-write workload, and no measured production failover RTO.
Uncommitted multipart bytes require the original source and are outside the
committed-object recovery denominator.

Public evidence: [raw report](evidence/sprint-5/restore-41020473181/results.json),
[workflow identity](evidence/sprint-5/restore-41020473181/workflow.json),
[verified-files CSV](evidence/sprint-5/restore-41020473181/verified-files.csv), and
[independent checks](evidence/sprint-5/restore-41020473181/independent-validation.json).
The raw SHA-256 is
`d4b3af2d68217d5771418e9d85bf1bd48ec28d499cd0f0dd36ce2ac832892d26`.
All 24 recorded source hashes match the implementation. The CSV is a direct
export of `verified_downloads` plus `restarted_download` with separate scope labels
and LF endings. Private archives, bearer keys, filenames belonging to users,
signed URLs and raw request logs are not published.

## Regression and attempt history

Implementation `d723e4e` passed [ordinary CI](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41020473284),
[20-case recovery CI](https://github.com/maneesh-bijjula/clouddrive/actions/runs/41020473334),
and the restore workflow linked above:

| Check                     | Verified denominator                                                                                                                                     |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python                    | **121/121 = 45 unit + 76 integration**, zero skips/failures/errors; one existing TestClient deprecation warning                                          |
| Host Playwright           | **29/29 = 27 browser workflows + 2 scheduler checks**, zero runner retries                                                                               |
| Packaged Playwright       | Same **29/29**, real packaged API/Celery/Redis/storage, zero runner retries                                                                              |
| Named recovery regression | **20/20 distinct cases**, one complete additional pass, each with observed fault and exact integrity; not the historical Sprint 3 60-attempt denominator |
| Lightweight Node helpers  | **9/9**                                                                                                                                                  |
| Small concurrency smoke   | **10/10**, 16,777,233 bytes each; not a new 500 MB target-size measurement                                                                               |
| Restore                   | **3/3 committed objects**, plus a separate 12,500,000-byte browser restart                                                                               |
| Build/format              | TypeScript/Vite, Black and Prettier passed                                                                                                               |

Evidence: [Python JUnit](evidence/sprint-5/python.xml),
[regression summary](evidence/sprint-5/regression-summary.txt),
[recovery matrix](evidence/sprint-5/recovery-matrix.json),
[small smoke](evidence/sprint-5/small-concurrency-smoke.json),
[CI identity](evidence/sprint-5/implementation-ci.json),
[recovery CI identity](evidence/sprint-5/implementation-recovery-ci.json), and
[attempt history](evidence/sprint-5/run-history.json).
The raw recovery matrix's three-pass release flag and small smoke's target-size
release flag are false by design: these are regression/smoke runs with their own
denominators. Sprint 3's three-pass recovery and Sprint 4's 500 MB cohorts retain
their separate published historical evidence.

The final publication changes documentation/evidence only; all 24 recorded
implementation-source hashes remain unchanged. Exact-pushed-commit workflows
are checked after publication. Local verification stayed lightweight; no local
Docker, browser, fixture generation, restore stack or load test was started.

Earlier attempts remain distinct: initial implementation `517072d` passed the
restore exercise, but ordinary CI failed two preexisting migration-count
assertions (116/118 Python cases passed). Updating those expectations retained the
populated-data preservation assertions. On `02971a7`, all 120 Python cases passed
but the new browser seed helper correctly refused normal default settings before
connecting; the other 28 browser cases passed. The helper was corrected to use
explicit isolated browser defaults, and the full suite subsequently passed without hidden
browser retries. Both earlier successful restore reports remain in the evidence
directory; they are not pooled into the release denominator.

Reproduce the hosted exercise by dispatching the **Backup and restore
verification** workflow, or on a fresh GitHub-hosted job run:

```bash
python scripts/verify-restore.py --output test-results/operations/results.json
```

The script refuses local execution, requires fresh project containers/volumes,
and checks the original source inventory after restore. The restore workflow's artifacts contain
only public reports/service identities; private archives are excluded. Scheduled
off-site copies, encryption, retention, external alert delivery, credentials,
production TLS, hosting and live AWS validation remain operator-specific work.
Historical Sprint 2/Sprint 4 size measurements and Sprint 3's 60/60 recovery
attempts retain their original separate scope.
