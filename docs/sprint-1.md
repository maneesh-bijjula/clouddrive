# Sprint 1: correctness and recovery states

Implemented directly on `main`, extending baseline `6f439d97b62d25b16ef22d9ab91208955bc97983`. The existing deployment, original workspace volumes, Projects 1 and 2, and the resume were not modified during verification.

## Delivered behavior

- **Durable deletion:** the API commits `DELETING`, clears the worker claim, and removes outbox work before calling storage. Browsing, inspection, signing, completion, retry, and new download authorization treat a deleting file as absent. Storage cleanup transitions it to `DELETED`. Repeating DELETE or periodic reconciliation finishes interrupted cleanup safely.
- **Permanent expiry:** an explicit storage `NoSuchUpload` during signing, receipt confirmation, or finalization commits `EXPIRED` before returning HTTP 409 with `detail.code=upload_expired`. Already expired completion returns its terminal description. Processing retry is rejected, and the scheduler no longer selects it. Access errors, missing buckets, and transient storage errors do not incorrectly expire a session.
- **Browser restart:** an expired row offers Restart. Reselecting the original file requires matching size and full-file SHA-256 before retiring the expired session and creating a fresh request. Changed content is rejected. Deletion removes the saved request fingerprint, and stale fingerprints from another tab recover with a new request ID. Request IDs are scoped to the workspace.
- **Workspace switching and duplicate selection:** each transfer captures its workspace key; paused requests cannot continue uploading under a newly imported key. Responses from a previous workspace cannot overwrite the current file list. Pending file-selection batches stop on workspace changes. Duplicate active selections share neither a second session nor a second transfer.
- **Recovery scheduling:** independent deletion/finalization budgets and per-file transactions keep one failure from rolling back another file's recovery. Reconciliation runs before outbox publication so a publication error does not skip cleanup in an executing dispatch task. Dispatch still depends on Redis for delivery; this does not provide broker-independent scheduling.
- **Packaged CI:** the workflow tests both host processes and a separately built Docker API/worker/beat stack. Reports have separate directories, and Playwright no longer erases Python/build logs when clearing its browser output directory.

## Verification scope

Python coverage includes failures before durable intent, during multipart abort/object deletion, and after object removal before the metadata update; an actual child-process SIGKILL after object deletion; both completion/delete orderings with a confirmed PostgreSQL lock wait; simultaneous completion; worker streaming while deletion cleanup fails; recovery despite another file/publication failing; terminal expiry across API and scheduler paths; and seven file operations under wrong/missing/invalid workspace keys across eight states.

A populated baseline-schema migration test preserves all original file/workspace/part/outbox rows and an actual stored object, applies the additive migration twice, and verifies both new states are accepted. `001_initial.sql` is unchanged.

Browser coverage adds interrupted delete/re-upload, deletion in another tab with/without reload, expired restart and wrong-content rejection, duplicate active selection, and workspace switching while an old initiation response is held. Downloads in the new successful browser workflows are compared with the original bytes and SHA-256.

Final results and reproduction commands are in [verification notes](verification.md). Committed evidence under `evidence/sprint-1/` records local outcomes; GitHub Actions records the tested pushed SHA. The individual cases, parameterizations, and repeated suites are not the future 20-case recovery denominator. No 5 GB or ten-concurrent-500 MB benchmark was performed in Sprint 1.

## Upgrade and recovery notes

Migration `002_recovery_states.sql` extends the status constraint and adds a partial recovery index. It does not delete or recreate existing rows or change the first migration's checksum. Migration runs under the existing startup advisory lock. Deploy API, workers, and beat from the same revision, retaining named data volumes; refresh the browser to load the matching UI. Do not use `down -v` or test cleanup fixtures against the normal deployment.

A deletion may return 503 after its intent has committed. The file stays unavailable while storage is unreachable. Repeating DELETE is safe; the scheduler retries intents older than 30 seconds when its dependencies are working. Operational recovery timing under prolonged outages is not an availability guarantee.

An expired multipart upload has already lost its storage parts. Restart re-uploads the original local file; it cannot recover bytes discarded by the provider. Download URLs issued before deletion remain bearer URLs until their expiration, although removal of the object normally makes them unusable sooner.

Migration cannot reconstruct deletion intent from failed deletes made by the old implementation. If an old READY row points to a missing object, investigate and repeat the authorized deletion rather than deleting unrelated metadata automatically.

The existing Starlette/httpx TestClient deprecation warning remains visible and non-fatal. Dependency migration is deferred; no warning is suppressed.

## Remaining work

Sprint 2 enables and measures decimal 5 GB uploads. Sprint 3 establishes the explicit 20-case recovery matrix. Sprint 4 measures ten concurrent decimal 500 MB uploads and adds resource/observability controls. Sprint 5 completes backup/restore, cleanup policy, pagination, and release evidence. See [the completion plan](completion-plan.md).
