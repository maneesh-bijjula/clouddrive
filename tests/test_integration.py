import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import httpx
import pytest

from clouddrive import service
from clouddrive.config import settings
from clouddrive.db import engine, migrate
from clouddrive.service import query
from clouddrive.storage import s3, head
from clouddrive.worker import dispatch_pending, process_file

from helpers import begin, send_part, uploaded

pytestmark = pytest.mark.integration


def test_end_to_end_multipart_verified_download(client, auth):
    content = b"CloudDrive multipart data\n" * 360000
    upload, data = uploaded(client, auth, content, name="résumé draft.txt")
    assert upload["part_count"] == 2
    assert upload["status"] == "PROCESSING"
    id = UUID(upload["id"])
    assert process_file(id) == "ready"
    assert process_file(id) == "skipped"
    detail = client.get(f"/api/uploads/{id}", headers=auth).json()
    assert detail["sha256"] == data["sha256"]
    assert detail["result"]["bytes_verified"] == len(content)
    assert len(detail["result"]["preview"]) == 2000
    assert detail["result"]["preview_truncated"]
    signed = client.get(f"/api/files/{id}/download", headers=auth)
    response = httpx.get(signed.json()["url"])
    assert response.content == content
    assert (
        "filename*=UTF-8''r%C3%A9sum%C3%A9%20draft.txt" in response.headers["Content-Disposition"]
    )
    assert httpx.get(signed.json()["url"].split("?")[0]).status_code == 403


def test_idempotent_initiation_and_mismatched_request(client, auth):
    upload, data = begin(client, auth)
    assert client.post("/api/uploads", headers=auth, json=data).json()["id"] == upload["id"]
    assert (
        client.post("/api/uploads", headers=auth, json={**data, "name": "other.txt"}).status_code
        == 409
    )


def test_simultaneous_initiation_is_single_session(client, auth):
    data = {
        "request_id": str(uuid4()),
        "name": "same.txt",
        "size": 1,
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(
            executor.map(lambda _: client.post("/api/uploads", headers=auth, json=data), range(4))
        )
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1


def test_lost_acknowledgement_resume(client, auth):
    upload, _ = begin(client, auth, b"resume me")
    checksum, etag = send_part(client, auth, upload, 1, b"resume me", acknowledge=False)
    signed = client.post(
        f"/api/uploads/{upload['id']}/parts/1/sign", headers=auth, json={"checksum": checksum}
    ).json()
    assert signed == {"uploaded": True, "etag": etag}
    detail = client.get(f"/api/uploads/{upload['id']}", headers=auth).json()
    assert detail["parts"][0]["etag"] == etag
    assert client.post(f"/api/uploads/{upload['id']}/complete", headers=auth).status_code == 202


def test_missing_part_and_changed_part_rejected(client, auth):
    upload, _ = begin(client, auth)
    id = upload["id"]
    assert client.post(f"/api/uploads/{id}/complete", headers=auth).status_code == 409
    send_part(client, auth, upload, 1, b"Hello, CloudDrive!\n")
    checksum = base64.b64encode(hashlib.sha256(b"wrong").digest()).decode()
    assert (
        client.post(
            f"/api/uploads/{id}/parts/1/sign", headers=auth, json={"checksum": checksum}
        ).status_code
        == 409
    )


def test_storage_rejects_corrupt_part(client, auth):
    upload, _ = begin(client, auth, b"hello")
    checksum = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    signed = client.post(
        f"/api/uploads/{upload['id']}/parts/1/sign", headers=auth, json={"checksum": checksum}
    ).json()
    response = httpx.put(signed["url"], headers=signed["headers"], content=b"wrong")
    assert response.status_code == 400
    assert "Checksum" in response.text or "Digest" in response.text


def test_forged_receipt_and_out_of_range_rejected(client, auth):
    upload, _ = begin(client, auth, b"hello")
    path = f"/api/uploads/{upload['id']}/parts"
    checksum = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    assert (
        client.post(path + "/2/sign", headers=auth, json={"checksum": checksum}).status_code == 422
    )
    client.post(path + "/1/sign", headers=auth, json={"checksum": checksum})
    assert (
        client.put(
            path + "/1", headers=auth, json={"checksum": checksum, "etag": '"forged"'}
        ).status_code
        == 409
    )


def test_workspace_isolation(client, auth):
    upload, _ = begin(client, auth)
    other = {"Authorization": "Bearer " + client.post("/api/workspaces").json()["access_key"]}
    id = upload["id"]
    assert client.get("/api/files", headers=other).json()["files"] == []
    for method, path in [
        ("GET", f"/api/uploads/{id}"),
        ("DELETE", f"/api/files/{id}"),
        ("POST", f"/api/uploads/{id}/complete"),
        ("GET", f"/api/files/{id}/download"),
        ("POST", f"/api/files/{id}/retry"),
    ]:
        assert client.request(method, path, headers=other).status_code == 404
    assert client.get("/api/files").status_code == 401
    assert client.get("/api/files", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_download_blocked_until_whole_checksum_passes(client, auth):
    upload, _ = uploaded(client, auth, sha256="0" * 64)
    id = upload["id"]
    assert client.get(f"/api/files/{id}/download", headers=auth).status_code == 409
    assert process_file(UUID(id)) == "failed"
    detail = client.get(f"/api/uploads/{id}", headers=auth).json()
    assert detail["status"] == "FAILED" and detail["result"] is None
    assert client.get(f"/api/files/{id}/download", headers=auth).status_code == 409
    assert client.post(f"/api/files/{id}/retry", headers=auth).status_code == 202
    assert process_file(UUID(id)) == "failed"


def test_idempotent_completion(client, auth):
    upload, _ = uploaded(client, auth)
    path = f"/api/uploads/{upload['id']}/complete"
    assert client.post(path, headers=auth).json()["status"] == "PROCESSING"
    process_file(UUID(upload["id"]))
    assert client.post(path, headers=auth).json()["status"] == "READY"
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM processing_outbox").scalar_one() == 1


def test_finalization_recovers_after_s3_success_db_failure(client, auth, monkeypatch):
    upload, _ = begin(client, auth)
    send_part(client, auth, upload, 1, b"Hello, CloudDrive!\n")
    workspace_id = service.workspace(auth["Authorization"][7:])["id"]
    real_query = service.query

    def fail_status(conn, sql, **params):
        if "UPDATE files SET status='PROCESSING'" in sql:
            raise RuntimeError("simulated DB failure after S3 completed")
        return real_query(conn, sql, **params)

    monkeypatch.setattr(service, "query", fail_status)
    with pytest.raises(RuntimeError):
        service.finalize(workspace_id, UUID(upload["id"]))
    monkeypatch.setattr(service, "query", real_query)
    assert client.get(f"/api/uploads/{upload['id']}", headers=auth).json()["status"] == "FINALIZING"
    assert (
        client.post(f"/api/uploads/{upload['id']}/complete", headers=auth).json()["status"]
        == "PROCESSING"
    )
    assert process_file(UUID(upload["id"])) == "ready"


def test_outbox_survives_broker_failure_and_recovers_lost_message(client, auth):
    upload, _ = uploaded(client, auth)

    def unavailable(id):
        raise ConnectionError("broker unavailable")

    with pytest.raises(ConnectionError):
        dispatch_pending(unavailable)
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM processing_outbox").scalar_one() == 1
    delivered = []
    assert dispatch_pending(delivered.append) == 1
    assert delivered == [UUID(upload["id"])]
    with engine().begin() as conn:
        query(
            conn,
            "UPDATE files SET updated_at=now()-interval '1 minute' WHERE id=:id",
            id=UUID(upload["id"]),
        )
    assert dispatch_pending(delivered.append) == 1
    assert process_file(UUID(upload["id"])) == "ready"
    assert process_file(UUID(upload["id"])) == "skipped"


def test_active_claim_excludes_duplicate_and_expired_claim_recovers(client, auth):
    upload, _ = uploaded(client, auth)
    id = UUID(upload["id"])
    with engine().begin() as conn:
        query(
            conn,
            "UPDATE files SET claim=:claim,lease_until=now()+interval '1 minute' WHERE id=:id",
            id=id,
            claim=uuid4(),
        )
    assert process_file(id) == "skipped"
    with engine().begin() as conn:
        query(conn, "UPDATE files SET lease_until=now()-interval '1 second' WHERE id=:id", id=id)
    assert process_file(id) == "ready"


@pytest.mark.parametrize("completed", [False, True])
def test_delete_cleans_objects_and_duplicate_worker_cannot_restore(client, auth, completed):
    upload, _ = uploaded(client, auth) if completed else begin(client, auth)
    id = upload["id"]
    with engine().connect() as conn:
        key = query(conn, "SELECT object_key FROM files WHERE id=:id", id=UUID(id)).scalar_one()
    assert client.delete(f"/api/files/{id}", headers=auth).status_code == 204
    assert client.delete(f"/api/files/{id}", headers=auth).status_code == 204
    assert process_file(UUID(id)) == "skipped"
    assert head(key) is None
    assert client.get(f"/api/uploads/{id}", headers=auth).status_code == 404
    assert client.get("/api/files", headers=auth).json()["files"] == []


def test_binary_preview_and_size_validation(client, auth):
    upload, _ = uploaded(client, auth, b"\x00\xff\x00binary")
    process_file(UUID(upload["id"]))
    detail = client.get(f"/api/uploads/{upload['id']}", headers=auth).json()
    assert detail["result"]["preview"] is None
    response = client.post(
        "/api/uploads",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "name": "huge",
            "size": settings().max_file_bytes + 1,
            "sha256": "0" * 64,
        },
    )
    assert response.status_code == 413


def test_token_hash_and_headers(client, auth):
    with engine().connect() as conn:
        saved = query(conn, "SELECT token_hash FROM workspaces").scalar_one()
    assert saved != auth["Authorization"][7:]
    assert saved == hashlib.sha256(auth["Authorization"][7:].encode()).hexdigest()
    response = client.get("/api/workspace", headers=auth)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_migrations_repeat_safely(client):
    migrate()
    migrate()
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM schema_migrations").scalar_one() == 2


@pytest.mark.parametrize("action", ["delete", "replace-claim"])
def test_worker_cannot_commit_after_its_claim_is_lost(client, auth, monkeypatch, action):
    from clouddrive import worker

    upload, _ = uploaded(client, auth)
    id = UUID(upload["id"])
    workspace_id = service.workspace(auth["Authorization"][7:])["id"]
    client_s3 = s3()
    original = client_s3.get_object

    class Body:
        def __init__(self, body):
            self.body = body

        def iter_chunks(self, **kwargs):
            for chunk in self.body.iter_chunks(**kwargs):
                if action == "delete":
                    service.delete_file(workspace_id, id)
                else:
                    with engine().begin() as conn:
                        query(
                            conn,
                            "UPDATE files SET claim=:claim,lease_until=now()+interval '90 seconds' WHERE id=:id",
                            id=id,
                            claim=uuid4(),
                        )
                yield chunk

        def close(self):
            self.body.close()

    class Client:
        def get_object(self, **kwargs):
            response = original(**kwargs)
            response["Body"] = Body(response["Body"])
            return response

    monkeypatch.setattr(worker, "s3", lambda: Client())
    assert process_file(id) == "skipped"
    with engine().connect() as conn:
        assert query(conn, "SELECT status FROM files WHERE id=:id", id=id).scalar_one() == (
            "DELETED" if action == "delete" else "PROCESSING"
        )


def test_workspace_totals_include_files_outside_browsing_limit(client, auth):
    workspace_id = service.workspace(auth["Authorization"][7:])["id"]
    with engine().begin() as conn:
        query(
            conn,
            """INSERT INTO files(id,workspace_id,request_id,name,size,media_type,expected_sha256,part_size,object_key,status)
            SELECT gen_random_uuid(),:workspace,gen_random_uuid(),'metadata fixture '||n,100,'text/plain',repeat('0',64),8388608,gen_random_uuid()::text,
            CASE WHEN n <= 2 THEN 'READY' ELSE 'UPLOADING' END FROM generate_series(1,205) n""",
            workspace=workspace_id,
        )
    response = client.get("/api/files", headers=auth).json()
    assert len(response["files"]) == 200
    assert response["stats"] == {"total": 205, "ready": 2, "bytes": 200, "pending": 203}


def test_target_size_metadata_and_rejection_before_storage_allocation(client, auth, monkeypatch):
    upload, data = begin(client, auth, size=5_000_000_000)
    assert upload["part_count"] == 597
    with engine().connect() as conn:
        assert (
            query(conn, "SELECT size FROM files WHERE id=:id", id=UUID(upload["id"])).scalar_one()
            == 5_000_000_000
        )

    def forbidden(**kwargs):
        pytest.fail("over-limit request reached object storage")

    monkeypatch.setattr(s3(), "create_multipart_upload", forbidden)
    result = client.post(
        "/api/uploads",
        headers=auth,
        json={**data, "request_id": str(uuid4()), "size": 5_000_000_001},
    )
    assert result.status_code == 413
    with engine().connect() as conn:
        assert query(conn, "SELECT count(*) FROM files").scalar_one() == 1
    assert client.get("/api/workspace", headers=auth).json()["max_file_bytes"] == 5_000_000_000


def test_sparse_parts_can_finish_in_reverse_order(client, auth):
    content = b"a" * (8 * 1024**2) + b"b" * (8 * 1024**2) + b"last sparse bytes"
    upload, _ = begin(client, auth, content)
    for number in [3, 2, 1]:
        send_part(
            client,
            auth,
            upload,
            number,
            content[(number - 1) * upload["part_size"] : number * upload["part_size"]],
        )
    response = client.post(f"/api/uploads/{upload['id']}/complete", headers=auth)
    assert response.status_code == 202
    assert process_file(UUID(upload["id"])) == "ready"
    url = client.get(f"/api/files/{upload['id']}/download", headers=auth).json()["url"]
    assert httpx.get(url).content == content
