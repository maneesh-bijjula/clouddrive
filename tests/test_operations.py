from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from clouddrive import operations
from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.service import query
from clouddrive.storage import s3
from clouddrive.worker import process_file
from helpers import begin, uploaded


def test_restore_state_policy_fences_old_workers_and_expires_multipart():
    for status in (
        "UPLOADING",
        "FINALIZING",
        "PROCESSING",
        "READY",
        "FAILED",
        "DELETING",
        "DELETED",
        "EXPIRED",
    ):
        source = {
            "id": "file",
            "status": status,
            "upload_id": "old",
            "claim": "old",
            "lease_until": "old",
            "result": {},
            "sha256": "hash",
        }
        result = operations.restored_file(
            source, {"file": {}} if status in {"PROCESSING", "READY", "FAILED"} else {}
        )
        assert result["claim"] is result["lease_until"] is result["upload_id"] is None
        assert result["status"] == {
            "UPLOADING": "EXPIRED",
            "FINALIZING": "EXPIRED",
            "DELETING": "DELETED",
        }.get(status, status)
        assert source["upload_id"] == "old"
    assert (
        operations.restored_file({"id": "file", "status": "FINALIZING"}, {"file": {}})["status"]
        == "PROCESSING"
    )


def test_archive_paths_and_incomplete_archive_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        operations.validate_archive(tmp_path)
    (tmp_path / "outside").symlink_to(Path(__file__))
    with pytest.raises(ValueError):
        operations.checked_path(tmp_path, "outside")


def test_cleanup_rejects_unsafe_grace_and_limits():
    for args in ({"older_than_seconds": 3600}, {"limit": 0}, {"limit": 1001}):
        with pytest.raises(ValueError):
            operations.cleanup_multipart(**args)


def empty_fixture_storage():
    bucket = settings().s3_bucket
    assert bucket.endswith("-test")
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            s3().delete_object(Bucket=bucket, Key=obj["Key"])
    for page in s3().get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
        for upload in page.get("Uploads", []):
            s3().abort_multipart_upload(
                Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )


@pytest.mark.integration
def test_backup_restore_full_inventory_and_integrity(client, auth, tmp_path):
    ready, ready_request = uploaded(client, auth, b"committed ready bytes")
    process_file(ready["id"])
    pending, _ = uploaded(client, auth, b"committed pending bytes")
    unfinished, _ = begin(client, auth, b"not finished")
    with engine().begin() as conn:
        query(
            conn,
            "UPDATE files SET claim=:claim,lease_until=now()+interval '90 seconds' WHERE id=:id",
            id=pending["id"],
            claim=uuid4(),
        )
    destination = tmp_path / "snapshot"
    manifest = operations.backup(destination)
    assert manifest["object_count"] == 2
    with pytest.raises(ValueError, match="Confirm exact"):
        operations.restore(destination, "wrong", settings().s3_bucket)
    with pytest.raises(ValueError, match="empty destination bucket"):
        operations.restore(destination, engine().url.database, settings().s3_bucket)
    empty_fixture_storage()
    with pytest.raises(ValueError, match="empty destination database"):
        operations.restore(destination, engine().url.database, settings().s3_bucket)
    with engine().begin() as conn:
        query(conn, "TRUNCATE workspaces,files,upload_parts,processing_outbox CASCADE")
    result = operations.restore(destination, engine().url.database, settings().s3_bucket)
    assert result["objects_verified"] == 2
    assert result["files_by_status"] == {"READY": 1, "PROCESSING": 1, "EXPIRED": 1}
    assert client.get("/api/workspace", headers=auth).status_code == 200
    assert client.get(f"/api/files/{ready['id']}/download", headers=auth).status_code == 200
    with engine().connect() as conn:
        assert (
            query(
                conn, "SELECT count(*) FROM files WHERE claim IS NOT NULL OR upload_id IS NOT NULL"
            ).scalar()
            == 0
        )
        assert query(conn, "SELECT count(*) FROM processing_outbox").scalar() == 1
        assert query(conn, "SELECT count(*) FROM upload_parts").scalar() == 0
    replay = client.post("/api/uploads", headers=auth, json=ready_request)
    assert replay.status_code == 201 and replay.json()["status"] == "READY"
    assert not s3().list_multipart_uploads(Bucket=settings().s3_bucket).get("Uploads")
    assert process_file(pending["id"]) == "ready"
    assert (
        client.get(f"/api/uploads/{unfinished['id']}", headers=auth).json()["status"] == "EXPIRED"
    )


@pytest.mark.integration
def test_corrupt_or_incomplete_archives_fail_before_writes(client, auth, tmp_path):
    ready, _ = uploaded(client, auth)
    process_file(ready["id"])
    destination = tmp_path / "snapshot"
    operations.backup(destination)
    operations.validate_archive(destination)
    obj = destination / "objects" / ready["id"]
    obj.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Object checksum"):
        operations.validate_archive(destination)
    (destination / "manifest.json").unlink()
    with pytest.raises(ValueError, match="missing archive member"):
        operations.validate_archive(destination)


@pytest.mark.integration
def test_backup_rejects_corrupt_ready_object_and_has_no_completion_marker(client, auth, tmp_path):
    ready, _ = uploaded(client, auth)
    process_file(ready["id"])
    with engine().connect() as conn:
        key = query(conn, "SELECT object_key FROM files WHERE id=:id", id=ready["id"]).scalar()
    s3().put_object(
        Bucket=settings().s3_bucket, Key=key, Body=b"corrupted", Metadata={"file-id": ready["id"]}
    )
    with pytest.raises(ValueError, match="READY integrity"):
        operations.backup(tmp_path / "incomplete")
    assert not (tmp_path / "incomplete/manifest.json").exists()


@pytest.mark.integration
def test_cleanup_aborts_only_old_orphans_and_protects_all_live_references(
    client, auth, monkeypatch
):
    active, _ = begin(client, auth, b"keep my upload")
    bucket = settings().s3_bucket
    orphan = f"{uuid4()}/{uuid4()}"
    recent = f"{uuid4()}/{uuid4()}"
    for key in (orphan, recent, "foreign-unmanaged-key"):
        s3().create_multipart_upload(Bucket=bucket, Key=key)
    actual = s3().list_multipart_uploads(Bucket=bucket)["Uploads"]
    for upload in actual:
        upload["Initiated"] = datetime.now(timezone.utc) - timedelta(
            days=2 if upload["Key"] != recent else 0
        )

    class Pages:
        def paginate(self, **kwargs):
            return [{"Uploads": actual}]

    original = s3().get_paginator
    monkeypatch.setattr(
        s3(),
        "get_paginator",
        lambda name: Pages() if name == "list_multipart_uploads" else original(name),
    )
    result = operations.cleanup_multipart()
    assert [x["action"] for x in result["outcomes"]].count("would_abort") == 1
    assert len(s3().list_multipart_uploads(Bucket=bucket)["Uploads"]) == 4
    applied = operations.cleanup_multipart(apply=True)
    assert [x["action"] for x in applied["outcomes"]].count("aborted") == 1
    remaining = s3().list_multipart_uploads(Bucket=bucket)["Uploads"]
    assert orphan not in {x["Key"] for x in remaining}
    assert {recent, "foreign-unmanaged-key"} <= {x["Key"] for x in remaining}
    assert client.get(f"/api/uploads/{active['id']}", headers=auth).status_code == 200
