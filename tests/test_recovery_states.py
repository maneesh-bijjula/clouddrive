"""Sprint 1 regressions, deterministic races, isolation, and populated migration."""

import base64
import hashlib
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from clouddrive import db, service, worker
from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.service import query
from clouddrive.storage import head, s3
from helpers import begin, send_part, uploaded

pytestmark = pytest.mark.integration


def stored(id):
    with engine().connect() as conn:
        return query(conn, "SELECT * FROM files WHERE id=:id", id=UUID(str(id))).mappings().one()


def age(id):
    with engine().begin() as conn:
        query(
            conn,
            "UPDATE files SET updated_at=now()-interval '1 minute' WHERE id=:id",
            id=UUID(str(id)),
        )


def abort(upload):
    row = stored(upload["id"])
    s3().abort_multipart_upload(
        Bucket=settings().s3_bucket, Key=row["object_key"], UploadId=row["upload_id"]
    )


@pytest.mark.parametrize("boundary", ["intent", "abort", "object-delete", "metadata-commit"])
def test_durable_delete_recovers_every_storage_boundary(client, auth, monkeypatch, boundary):
    upload, data = uploaded(client, auth)
    id = upload["id"]
    if boundary != "abort":
        assert worker.process_file(UUID(id)) == "ready"
    row = stored(id)
    original_query = service.query
    storage = s3()

    def fail_query(conn, sql, **params):
        if (boundary == "intent" and "UPDATE files SET status='DELETING'" in sql) or (
            boundary == "metadata-commit" and "UPDATE files SET status='DELETED'" in sql
        ):
            raise SQLAlchemyError("injected deletion database failure")
        return original_query(conn, sql, **params)

    def unavailable(**kwargs):
        raise EndpointConnectionError(endpoint_url="http://isolated-test-storage")

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "query", fail_query)
        if boundary in {"abort", "object-delete"}:
            scoped.setattr(
                storage,
                "abort_multipart_upload" if boundary == "abort" else "delete_object",
                unavailable,
            )
        response = client.delete(f"/api/files/{id}", headers=auth)
        assert response.status_code == 503
    if boundary == "intent":
        assert stored(id)["status"] == "READY"
        assert head(row["object_key"]) is not None
        signed = client.get(f"/api/files/{id}/download", headers=auth).json()["url"]
        assert hashlib.sha256(httpx.get(signed).content).hexdigest() == data["sha256"]
        assert client.delete(f"/api/files/{id}", headers=auth).status_code == 204
    else:
        assert stored(id)["status"] == "DELETING"
        for path in (f"/api/uploads/{id}", f"/api/files/{id}/download"):
            assert client.get(path, headers=auth).status_code == 404
        listing = client.get("/api/files", headers=auth).json()
        assert listing["files"] == [] and listing["stats"]["total"] == 0
        assert worker.process_file(UUID(id)) == "skipped"
        age(id)
        worker.reconcile_files()
    assert stored(id)["status"] == "DELETED"
    assert head(row["object_key"]) is None
    assert client.delete(f"/api/files/{id}", headers=auth).status_code == 204
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM processing_outbox").scalar_one() == 0


def test_cleanup_continues_after_one_file_fails_and_before_publish(client, auth, monkeypatch):
    bad, _ = uploaded(client, auth)
    good, _ = uploaded(client, auth)
    pending, _ = uploaded(client, auth)
    for upload in (bad, good):
        with monkeypatch.context() as scoped:
            scoped.setattr(
                service,
                "finish_delete",
                lambda *args: (_ for _ in ()).throw(OSError("interrupted")),
            )
            with pytest.raises(OSError):
                service.delete_file(stored(upload["id"])["workspace_id"], UUID(upload["id"]))
        age(upload["id"])
    real_delete = s3().delete_object

    def delete(**kwargs):
        if kwargs["Key"] == stored(bad["id"])["object_key"]:
            raise EndpointConnectionError(endpoint_url="http://isolated-test-storage")
        return real_delete(**kwargs)

    def publish_failure(*args):
        raise ConnectionError("broker publication unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(s3(), "delete_object", delete)
        scoped.setattr(worker, "dispatch_pending", publish_failure)
        with pytest.raises(ConnectionError):
            worker.dispatch.run()
    assert stored(bad["id"])["status"] == "DELETING"
    assert stored(good["id"])["status"] == "DELETED"
    assert stored(pending["id"])["status"] == "PROCESSING"
    worker.reconcile_files()
    assert stored(bad["id"])["status"] == "DELETED"


def test_process_killed_after_object_delete_recovers_durable_intent(client, auth):
    upload, _ = uploaded(client, auth)
    assert worker.process_file(UUID(upload["id"])) == "ready"
    row = stored(upload["id"])
    code = """
import os, signal, sys
from uuid import UUID
from clouddrive.service import delete_file
from clouddrive.storage import s3
original = s3().delete_object
def kill_after_delete(**kwargs):
    original(**kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
s3().delete_object = kill_after_delete
delete_file(UUID(sys.argv[1]), UUID(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(row["workspace_id"]), upload["id"]],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr.decode()
    assert head(row["object_key"]) is None
    assert stored(upload["id"])["status"] == "DELETING"
    assert client.get(f"/api/files/{upload['id']}/download", headers=auth).status_code == 404
    age(upload["id"])
    worker.reconcile_files()
    assert stored(upload["id"])["status"] == "DELETED"


@pytest.mark.parametrize(
    "operation", ["sign-new", "sign-existing", "acknowledge", "complete", "scheduler"]
)
def test_expired_multipart_is_terminal_and_can_restart(client, auth, operation):
    content = b"expiry content"
    upload, data = begin(client, auth, content)
    checksum = base64.b64encode(hashlib.sha256(content).digest()).decode()
    etag = '"unused"'
    if operation != "sign-new":
        checksum, etag = send_part(client, auth, upload, 1, content)
    if operation == "scheduler":
        with engine().begin() as conn:
            query(conn, "UPDATE files SET status='FINALIZING' WHERE id=:id", id=UUID(upload["id"]))
        age(upload["id"])
    abort(upload)
    path = f"/api/uploads/{upload['id']}"
    if operation == "scheduler":
        worker.reconcile_files()
    else:
        method, endpoint, body = {
            "sign-new": ("POST", path + "/parts/1/sign", {"checksum": checksum}),
            "sign-existing": ("POST", path + "/parts/1/sign", {"checksum": checksum}),
            "acknowledge": ("PUT", path + "/parts/1", {"checksum": checksum, "etag": etag}),
            "complete": ("POST", path + "/complete", None),
        }[operation]
        response = client.request(method, endpoint, headers=auth, json=body)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "upload_expired"
    assert stored(upload["id"])["status"] == "EXPIRED"
    # Repeated completion and scheduler runs do not resurrect or retry an expiry.
    assert client.post(path + "/complete", headers=auth).json()["status"] == "EXPIRED"
    assert client.post(f"/api/files/{upload['id']}/retry", headers=auth).status_code == 409
    assert client.get(f"/api/files/{upload['id']}/download", headers=auth).status_code == 409
    assert worker.process_file(UUID(upload["id"])) == "skipped"
    worker.reconcile_files()
    assert client.delete(f"/api/files/{upload['id']}", headers=auth).status_code == 204
    replacement, _ = uploaded(client, auth, content)
    assert replacement["id"] != upload["id"]
    assert worker.process_file(UUID(replacement["id"])) == "ready"
    signed = client.get(f"/api/files/{replacement['id']}/download", headers=auth).json()["url"]
    assert httpx.get(signed).content == content


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "InternalError"])
def test_transient_or_access_errors_do_not_expire_upload(client, auth, monkeypatch, code):
    upload, _ = begin(client, auth)

    def fail(*args):
        raise ClientError({"Error": {"Code": code}}, "ListParts")

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "remote_part", fail)
        response = client.post(
            f"/api/uploads/{upload['id']}/parts/1/sign",
            headers=auth,
            json={"checksum": base64.b64encode(b"x" * 32).decode()},
        )
    assert response.status_code == 503
    assert stored(upload["id"])["status"] == "UPLOADING"


@pytest.mark.parametrize("first", ["complete", "delete"])
def test_completion_and_delete_serialize_without_resurrection(client, auth, monkeypatch, first):
    upload, _ = begin(client, auth)
    send_part(client, auth, upload, 1, b"Hello, CloudDrive!\n")
    id = upload["id"]
    key = stored(id)["object_key"]
    entered, release = Event(), Event()
    operation = "complete_multipart_upload" if first == "complete" else "delete_object"
    original = getattr(s3(), operation)

    def hold(**kwargs):
        entered.set()
        assert release.wait(10), "test did not release storage boundary"
        return original(**kwargs)

    monkeypatch.setattr(s3(), operation, hold)

    def complete():
        return client.post(f"/api/uploads/{id}/complete", headers=auth)

    def delete():
        return client.delete(f"/api/files/{id}", headers=auth)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leading = pool.submit(complete if first == "complete" else delete)
        try:
            assert entered.wait(10)
            trailing = pool.submit(delete if first == "complete" else complete)
            # Prove an actual PostgreSQL lock wait rather than relying on timing.
            deadline = time.monotonic() + 5
            while True:
                with engine().connect() as conn:
                    blocked = query(
                        conn,
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND cardinality(pg_blocking_pids(pid))>0",
                    ).scalar_one()
                if blocked:
                    break
                assert time.monotonic() < deadline, "concurrent request never reached row lock"
                time.sleep(0.01)
        finally:
            release.set()
        leading_result, trailing_result = leading.result(timeout=10), trailing.result(timeout=10)
    assert leading_result.status_code == (202 if first == "complete" else 204)
    assert trailing_result.status_code == (204 if first == "complete" else 404)
    assert stored(id)["status"] == "DELETED"
    assert worker.process_file(UUID(id)) == "skipped"
    assert head(key) is None
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM processing_outbox").scalar_one() == 0


def test_simultaneous_completion_uses_one_manifest_and_outbox(client, auth, monkeypatch):
    upload, _ = begin(client, auth)
    send_part(client, auth, upload, 1, b"Hello, CloudDrive!\n")
    original = s3().complete_multipart_upload
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["UploadId"])
        return original(**kwargs)

    monkeypatch.setattr(s3(), "complete_multipart_upload", complete)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(f"/api/uploads/{upload['id']}/complete", headers=auth),
                range(4),
            )
        )
    assert all(response.status_code == 202 for response in responses)
    assert len(calls) == 1
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM processing_outbox").scalar_one() == 1
    assert worker.process_file(UUID(upload["id"])) == "ready"


def test_worker_stream_cannot_commit_after_failed_deletion_cleanup(client, auth, monkeypatch):
    upload, _ = uploaded(client, auth)
    id = UUID(upload["id"])
    entered, release = Event(), Event()
    original = s3().get_object

    class Body:
        def __init__(self, body):
            self.body = body

        def iter_chunks(self, **kwargs):
            for chunk in self.body.iter_chunks(**kwargs):
                entered.set()
                assert release.wait(10)
                yield chunk

        def close(self):
            self.body.close()

    def get(**kwargs):
        response = original(**kwargs)
        response["Body"] = Body(response["Body"])
        return response

    monkeypatch.setattr(s3(), "get_object", get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.process_file, id)
        try:
            assert entered.wait(10)
            with monkeypatch.context() as scoped:

                def unavailable(**kwargs):
                    raise EndpointConnectionError(endpoint_url="http://isolated-test-storage")

                scoped.setattr(s3(), "delete_object", unavailable)
                assert client.delete(f"/api/files/{id}", headers=auth).status_code == 503
            assert stored(id)["status"] == "DELETING"
            assert stored(id)["claim"] is None
        finally:
            release.set()
        assert future.result(timeout=10) == "skipped"
    age(id)
    worker.reconcile_files()
    assert stored(id)["status"] == "DELETED"


@pytest.mark.parametrize(
    "state",
    ["UPLOADING", "FINALIZING", "PROCESSING", "READY", "FAILED", "EXPIRED", "DELETING", "DELETED"],
)
def test_all_file_operations_are_workspace_scoped(client, auth, state):
    upload, data = begin(client, auth)
    id = upload["id"]
    with engine().begin() as conn:
        query(conn, "UPDATE files SET status=:status WHERE id=:id", id=UUID(id), status=state)
    before = dict(stored(id))
    other = {"Authorization": "Bearer " + client.post("/api/workspaces").json()["access_key"]}
    checksum = base64.b64encode(b"x" * 32).decode()
    operations = [
        ("GET", f"/api/uploads/{id}", None),
        ("POST", f"/api/uploads/{id}/parts/1/sign", {"checksum": checksum}),
        ("PUT", f"/api/uploads/{id}/parts/1", {"checksum": checksum, "etag": '"forged"'}),
        ("POST", f"/api/uploads/{id}/complete", None),
        ("GET", f"/api/files/{id}/download", None),
        ("POST", f"/api/files/{id}/retry", None),
        ("DELETE", f"/api/files/{id}", None),
    ]
    with ThreadPoolExecutor(max_workers=7) as pool:
        responses = list(
            pool.map(lambda op: client.request(op[0], op[1], headers=other, json=op[2]), operations)
        )
    assert all(response.status_code == 404 for response in responses)
    assert client.get("/api/files", headers=other).json()["files"] == []
    assert dict(stored(id)) == before
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        for method, path, body in operations:
            assert client.request(method, path, headers=headers, json=body).status_code == 401
    # A request ID is unique within its workspace, not globally.
    second = client.post("/api/uploads", headers=other, json=data)
    assert second.status_code == 201 and second.json()["id"] != id


def test_upgrade_preserves_populated_baseline_schema_and_object(client, auth, monkeypatch):
    schema = "migration_" + uuid4().hex
    scoped = create_engine(
        settings().database_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    initial = Path(db.__file__).parent / "migrations/001_initial.sql"
    workspace_id, file_id = uuid4(), uuid4()
    content = b"bytes from before the migration"
    key = "migration/" + str(file_id)
    s3().put_object(Bucket=settings().s3_bucket, Key=key, Body=content)
    with engine().begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA " + schema)
    try:
        with scoped.begin() as conn:
            conn.exec_driver_sql(initial.read_text())
            conn.exec_driver_sql(
                "CREATE TABLE schema_migrations(name text PRIMARY KEY,checksum text NOT NULL)"
            )
            query(
                conn,
                "INSERT INTO schema_migrations VALUES (:name,:checksum)",
                name=initial.name,
                checksum=hashlib.sha256(initial.read_bytes()).hexdigest(),
            )
            query(
                conn,
                "INSERT INTO workspaces(id,token_hash) VALUES (:id,:hash)",
                id=workspace_id,
                hash="preserved-workspace-token-hash",
            )
            for status in ["UPLOADING", "FINALIZING", "PROCESSING", "READY", "FAILED", "DELETED"]:
                query(
                    conn,
                    """INSERT INTO files(id,workspace_id,request_id,name,size,media_type,expected_sha256,part_size,object_key,status,claim,lease_until)
                    VALUES (:id,:workspace,:request,:name,:size,'text/plain',:hash,8388608,:key,:status,:claim,now()+interval '1 minute')""",
                    id=file_id if status == "READY" else uuid4(),
                    workspace=workspace_id,
                    request=uuid4(),
                    name=status,
                    size=len(content),
                    hash=hashlib.sha256(content).hexdigest(),
                    key=key if status == "READY" else str(uuid4()),
                    status=status,
                    claim=uuid4(),
                )
            query(
                conn,
                "INSERT INTO upload_parts VALUES (:id,1,:checksum,'receipt')",
                id=file_id,
                checksum=base64.b64encode(hashlib.sha256(content).digest()).decode(),
            )
            query(
                conn,
                "INSERT INTO processing_outbox(file_id) SELECT id FROM files WHERE status='PROCESSING'",
            )
            before = {
                table: [
                    dict(r)
                    for r in query(conn, "SELECT * FROM " + table + " ORDER BY 1").mappings()
                ]
                for table in ["workspaces", "files", "upload_parts", "processing_outbox"]
            }
        with monkeypatch.context() as patch:
            patch.setattr(db, "engine", lambda: scoped)
            db.migrate()
            db.migrate()
        with scoped.begin() as conn:
            after = {
                table: [
                    dict(r)
                    for r in query(conn, "SELECT * FROM " + table + " ORDER BY 1").mappings()
                ]
                for table in before
            }
            assert after == before
            assert query(conn, "SELECT count(*) FROM schema_migrations").scalar_one() == 2
            query(conn, "UPDATE files SET status='DELETING' WHERE id=:id", id=file_id)
            query(conn, "UPDATE files SET status='EXPIRED' WHERE id=:id", id=file_id)
        response = s3().get_object(Bucket=settings().s3_bucket, Key=key)
        try:
            assert response["Body"].read() == content
        finally:
            response["Body"].close()
    finally:
        scoped.dispose()
        with engine().begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA " + schema + " CASCADE")
