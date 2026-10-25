"""Historical baseline 6f439d9 audit: reproduce pre-fix gaps in isolated test data.

These assertions document pre-fix behavior and intentionally fail on Sprint 1 code.
Run before starting the packaged worker, using the same isolated environment
as pytest plus PYTHONPATH="$PWD:$PWD/tests" and pytest -p conftest.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from clouddrive import service
from clouddrive.db import engine
from clouddrive.service import query
from clouddrive.storage import head, s3
from clouddrive.config import settings
from clouddrive.worker import process_file, dispatch
from test_integration import begin, uploaded, send_part


def test_delete_storage_success_then_database_failure(client, auth, monkeypatch):
    upload, _ = uploaded(client, auth)
    file_id = UUID(upload["id"])
    assert process_file(file_id) == "ready"
    workspace_id = service.workspace(auth["Authorization"][7:])["id"]
    with engine().connect() as conn:
        key = query(conn, "SELECT object_key FROM files WHERE id=:id", id=file_id).scalar_one()
    original = service.query

    def fail_deleted(conn, sql, **params):
        if "UPDATE files SET status='DELETED'" in sql:
            raise SQLAlchemyError("audit: DB failure after storage deletion")
        return original(conn, sql, **params)

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "query", fail_deleted)
        with pytest.raises(SQLAlchemyError):
            service.delete_file(workspace_id, file_id)
    assert head(key) is None
    assert client.get(f"/api/uploads/{file_id}", headers=auth).json()["status"] == "READY"
    signed = client.get(f"/api/files/{file_id}/download", headers=auth)
    assert signed.status_code == 200
    import httpx

    assert httpx.get(signed.json()["url"]).status_code == 404
    service.delete_file(workspace_id, file_id)
    print(
        "CONFIRMED: failed deletion leaves READY metadata and a signed URL for a missing object; explicit DELETE retry repairs it."
    )


def test_expired_multipart_remains_finalizing(client, auth):
    upload, _ = begin(client, auth)
    file_id = UUID(upload["id"])
    send_part(client, auth, upload, 1, b"Hello, CloudDrive!\n")
    with engine().begin() as conn:
        row = (
            query(conn, "SELECT object_key,upload_id FROM files WHERE id=:id", id=file_id)
            .mappings()
            .one()
        )
        query(
            conn,
            "UPDATE files SET status='FINALIZING',updated_at=now()-interval '1 minute' WHERE id=:id",
            id=file_id,
        )
    s3().abort_multipart_upload(
        Bucket=settings().s3_bucket, Key=row["object_key"], UploadId=row["upload_id"]
    )
    assert client.post(f"/api/uploads/{file_id}/complete", headers=auth).status_code == 409
    dispatch.run()
    assert client.get(f"/api/uploads/{file_id}", headers=auth).json()["status"] == "FINALIZING"
    print(
        "CONFIRMED: expired multipart stays FINALIZING after request and scheduler retry; manual delete/re-upload required."
    )


@pytest.mark.parametrize("size", [500_000_000, 5_000_000_000])
def test_default_rejects_resume_target_sizes(client, auth, size):
    response = client.post(
        "/api/uploads",
        headers=auth,
        json={"request_id": str(uuid4()), "name": "target.bin", "size": size, "sha256": "0" * 64},
    )
    assert response.status_code == 413
    print(f"CONFIRMED: default API rejects declared {size} byte file before any transfer.")
