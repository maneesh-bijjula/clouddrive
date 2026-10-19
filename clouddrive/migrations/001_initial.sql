CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE files (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    request_id uuid NOT NULL,
    name text NOT NULL,
    media_type text NOT NULL,
    size bigint NOT NULL CHECK (size > 0),
    expected_sha256 text NOT NULL CHECK (length(expected_sha256) = 64),
    part_size integer NOT NULL CHECK (part_size >= 5242880),
    object_key text NOT NULL UNIQUE,
    upload_id text,
    status text NOT NULL CHECK (status IN ('UPLOADING','FINALIZING','PROCESSING','READY','FAILED','DELETED')),
    sha256 text,
    result jsonb,
    error text,
    claim uuid,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, request_id)
);
CREATE INDEX files_workspace_created ON files(workspace_id, created_at DESC);
CREATE INDEX files_processing ON files(status, lease_until);
CREATE TABLE upload_parts (
    file_id uuid NOT NULL REFERENCES files(id),
    number integer NOT NULL CHECK (number > 0),
    checksum text NOT NULL,
    etag text,
    PRIMARY KEY (file_id, number)
);
CREATE TABLE processing_outbox (
    file_id uuid PRIMARY KEY REFERENCES files(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
