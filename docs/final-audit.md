# Post-sprint completeness audit

Status: fixes implemented; hosted verification and fresh target measurements pending.
Audit baseline: `96ed7dc26be687189cd627622129006a76dc3119` on main.

The original five sprint deliverables and three size/recovery/concurrency targets
have permanent evidence in the sprint reports. This follow-up checks edge cases
across those features; it does not add account management, public hosting or
claim execution against live AWS. Existing data, other projects, containers and
the resume remain unchanged. Heavy work runs only on GitHub-hosted runners.

## Gaps corrected

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

## Verification contract

Run the complete Python suite, both host and packaged Playwright workflows,
lightweight Node helpers, the named 20-case recovery regression, and a fresh
isolated restore. Repeat the three complete ten-file 500 MB cohorts and one
5,000,000,000-byte pause/reload/resume/download run using hosted manual workflows.
Publish exact source/run identities, denominators, failures and measurement
scope only after results exist. Preserve prior results as historical evidence.

Read [operations](operations.md) for updated commands and policies. Restore
requires version-list permissions and an empty offline destination. Older
archives must be restored with their matching schema/image before upgrading.
Cleanup defaults to dry-run; continue through `next_cursor` with `--cursor`.
The original 5 GB/500 MB requirements use decimal bytes; MiB/GiB remain binary.
