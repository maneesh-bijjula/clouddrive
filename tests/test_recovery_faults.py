"""Safety and synchronization boundaries for opt-in recovery instrumentation."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy.exc import OperationalError

from clouddrive import recovery_faults


def enable(monkeypatch, tmp_path, database="isolated_test", bucket="isolated-test"):
    monkeypatch.setenv("CLOUDDRIVE_RECOVERY_CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery_faults,
        "settings",
        lambda: SimpleNamespace(
            database_url=f"postgresql+psycopg://localhost/{database}", s3_bucket=bucket
        ),
    )


def test_disabled_checkpoint_has_no_configuration_or_filesystem_dependency(monkeypatch):
    monkeypatch.delenv("CLOUDDRIVE_RECOVERY_CONTROL_DIR", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled instrumentation accessed configuration or filesystem")

    monkeypatch.setattr(recovery_faults, "settings", forbidden)
    monkeypatch.setattr(recovery_faults.os, "open", forbidden)
    recovery_faults.checkpoint(
        "worker_streaming",
        "irrelevant-with-hooks-disabled",
        connection=SimpleNamespace(exec_driver_sql=forbidden),
    )


@pytest.mark.parametrize(
    "database,bucket",
    [
        ("production", "isolated-test"),
        ("isolated_test", "production"),
        ("production", "production"),
    ],
)
def test_checkpoint_rejects_non_test_resources_before_writing(
    monkeypatch, tmp_path, database, bucket
):
    enable(monkeypatch, tmp_path, database, bucket)
    with pytest.raises(RuntimeError, match="isolated test database and bucket"):
        recovery_faults.checkpoint("worker_streaming", uuid4())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "name,file_id", [("../../outside", str(uuid4())), ("worker_streaming", "../outside")]
)
def test_checkpoint_rejects_path_components(monkeypatch, tmp_path, name, file_id):
    enable(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        recovery_faults.checkpoint(name, file_id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "mode,error", [("raise_storage", EndpointConnectionError), ("raise_sql", OperationalError)]
)
def test_armed_boundary_fails_once_and_records_secret_free_proof(
    monkeypatch, tmp_path, mode, error
):
    enable(monkeypatch, tmp_path)
    file_id = uuid4()
    arm = tmp_path / f"arm-storage_completed-{file_id}.json"
    arm.write_text(json.dumps({"mode": mode}))
    with pytest.raises(error):
        recovery_faults.checkpoint(
            "storage_completed", file_id, bytes_read=1048576, access_key="private", url="private"
        )
    assert not arm.exists()
    reached = json.loads((tmp_path / f"reached-storage_completed-{file_id}.json").read_text())
    assert reached["bytes_read"] == 1048576
    assert reached["pid"] > 0
    assert "private" not in json.dumps(reached)
    # A real recovery retry crosses the same point without repeating the fault.
    recovery_faults.checkpoint("storage_completed", file_id)
    events = (tmp_path / f"events-{file_id}.jsonl").read_text().splitlines()
    assert len(events) == 2
    assert all(json.loads(event)["file_id"] == str(file_id) for event in events)
    assert "private" not in "\n".join(events)


def test_concurrent_crossings_claim_one_fault_and_keep_valid_events(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    file_id = uuid4()
    (tmp_path / f"arm-outbox_published-{file_id}.json").write_text(
        json.dumps({"mode": "raise_storage"})
    )

    def cross():
        try:
            recovery_faults.checkpoint("outbox_published", file_id)
            return "continued"
        except EndpointConnectionError:
            return "injected"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: cross(), range(12)))
    assert outcomes.count("injected") == 1
    events = (tmp_path / f"events-{file_id}.jsonl").read_text().splitlines()
    assert len(events) == 12
    assert all(json.loads(event)["checkpoint"] == "outbox_published" for event in events)


def test_pause_has_finite_timeout_and_can_be_released(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    timed_out = uuid4()
    (tmp_path / f"arm-worker_streaming-{timed_out}.json").write_text(
        json.dumps({"mode": "pause", "timeout_s": 0})
    )
    with pytest.raises(TimeoutError, match="worker_streaming"):
        recovery_faults.checkpoint("worker_streaming", timed_out)
    released = uuid4()
    (tmp_path / f"arm-worker_streaming-{released}.json").write_text(json.dumps({"mode": "pause"}))
    (tmp_path / f"release-worker_streaming-{released}").touch()
    recovery_faults.checkpoint("worker_streaming", released)


def test_enabled_result_marker_records_transaction_backend_and_outcome(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    file_id = uuid4()
    statements = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalar_one=lambda: 117)

    recovery_faults.checkpoint(
        "worker_result",
        file_id,
        connection=SimpleNamespace(exec_driver_sql=execute),
        bytes_read=1048576,
    )
    recovery_faults.checkpoint("worker_result_committed", file_id, outcome="ready")
    events = [
        json.loads(line) for line in (tmp_path / f"events-{file_id}.jsonl").read_text().splitlines()
    ]
    assert statements == ["SELECT pg_backend_pid()"]
    assert events[0]["backend_pid"] == 117
    assert events[1]["outcome"] == "ready"
