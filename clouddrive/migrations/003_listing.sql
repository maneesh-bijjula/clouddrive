-- Stable keyset pagination; keep earlier migration checksums unchanged.
CREATE INDEX files_workspace_page ON files(workspace_id, created_at DESC, id DESC)
    WHERE status NOT IN ('DELETING','DELETED');
