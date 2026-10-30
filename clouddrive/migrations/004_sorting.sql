-- Sort the whole workspace with stable keyset pagination, preserving existing rows.
CREATE INDEX files_workspace_name ON files(workspace_id, lower(name), id)
    WHERE status NOT IN ('DELETING','DELETED');
CREATE INDEX files_workspace_size ON files(workspace_id, size DESC, id DESC)
    WHERE status NOT IN ('DELETING','DELETED');
