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


def test_archive_flush_failure_never_publishes_completion(tmp_path, monkeypatch):
    (tmp_path / "objects").mkdir()
    (tmp_path / "files.jsonl").write_text("source bytes\n")
    monkeypatch.setattr(
        operations.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk failure"))
    )
    with pytest.raises(OSError):
        operations.publish_archive(tmp_path, {"format": 1})
    assert not (tmp_path / "manifest.json").exists()


def test_archive_completion_is_durable_and_does_not_replace_existing_marker(tmp_path):
    (tmp_path / "objects").mkdir()
    (tmp_path / "files.jsonl").write_text("source bytes\n")
    operations.publish_archive(tmp_path, {"format": 1})
    original = (tmp_path / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        operations.publish_archive(tmp_path, {"format": 2})
    assert (tmp_path / "manifest.json").read_bytes() == original


@pytest.mark.parametrize("entry", ["Versions", "DeleteMarkers"])
def test_restore_refuses_hidden_versions_before_writing(entry):
    from unittest.mock import Mock

    client = Mock()
    client.list_objects_v2.return_value = {"Contents": []}
    client.list_multipart_uploads.return_value = {}
    client.list_object_versions.return_value = {entry: [{"Key": "retained"}]}
    with pytest.raises(ValueError, match="including versions"):
        operations.require_empty_bucket(client, "target")
    client.upload_fileobj.assert_not_called()


@pytest.mark.integration
def test_real_versioned_destination_with_only_a_delete_marker_is_not_empty(client):
    bucket = f"clouddrive-versioned-{uuid4().hex}-test"
    storage = s3()
    storage.create_bucket(Bucket=bucket)
    try:
        storage.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        operations.require_empty_bucket(storage, bucket)
        storage.put_object(Bucket=bucket, Key="retained-original", Body=b"preserve")
        storage.delete_object(Bucket=bucket, Key="retained-original")
        assert not storage.list_objects_v2(Bucket=bucket).get("Contents")
        with pytest.raises(ValueError, match="including versions"):
            operations.require_empty_bucket(storage, bucket)
    finally:
        for page in storage.get_paginator("list_object_versions").paginate(Bucket=bucket):
            for obj in page.get("Versions", []) + page.get("DeleteMarkers", []):
                storage.delete_object(Bucket=bucket, Key=obj["Key"], VersionId=obj["VersionId"])
        storage.delete_bucket(Bucket=bucket)


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


@pytest.mark.integration
def test_backup_fences_a_concurrent_delete_until_committed_bytes_are_copied(
    client, auth, tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from clouddrive.service import delete_file

    file, _ = uploaded(client, auth, b"retain the recovery-point bytes")
    process_file(file["id"])
    workspace = client.get("/api/workspace", headers=auth).json()["id"]
    entered, release, deleting = Event(), Event(), Event()
    original = s3().get_object

    def held_copy(**kwargs):
        response = original(**kwargs)
        entered.set()
        assert release.wait(5)
        return response

    def remove():
        deleting.set()
        delete_file(workspace, file["id"])

    monkeypatch.setattr(s3(), "get_object", held_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        copying = pool.submit(operations.backup, tmp_path / "snapshot")
        try:
            assert entered.wait(5)
            deletion = pool.submit(remove)
            assert deleting.wait(5)
            # The backup holds SHARE locks; deletion cannot commit its intent.
            from concurrent.futures import TimeoutError

            with pytest.raises(TimeoutError):
                deletion.result(timeout=0.1)
        finally:
            release.set()
        assert copying.result(timeout=10)["object_count"] == 1
        deletion.result(timeout=10)
    operations.validate_archive(tmp_path / "snapshot")
    assert client.get(f"/api/uploads/{file['id']}", headers=auth).status_code == 404


@pytest.mark.integration
def test_restore_policy_for_lost_finalization_reply_keeps_committed_object(client, auth, tmp_path):
    committed, _ = uploaded(client, auth, b"completed storage with lost commit reply")
    with engine().begin() as conn:
        query(conn, "UPDATE files SET status='FINALIZING' WHERE id=:id", id=committed["id"])
    destination = tmp_path / "snapshot"
    operations.backup(destination)
    _, objects = operations.validate_archive(destination)
    assert committed["id"] in objects
    source = next(operations.records(destination / "files.jsonl"))
    restored = operations.restored_file(source, objects)
    assert restored["status"] == "PROCESSING"
    assert restored["result"] is restored["claim"] is restored["upload_id"] is None


@pytest.mark.integration
def test_restore_rechecks_bucket_after_waiting_for_metadata_lock(
    client, auth, tmp_path, monkeypatch
):
    file, _ = uploaded(client, auth, b"restore archive")
    process_file(file["id"])
    destination = tmp_path / "snapshot"
    operations.backup(destination)
    empty_fixture_storage()
    with engine().begin() as conn:
        query(conn, "TRUNCATE workspaces,files,upload_parts,processing_outbox CASCADE")
    check = operations.require_empty_bucket
    checks = []

    def competing_copy(storage, bucket):
        check(storage, bucket)
        checks.append(True)
        if len(checks) == 1:
            storage.put_object(Bucket=bucket, Key="partial-other-restore", Body=b"preserve")

    monkeypatch.setattr(operations, "require_empty_bucket", competing_copy)
    with pytest.raises(ValueError, match="empty destination bucket"):
        operations.restore(destination, engine().url.database, settings().s3_bucket)
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM files").scalar() == 0
    with s3().get_object(Bucket=settings().s3_bucket, Key="partial-other-restore")["Body"] as body:
        assert body.read() == b"preserve"


@pytest.mark.integration
def test_cleanup_cursor_advances_past_protected_uploads(client, auth, monkeypatch):
    active, _ = begin(client, auth, b"protected")
    bucket = settings().s3_bucket
    with engine().connect() as conn:
        key = query(conn, "SELECT object_key FROM files WHERE id=:id", id=active["id"]).scalar()
    orphan_key = f"{uuid4()}/{uuid4()}"
    orphan = s3().create_multipart_upload(Bucket=bucket, Key=orphan_key)
    active_upload = next(
        x for x in s3().list_multipart_uploads(Bucket=bucket)["Uploads"] if x["Key"] == key
    )
    rows = [active_upload, {"Key": orphan_key, "UploadId": orphan["UploadId"]}]
    for row in rows:
        row["Initiated"] = datetime.now(timezone.utc) - timedelta(days=2)

    class Pages:
        def paginate(self, **params):
            if params.get("KeyMarker"):
                assert (
                    params["KeyMarker"] == key
                    and params["UploadIdMarker"] == active_upload["UploadId"]
                )
                return [{"Uploads": rows[1:]}]
            return [{"Uploads": rows}]

    monkeypatch.setattr(s3(), "get_paginator", lambda name: Pages())
    first = operations.cleanup_multipart(apply=True, limit=1)
    assert first["scanned"] == 1 and first["outcomes"][0]["action"] == "protected"
    assert first["next_cursor"] and first["truncated"]
    second = operations.cleanup_multipart(apply=True, limit=1, cursor=first["next_cursor"])
    assert second["outcomes"][0]["action"] == "aborted" and second["next_cursor"] is None
    remaining = s3().list_multipart_uploads(Bucket=bucket).get("Uploads", [])
    assert {row["Key"] for row in remaining} == {key}
    with pytest.raises(ValueError, match="Invalid cleanup cursor"):
        operations.cleanup_multipart(cursor=first["next_cursor"], older_than_seconds=86401)


@pytest.mark.integration
@pytest.mark.parametrize("same_key", [False, True])
def test_cleanup_provider_cursor_progresses_after_aborting_its_marker(
    client, monkeypatch, same_key
):
    bucket = settings().s3_bucket
    key = f"{uuid4()}/{uuid4()}"
    for _ in range(3):
        s3().create_multipart_upload(Bucket=bucket, Key=key if same_key else f"{uuid4()}/{uuid4()}")
    original = s3().get_paginator("list_multipart_uploads")

    class Pages:
        def paginate(self, **params):
            for page in original.paginate(**params):
                for row in page.get("Uploads", []):
                    row["Initiated"] = datetime.now(timezone.utc) - timedelta(days=2)
                yield page

    monkeypatch.setattr(s3(), "get_paginator", lambda name: Pages())
    cursor, aborted = None, 0
    for _ in range(4):
        result = operations.cleanup_multipart(apply=True, limit=1, cursor=cursor)
        aborted += sum(row["action"] == "aborted" for row in result["outcomes"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert cursor is None and aborted == 3
    assert not s3().list_multipart_uploads(Bucket=bucket).get("Uploads")
