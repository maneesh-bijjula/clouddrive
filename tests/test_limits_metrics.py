"""Admission races, bounded resource failures and tenant-safe observations."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest


def payload():
    return {
        "request_id": str(uuid4()),
        "name": "capacity.bin",
        "size": 32,
        "sha256": hashlib.sha256(b"x" * 32).hexdigest(),
        "media_type": "application/octet-stream",
    }


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["workspace", "global"])
def test_declared_storage_reservations_are_atomic_across_admissions(
    client, auth, monkeypatch, scope
):
    from clouddrive.config import settings

    monkeypatch.setattr(
        settings(), f"max_storage_bytes_{'per_workspace' if scope == 'workspace' else 'global'}", 40
    )
    other = (
        {"Authorization": "Bearer " + client.post("/api/workspaces").json()["access_key"]}
        if scope == "global"
        else auth
    )
    barrier = threading.Barrier(2)

    def submit(headers):
        barrier.wait(timeout=5)
        return client.post("/api/uploads", headers=headers, json=payload())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [auth, other]))
    assert sorted(r.status_code for r in results) == [201, 429]
    rejected = next(r for r in results if r.status_code == 429)
    assert rejected.json()["detail"]["code"] == "upload_storage_capacity"
    assert rejected.headers["Retry-After"] == "1"


@pytest.mark.integration
@pytest.mark.parametrize("failed", [False, True])
def test_stored_and_deleting_objects_keep_quota_until_physical_cleanup(
    client, auth, monkeypatch, failed
):
    from clouddrive import service
    from clouddrive.config import settings
    from clouddrive.worker import process_file
    from helpers import uploaded
    from botocore.exceptions import EndpointConnectionError

    monkeypatch.setattr(settings(), "max_storage_bytes_per_workspace", 8)
    file, original = uploaded(client, auth, b"12345678", **({"sha256": "0" * 64} if failed else {}))
    assert process_file(file["id"]) == ("failed" if failed else "ready")
    if failed:
        monkeypatch.setattr(settings(), "max_storage_bytes_per_workspace", 7)
        assert client.post(f"/api/files/{file['id']}/retry", headers=auth).status_code == 202
        assert process_file(file["id"]) == "failed"
        monkeypatch.setattr(settings(), "max_storage_bytes_per_workspace", 8)
    replay = client.post("/api/uploads", headers=auth, json=original)
    assert replay.status_code == 201 and replay.json()["id"] == file["id"]
    candidate = payload()
    candidate["size"] = 1
    assert client.post("/api/uploads", headers=auth, json=candidate).status_code == 429
    with monkeypatch.context() as scoped:

        def unavailable(*args):
            raise EndpointConnectionError(endpoint_url="http://isolated-test-storage")

        scoped.setattr(service, "finish_delete", unavailable)
        assert client.delete(f"/api/files/{file['id']}", headers=auth).status_code == 503
    assert client.post("/api/uploads", headers=auth, json=candidate).status_code == 429
    assert client.delete(f"/api/files/{file['id']}", headers=auth).status_code == 204
    assert client.post("/api/uploads", headers=auth, json=candidate).status_code == 201


def test_request_identifiers_are_bounded_and_do_not_accept_log_injection():
    from clouddrive.observability import request_id

    assert request_id("trace-safe:123") == "trace-safe:123"
    for value in ("secret\nforged", "https://host/key?credential=secret", "x" * 81, None):
        assert UUID(request_id(value))


def test_observations_have_bounded_retention_and_isolated_histograms():
    from clouddrive import observability

    owner, stranger = str(uuid4()), str(uuid4())
    for _ in range(observability.MAX_LATENCY_SAMPLES + 10):
        observability.observe(owner, 201, 12.5)
    observability.observe(stranger, 503, 999)
    mine = observability.snapshot(owner)
    assert mine["request_count"] == observability.MAX_LATENCY_SAMPLES + 10
    assert mine["recent_sample_count"] == observability.MAX_LATENCY_SAMPLES
    assert mine["status_classes"]["5"] == 0
    assert mine["recent_p95_ms"] == 12.5
    assert sum(mine["latency_histogram_ms"].values()) == mine["request_count"]
    for _ in range(observability.MAX_WORKSPACES):
        observability.observe(str(uuid4()), 200, 1)
    assert observability.snapshot(owner)["request_count"] == 0
    assert len(observability._workspaces) <= observability.MAX_WORKSPACES


@pytest.mark.integration
def test_concurrent_new_upload_admissions_never_oversubscribe(client, auth, monkeypatch):
    from clouddrive.config import settings

    monkeypatch.setattr(settings(), "max_active_uploads_per_workspace", 2)
    barrier = threading.Barrier(4)

    def create():
        barrier.wait(timeout=5)
        return client.post("/api/uploads", json=payload(), headers=auth)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(), range(4)))
    assert sorted(result.status_code for result in results) == [201, 201, 429, 429]
    denied = next(result for result in results if result.status_code == 429)
    assert denied.headers["Retry-After"] == "1"
    listing = client.get("/api/files", headers=auth).json()
    assert listing["stats"]["pending"] == 2 and len(listing["files"]) == 2


@pytest.mark.integration
def test_global_admissions_serialize_across_different_workspaces(client, monkeypatch):
    from clouddrive.config import settings

    monkeypatch.setattr(settings(), "max_active_uploads_global", 2)
    keys = [client.post("/api/workspaces").json()["access_key"] for _ in range(4)]
    barrier = threading.Barrier(4)

    def create(key):
        barrier.wait(timeout=5)
        return client.post(
            "/api/uploads", json=payload(), headers={"Authorization": "Bearer " + key}
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, keys))
    assert sorted(result.status_code for result in results) == [201, 201, 429, 429]


@pytest.mark.integration
def test_full_capacity_preserves_replay_and_releases_deleted_slot(client, auth, monkeypatch):
    from clouddrive.config import settings

    monkeypatch.setattr(settings(), "max_active_uploads_per_workspace", 1)
    original = payload()
    first = client.post("/api/uploads", json=original, headers=auth)
    assert first.status_code == 201
    assert client.post("/api/uploads", json=payload(), headers=auth).status_code == 429
    replay = client.post("/api/uploads", json=original, headers=auth)
    assert replay.status_code == 201 and replay.json()["id"] == first.json()["id"]
    changed = {**original, "name": "different.bin"}
    assert client.post("/api/uploads", json=changed, headers=auth).status_code == 409
    assert client.delete(f"/api/files/{first.json()['id']}", headers=auth).status_code == 204
    assert client.post("/api/uploads", json=payload(), headers=auth).status_code == 201


@pytest.mark.integration
@pytest.mark.parametrize("state", ["READY", "FAILED", "EXPIRED", "DELETING", "DELETED"])
def test_terminal_states_do_not_consume_active_capacity(client, auth, monkeypatch, state):
    from clouddrive.config import settings
    from clouddrive.db import engine
    from clouddrive.service import query

    monkeypatch.setattr(settings(), "max_active_uploads_per_workspace", 1)
    first = client.post("/api/uploads", json=payload(), headers=auth).json()
    with engine().begin() as conn:
        query(
            conn, "UPDATE files SET status=:status WHERE id=:id", status=state, id=UUID(first["id"])
        )
    assert client.post("/api/uploads", json=payload(), headers=auth).status_code == 201


@pytest.mark.integration
@pytest.mark.parametrize("state", ["FINALIZING", "PROCESSING"])
def test_nonterminal_states_still_consume_active_capacity(client, auth, monkeypatch, state):
    from clouddrive.config import settings
    from clouddrive.db import engine
    from clouddrive.service import query

    monkeypatch.setattr(settings(), "max_active_uploads_per_workspace", 1)
    first = client.post("/api/uploads", json=payload(), headers=auth).json()
    with engine().begin() as conn:
        query(
            conn, "UPDATE files SET status=:status WHERE id=:id", status=state, id=UUID(first["id"])
        )
    assert client.post("/api/uploads", json=payload(), headers=auth).status_code == 429


@pytest.mark.integration
def test_failed_processing_retry_also_obeys_capacity(client, auth, monkeypatch):
    from clouddrive.config import settings
    from clouddrive.db import engine
    from clouddrive.service import query

    monkeypatch.setattr(settings(), "max_active_uploads_per_workspace", 1)
    first = client.post("/api/uploads", json=payload(), headers=auth).json()
    with engine().begin() as conn:
        query(conn, "UPDATE files SET status='FAILED' WHERE id=:id", id=UUID(first["id"]))
    second = client.post("/api/uploads", json=payload(), headers=auth).json()
    assert client.post(f"/api/files/{first['id']}/retry", headers=auth).status_code == 429
    assert client.delete(f"/api/files/{second['id']}", headers=auth).status_code == 204
    assert client.post(f"/api/files/{first['id']}/retry", headers=auth).status_code == 202


@pytest.mark.integration
def test_global_quota_and_metrics_do_not_reveal_other_workspaces(client, auth, monkeypatch):
    from clouddrive.config import settings

    monkeypatch.setattr(settings(), "max_active_uploads_global", 1)
    monkeypatch.setattr(settings(), "metrics_key", "")
    own = client.post("/api/uploads", json=payload(), headers=auth)
    assert own.status_code == 201
    stranger = {"Authorization": "Bearer " + client.post("/api/workspaces").json()["access_key"]}
    denied = client.post("/api/uploads", json=payload(), headers=stranger)
    assert denied.status_code == 429
    mine = client.get("/api/metrics", headers=auth).json()
    theirs = client.get("/api/metrics", headers=stranger).json()
    assert mine["files_by_status"] == {"UPLOADING": 1}
    assert theirs["files_by_status"] == {}
    assert own.json()["id"] not in json.dumps(theirs)
    assert client.get("/api/metrics").status_code == 401
    # Global operational information requires a different explicit secret, and
    # remains disabled in ordinary deployments.
    assert client.get("/api/ops", headers=auth).status_code == 404
    assert (
        client.get("/api/ops", headers={"X-Metrics-Key": "isolated-test-ops-key"}).status_code
        == 404
    )
    monkeypatch.setattr(settings(), "metrics_key", "isolated-test-ops-key")
    assert client.get("/api/ops", headers=auth).status_code == 404
    assert client.get("/api/ops", headers={"X-Metrics-Key": "wrong"}).status_code == 404
    operations = client.get("/api/ops", headers={"X-Metrics-Key": "isolated-test-ops-key"})
    assert operations.status_code == 200
    assert operations.json()["uploads"]["active_uploads"] == 1
    assert (
        operations.json()["db_pool"]["capacity"]
        == settings().db_pool_size + settings().db_max_overflow
    )


@pytest.mark.integration
def test_request_ids_round_trip_and_logs_do_not_contain_credentials(client, auth, monkeypatch):
    from clouddrive import observability

    records = []
    monkeypatch.setattr(observability.logger, "info", records.append)
    response = client.get(
        "/api/workspace?private=should-not-appear",
        headers={**auth, "X-Request-ID": "trace-safe-123"},
    )
    assert response.status_code == 200 and response.headers["X-Request-ID"] == "trace-safe-123"
    logs = "\n".join(records)
    assert "trace-safe-123" in logs
    assert auth["Authorization"] not in logs and "should-not-appear" not in logs
    assert "Authorization" not in logs
    invalid = client.get("/api/workspace", headers={**auth, "X-Request-ID": "x" * 81})
    assert UUID(invalid.headers["X-Request-ID"])


@pytest.mark.integration
def test_exhausted_database_pool_returns_retryable_503(client, auth, monkeypatch):
    from sqlalchemy import create_engine
    from clouddrive import api, service
    from clouddrive.config import settings

    limited = create_engine(settings().database_url, pool_size=1, max_overflow=0, pool_timeout=0.05)
    monkeypatch.setattr(service, "engine", lambda: limited)
    monkeypatch.setattr(api, "engine", lambda: limited)
    try:
        with limited.connect():
            response = client.get("/api/workspace", headers=auth)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
    finally:
        limited.dispose()


@pytest.mark.integration
def test_unexpected_failures_keep_correlation_and_safe_error_observation(client, auth, monkeypatch):
    from clouddrive import observability, service

    records = []
    monkeypatch.setattr(observability.logger, "info", records.append)

    def broken(*args):
        raise RuntimeError("credential-should-not-appear")

    monkeypatch.setattr(service, "initiate", broken)
    response = client.post(
        "/api/uploads", json=payload(), headers={**auth, "X-Request-ID": "trace-failure-123"}
    )
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "trace-failure-123"
    logs = "\n".join(records)
    assert '"status":500' in logs and "RuntimeError" in logs
    assert "credential-should-not-appear" not in logs


@pytest.mark.integration
def test_api_inflight_limit_rejects_without_blocking_health(client, monkeypatch):
    from clouddrive import observability, service
    from clouddrive.config import settings

    monkeypatch.setattr(settings(), "max_api_inflight", 1)
    entered, release = threading.Event(), threading.Event()

    def workspace(_):
        entered.set()
        assert release.wait(timeout=5)
        return {"id": uuid4(), "created_at": datetime.now(timezone.utc)}

    monkeypatch.setattr(service, "workspace", workspace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            client.get, "/api/workspace", headers={"Authorization": "Bearer blocked"}
        )
        assert entered.wait(timeout=5)
        try:
            denied = client.get("/api/workspace", headers={"Authorization": "Bearer blocked"})
            assert denied.status_code == 429 and denied.headers["Retry-After"] == "1"
            assert client.get("/api/health").status_code == 200
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert observability.inflight() == 0


@pytest.mark.integration
def test_listing_batches_parts_and_preserves_receipts(client, auth):
    from sqlalchemy import event
    from clouddrive.db import engine

    for _ in range(3):
        assert client.post("/api/uploads", json=payload(), headers=auth).status_code == 201
    statements = []

    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine(), "before_cursor_execute", capture)
    try:
        listing = client.get("/api/files", headers=auth)
    finally:
        event.remove(engine(), "before_cursor_execute", capture)
    assert listing.status_code == 200 and len(listing.json()["files"]) == 3
    assert all(file["parts"] == [] for file in listing.json()["files"])
    assert sum("FROM upload_parts" in sql for sql in statements) == 1
