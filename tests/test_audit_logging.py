import json
import traceback
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError

from clouddrive import observability, worker


def test_worker_retry_trace_does_not_include_original_provider_text(monkeypatch):
    records = []
    monkeypatch.setattr(observability.logger, "info", records.append)

    def unavailable(id):
        raise EndpointConnectionError(
            endpoint_url="https://storage.invalid/?X-Amz-Signature=private-key"
        )

    monkeypatch.setattr(worker, "process_file", unavailable)
    worker.process.push_request(id="safe-task", retries=0, delivery_info={})
    try:
        with pytest.raises(RuntimeError) as error:
            worker.process.run(str(uuid4()))
        diagnostic = "".join(traceback.format_exception(error.value))
        assert "private-key" not in diagnostic and "storage.invalid" not in diagnostic
        assert "temporarily unavailable" in diagnostic
        assert "private-key" not in "\n".join(records)
        assert any(json.loads(row)["event"] == "processing_dependency_error" for row in records)
    finally:
        worker.process.pop_request()


def test_reconciler_reports_only_identifiers_and_error_type(monkeypatch):
    records = []
    monkeypatch.setattr(observability.logger, "info", records.append)
    monkeypatch.setattr(
        worker, "engine", lambda: SimpleNamespace(connect=lambda: nullcontext(object()))
    )
    id = uuid4()

    def rows(conn, sql, **params):
        values = [{"id": id, "workspace_id": uuid4()}] if params["status"] == "DELETING" else []
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: values))

    def failed(*args):
        raise RuntimeError("Bearer private-key; https://storage.invalid/?signed=secret")

    monkeypatch.setattr(worker, "query", rows)
    monkeypatch.setattr(worker, "finish_delete", failed)
    worker.reconcile_files()
    event = next(
        json.loads(row) for row in records if json.loads(row)["event"] == "reconciliation_failed"
    )
    assert event["file_id"] == str(id) and event["error_type"] == "RuntimeError"
    assert "private-key" not in "\n".join(records)


def test_dispatcher_trace_redacts_broker_diagnostics(monkeypatch):
    monkeypatch.setattr(worker, "reconcile_files", lambda: None)

    def unavailable():
        raise OSError("redis://user:private-key@broker/0")

    monkeypatch.setattr(worker, "dispatch_pending", unavailable)
    with pytest.raises(RuntimeError) as error:
        worker.dispatch.run()
    assert "private-key" not in "".join(traceback.format_exception(error.value))
