# Sprint 5: backup/restore and release evidence

Status: implementation in progress; hosted verification has not yet established
the release gate. See [operations](operations.md) for the backup consistency,
in-progress recovery, empty-destination, cleanup and pagination contracts.

Deliverables include versioned private metadata/object archives, checksum
validation, empty-target restore with renewed pending verification, conservative
multipart cleanup, compact keyset pagination and whole-workspace search,
packaged-browser verification, a separate hosted restore workflow, and operational
runbooks. Migration 003 adds only an index; prior migration checksums and existing
data are preserved. All work is delivered directly to main.

The hosted exercise creates a known source inventory in a fresh Compose project,
stops source writers, records the backup window/recovery point, restores into a
second fresh project/database/bucket, verifies every committed download and both
workspace keys, checks isolation, reprocesses pending work, and restarts an
unfinished upload through the packaged browser. It records actual restore time;
it does not assert a contractual RPO/RTO. Private archives and keys are excluded
from public artifacts. Heavy jobs do not run on the shared laptop.

Final counts, immutable source/workflow identity, results, and final CI links will
be published only after the complete gates pass. Earlier Sprint 2/Sprint 4 size
measurements and Sprint 3 recovery denominators remain separately scoped.
