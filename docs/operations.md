# CloudDrive operations and disaster recovery

These procedures operate on one explicitly selected CloudDrive deployment. They
do not authorize stopping unrelated projects or deleting existing data. The
automated exercise runs only on fresh GitHub-hosted runners. Do not run its Docker
stacks or large benchmarks on the shared laptop without a separate idle window.

## Backup contract

`python -m clouddrive.operations backup DIRECTORY --offline-confirmed` creates a
new private directory, JSONL copies of all four application tables, streamed
copies of committed objects, and a completion manifest with SHA-256 checksums,
migration checksums, row counts, backup window and recovery-point timestamp. Copied data and directories are fsynced before an atomic, non-replacing completion marker is published. Use a filesystem that supports these durability operations; failures are reported rather than silently weakening publication.
Directories must not already exist. A missing manifest means an incomplete,
unrestorable backup. The original database, objects and multipart sessions are
not changed. The tool does not back up Redis: PostgreSQL's durable processing
state and regenerated outbox replace transient broker deliveries after restore.

Stop **all** API writers, workers and beat processes, including other replicas,
and disable ingress before confirming offline mode. Drain requests first. Record
the last accepted write and retain source disks. Existing signed PUTs may still
change unfinished multipart sessions, which are deliberately not portable. A
SHARE lock on all application tables fences database writes during capture, with
a ten-second lock timeout; it is additional protection, not an online-backup
claim. Do not allow outside tools, storage lifecycle rules or administrators to
modify committed objects during the backup window. READY objects are immutable
through CloudDrive. An absent/corrupt READY or PROCESSING object fails capture;
READY size, source hash and worker hash must agree with copied bytes.

For the standard Compose deployment, select the project and absolute archive
parent yourself. The following commands are **manual maintenance instructions**;
they have not been run against the existing laptop deployment:

```bash
export CLOUDDRIVE_PROJECT=YOUR_EXACT_PROJECT
export CLOUDDRIVE_ARCHIVE_PARENT=/absolute/private/backups
mkdir -p "$CLOUDDRIVE_ARCHIVE_PARENT"
chmod 700 "$CLOUDDRIVE_ARCHIVE_PARENT"
# Disable ingress and stop every other replica before this point.
docker compose -p "$CLOUDDRIVE_PROJECT" -f compose.yaml stop api worker beat
docker compose -p "$CLOUDDRIVE_PROJECT" -f compose.yaml run --rm --no-deps \
  --user "$(id -u):$(id -g)" -v "$CLOUDDRIVE_ARCHIVE_PARENT:/backups" \
  api python -m clouddrive.operations backup /backups/snapshot-001 --offline-confirmed
docker compose -p "$CLOUDDRIVE_PROJECT" -f compose.yaml run --rm --no-deps \
  --user "$(id -u):$(id -g)" -v "$CLOUDDRIVE_ARCHIVE_PARENT:/backups" \
  api python -m clouddrive.operations verify /backups/snapshot-001
# Check both exit statuses and the completion manifest before restarting.
docker compose -p "$CLOUDDRIVE_PROJECT" -f compose.yaml up -d api worker beat
```

The backup is private: it contains original filenames, object bytes, workspace
token hashes and previews. Exported bearer keys remain separately owned secrets;
hashed keys in the database cannot reconstruct a lost bearer key. Do not commit
archives or attach them to CI artifacts. Store encrypted, access-restricted copies
off the application host and test decryption/restore. The `verify DIRECTORY` command checks archive integrity without a database or storage connection. Hashes detect accidental
corruption; they do not authenticate an archive against a malicious modifier.
Keep separate immutable copies under an operator-defined retention policy.
Retention/encryption/off-host transport are operator responsibilities, not
automatically configured guarantees.

## Restore contract and in-progress policy

Restore only into a **new, empty, offline database and bucket**, using the same
application migration version as the archive. For an older archive, first restore with its matching application image, then start the newer application to apply additive migrations. The database must first be migrated
and the empty bucket created. The tool checks the exact database and bucket
confirmation strings, checks archive integrity before writing, rejects populated
destinations (including hidden object versions and delete markers), rechecks storage after taking the metadata lock, verifies copied object bytes by streaming them back, and commits
metadata only after all copies pass. It never truncates tables or clears a bucket.
Keep destination API/worker/beat replicas stopped until it exits successfully.
Do not point it at a shared bucket. The restore credential needs `ListBucketVersions` as well as ordinary object/list/multipart permissions so hidden data can be detected; denied inspection fails closed. Check endpoint/region/database configuration
independently: confirmation names do not uniquely identify a server/account.

| State at recovery point                          | Restored behavior                                                                                           |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| READY                                            | Preserve identity, bytes, SHA-256, preview and original workspace access                                    |
| PROCESSING                                       | Preserve committed object, clear claims/leases, enqueue verification again                                  |
| FINALIZING with committed object                 | Clear claims, enqueue verification again                                                                    |
| UPLOADING or FINALIZING without committed object | EXPIRED; discard non-portable multipart IDs/receipts; reselect original contents to restart with a fresh ID |
| FAILED                                           | Preserve failure and any committed object; explicit retry remains available                                 |
| DELETING / DELETED                               | DELETED, without copying explicitly deleted bytes                                                           |
| EXPIRED                                          | Preserve expiry; original file can be reselected                                                            |

Existing S3 multipart sessions belong to their original bucket and cannot be
resumed across this restore. Bytes that had not become committed objects at the
recovery point are not covered by committed-object recovery. All original table
rows/receipts/outbox contents are retained in the private archive for inspection;
obsolete receipts and broker messages are intentionally not loaded into the new
deployment. Restored worker claims and upload IDs are always cleared. Restored
PROCESSING/FINALIZING objects are not downloadable until verification succeeds.

The isolated Compose example uses its own image, volumes, ports and network:

```bash
export OPS_DATABASE=clouddrive_ops_target_test
export OPS_BUCKET=clouddrive-ops-target-test
export OPS_API_PORT=18095
export OPS_STORAGE_PORT=19016
export CLOUDDRIVE_BACKUP_DIRECTORY=/absolute/private/backups
export RESTORE_PROJECT=YOUR_NEW_EMPTY_PROJECT
docker compose -p "$RESTORE_PROJECT" -f compose.operations.yaml build api minio
docker compose -p "$RESTORE_PROJECT" -f compose.operations.yaml up -d --wait postgres redis minio
docker compose -p "$RESTORE_PROJECT" -f compose.operations.yaml run --rm --no-deps \
  --entrypoint python maintenance -c \
  'from clouddrive.db import migrate; from clouddrive.storage import bootstrap; migrate(); bootstrap()'
docker compose -p "$RESTORE_PROJECT" -f compose.operations.yaml run --rm --no-deps \
  --user "$(id -u):$(id -g)" maintenance restore /backups/snapshot-001 \
  --offline-confirmed --confirm-database "$OPS_DATABASE" --confirm-bucket "$OPS_BUCKET"
docker compose -p "$RESTORE_PROJECT" -f compose.operations.yaml up -d --wait api worker beat
```

Import an original key in the packaged browser, compare the READY inventory,
download/hash every expected committed object, verify wrong-workspace access is
denied, and wait for pending objects to finish verification. Check metrics/outbox
and restart an expired upload with its original source. Keep the source intact
until the destination passes. Changing DNS/ingress or deleting the source is a
separate operator decision; the restore exercise does neither.

If restore fails after storage writes, the database transaction rolls back but
S3 may contain partial copies or multipart sessions. Preserve diagnostics and use
another new empty destination for the next attempt. There is no cross-system
transaction or automatic destructive rollback, and a partial bucket will be
refused on retry. Do not bypass that protection by emptying an existing bucket.

## Conservative storage cleanup

`python -m clouddrive.operations cleanup` is a dry run. `--apply` aborts only
multipart sessions older than 24 hours (or a larger `--older-than-seconds`) whose
CloudDrive UUID/UUID object key has no live metadata reference. Every reference
except DELETED/EXPIRED is protected regardless of age or upload-ID mismatch.
This deliberately protects a long-paused/slow upload. Newer sessions and foreign
key formats are untouched. Every abort rechecks metadata under a table SHARE
lock, waiting for an in-flight initializer to commit. `--limit` bounds all
sessions examined per invocation (default 100, maximum 1000); provider pagination
is followed. It does not delete completed objects or expire live sessions by age.

Follow `next_cursor` using `cleanup --cursor TOKEN` until it is null. `--limit` now bounds **all scanned sessions**, including young/protected/foreign ones; carrying the cursor forward prevents protected sessions at the front from starving old orphans later in the bucket. Cursors are tied to the bucket and grace period.

Review dry-run outcomes before applying to an exclusively owned CloudDrive
bucket. Delete unwanted referenced uploads through the authenticated UI/API;
durable DELETING intent and beat reconciliation finish interrupted deletion.
Completed unreferenced objects require separate inventory investigation; this
tool deliberately does not guess which completed bytes an operator wants deleted.
Do not enable a blanket provider MPU lifecycle rule that aborts active valid
uploads. Provider rules that cannot check database references are not equivalent
to this policy. Scheduling the reviewed cleanup command is optional and is not
silently enabled on existing deployments.

## Admission and storage capacity

The default metadata byte budgets are **10,000,000,000 bytes per workspace** and
**50,000,000,000 bytes globally**, configurable with
`CLOUDDRIVE_MAX_STORAGE_BYTES_PER_WORKSPACE` and `CLOUDDRIVE_MAX_STORAGE_BYTES_GLOBAL`.
The standard Compose file now forwards the documented resource, pool, scheduler,
file-limit and metrics settings from `.env`/the shell into app containers.
Choose budgets to fit provisioned storage before deploying; reservations do not
measure provider disk usage, historical object versions or orphan overhead.

A new file reserves its full declared size under the same transaction lock used
for session admission, before S3 allocation. READY, FAILED and DELETING records
retain their reservation; only DELETED/EXPIRED release it. Processing retries and
idempotent replays allocate no additional bytes and remain available at quota.
The quota never removes or hides old data, including data already over a newly
lowered cap. New allocations receive 429/Retry-After with a bounded browser retry.
Workspace metrics expose only the owner's reservation/quota; the private ops
endpoint exposes aggregate reservations. The UI displays workspace capacity.

## Monitoring and incident response

Use `/api/health` for DB/storage reachability, not worker progress. Authenticate
`/api/metrics` with the workspace key to inspect file states, outbox age,
active/expired claims, verification duration and process-local API latencies.
Enable `/api/ops` only with a separate private `CLOUDDRIVE_METRICS_KEY`. It exposes
global pending work and this API process's pool/inflight counts; never publish
that key or endpoint. Request IDs and file/task IDs connect safe structured logs.
Histograms reset on API restart/eviction and are not replica-wide aggregates.

Investigate a backlog that does not drain, expired claims, sustained 429/503,
increasing 5xx, disk exhaustion, or container OOM/restarts. Suggested alert
thresholds must be calibrated to the deployment; no external alert delivery is
configured by this repository. Avoid retry storms or raising caps to hide an
unhealthy worker/storage/database. Restore dependency reachability, check beat
and worker processes, and allow durable outbox/claim recovery to run. FAILED
files can be retried explicitly after the cause is fixed. Never mark a file READY
manually or disable its integrity/download gate.

For deletion incidents, preserve intent and let reconciliation finish; do not
resurrect metadata after object removal. For suspected data corruption, retain
originals, stop writes to affected storage and compare source/worker/download
hashes before using a verified archive. Database/storage credentials, TLS,
least-privilege policies, encryption, rotation, external monitoring and hosting
remain deployment-specific responsibilities. No blanket production-readiness,
RPO, RTO, live AWS, or public-availability claim follows from these tests.

## Pagination

`GET /api/files?limit=50&compact=true` returns compact summaries and `next_cursor`.
Pass that cursor with the same workspace/search/view/sort to retrieve the next page;
the maximum page is 200. Ordering is stable with an ID tie-breaker: `sort=newest` uses descending creation time, `sort=name` uses ascending case-insensitive name, and `sort=size` uses descending byte size. All three sorts operate on the entire matching workspace. Search is literal case-insensitive substring matching over the
whole workspace, and `view=uploads|recent|all` is server-side. Concurrent inserts
appear on refresh; deletion can shorten later pages. This is keyset traversal,
not a transactionally frozen inventory snapshot. Cursors are not credentials.

The browser loads 50 at a time, supports Load more and whole-workspace search,
and requests full receipts/previews only for authenticated file details/resume.
The sort selector applies to the entire workspace before pagination. Late responses from an older query/workspace cannot overwrite a newer refresh. Polling refreshes the currently requested page depth. The
legacy API default retains full receipts for compatibility; opt into compact
responses for browsing. Backup inventory uses the locked snapshot, not browsing.
