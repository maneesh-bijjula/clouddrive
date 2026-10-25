-- Preserve existing rows and the checksum of 001_initial.sql.
ALTER TABLE files DROP CONSTRAINT files_status_check;
ALTER TABLE files ADD CONSTRAINT files_status_check CHECK (
    status IN ('UPLOADING','FINALIZING','PROCESSING','READY','FAILED','DELETING','DELETED','EXPIRED')
);
CREATE INDEX files_recovery ON files(status, updated_at, id)
    WHERE status IN ('DELETING','FINALIZING');
