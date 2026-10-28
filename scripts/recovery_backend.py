"""Run one named backend recovery case against the dedicated packaged stack.

Faults use test-only file checkpoints, real SIGKILLs and actual dependency
outages. No production data is truncated, no timestamps/leases are accelerated,
and every attempted case emits a result, including failures.
"""

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.engine import make_url

from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.storage import s3

CASES = {
    "R10": "API killed during incomplete multipart upload",
    "R11": "API killed after durable FINALIZING intent",
    "R12": "Storage completion reply lost at the client boundary",
    "R13": "Completion database transaction rolls back after storage completion",
    "R14": "Completion commits then API dies before replying",
    "R15": "Redis unavailable before publication",
    "R16": "Dispatcher SIGKILL after publication before outbox commit",
    "R17": "Published broker message removed before worker delivery",
    "R18": "Worker child SIGKILL during streaming verification",
    "R19": "PostgreSQL unavailable during worker result transaction",
    "R20": "MinIO disconnected during an active worker stream",
}


class Fixture:
    """Generate repeatable bytes one bounded part at a time, never a full file."""

    block_size = 1024 * 1024

    def __init__(self, case):
        self.seed = f"CloudDrive recovery matrix v1/{case}".encode()
        self.size = (64 if case == "R20" else 17) * self.block_size + 17
        digest = hashlib.sha256()
        for offset in range(0, self.size, self.block_size):
            digest.update(self.read(offset, min(self.block_size, self.size - offset)))
        self.sha256 = digest.hexdigest()

    def read(self, offset, length):
        result = bytearray()
        while length:
            index, within = divmod(offset, self.block_size)
            pattern = hashlib.sha256(self.seed + str(index).encode()).digest()
            block = pattern * (self.block_size // len(pattern))
            take = min(length, self.block_size - within)
            result.extend(block[within : within + take])
            offset += take
            length -= take
        return bytes(result)


class Run:
    def __init__(self, args):
        self.args = args
        self.start = time.monotonic()
        self.restored = None
        self.restoration_requested = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.base = args.base_url.rstrip("/")
        self.control = Path(args.control_dir).resolve()
        if not self.control.is_relative_to(ROOT.resolve() / "test-results"):
            raise RuntimeError(
                "Recovery control directory must be inside this repository's test-results"
            )
        self.control.mkdir(parents=True, exist_ok=True)
        self.fixture = Fixture(args.case)
        self.token = None
        self.file = None
        self.managed = set()
        self.oneoff = None
        self.part_puts = []
        self.completion_requests = 0
        self.report = {
            "id": args.case,
            "title": CASES[args.case],
            "status": "running",
            "fault_observed": False,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "deadline_after_restoration_s": 180,
            "source": {"bytes": self.fixture.size, "sha256": self.fixture.sha256},
            "fault_proof": {},
            "events": [],
        }
        self.check_isolation()

    def command(self, *command, timeout=45):
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"Command failed: {' '.join(command[:5])}; exit={result.returncode}")
        return result.stdout.strip()

    def compose(self, *command, **kwargs):
        return self.command(
            "docker",
            "compose",
            "-p",
            self.args.project,
            "-f",
            "compose.recovery.yaml",
            *command,
            **kwargs,
        )

    def check_isolation(self):
        config = settings()
        url = urllib.parse.urlsplit(self.base)
        database = make_url(config.database_url)
        if (
            url.hostname not in {"localhost", "127.0.0.1"}
            or url.port != 18092
            or database.host not in {"localhost", "127.0.0.1"}
            or database.port != 15438
            or database.database != "clouddrive_recovery_test"
            or config.s3_bucket != "clouddrive-recovery-test"
            or urllib.parse.urlsplit(config.s3_endpoint).port != 19012
            or not re.fullmatch(r"clouddrive-[a-z0-9-]*recovery[a-z0-9-]*", self.args.project)
        ):
            raise RuntimeError(
                "Recovery runner requires the dedicated loopback database/bucket/ports"
            )
        raw = self.compose("ps", "--all", "--format", "json")
        try:
            containers = json.loads(raw or "[]")
        except json.JSONDecodeError:
            containers = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if isinstance(containers, dict):
            containers = [containers]
        if not containers:
            raise RuntimeError("No dedicated recovery containers found")
        for item in containers:
            if item.get("Project") != self.args.project:
                raise RuntimeError("Unexpected Compose project")
        api = next(item for item in containers if item["Service"] == "api")
        if not any(p.get("PublishedPort") == 18092 for p in api.get("Publishers") or []):
            raise RuntimeError("API loopback port does not belong to the recovery project")
        self.report["environment"] = {
            "compose_project": self.args.project,
            "database": database.database,
            "bucket": config.s3_bucket,
            "containers": [{"id": item["ID"], "service": item["Service"]} for item in containers],
        }

    def http(self, path, method="GET", body=None, token=None, timeout=20):
        headers = {}
        if token or self.token:
            headers["Authorization"] = "Bearer " + (token or self.token)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response) if response.status != 204 else None
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def expect(self, path, method="GET", body=None, status=200, **kwargs):
        actual, result = self.http(path, method, body, **kwargs)
        assert actual == status, f"{method} {path}: expected {status}, got {actual}"
        return result

    def wait(self, predicate, description, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.2)
        raise AssertionError(f"Timed out waiting for {description}")

    def ready_api(self):
        self.wait(lambda: self.http("/api/health")[0] == 200, "API readiness", timeout=60)

    def db(self, sql, **params):
        with engine().begin() as connection:
            return [dict(row) for row in connection.execute(text(sql), params).mappings()]

    def metadata(self):
        return self.db("SELECT * FROM files WHERE id=:id", id=UUID(self.file["id"]))[0]

    def outbox(self):
        return self.db(
            "SELECT count(*) AS count FROM processing_outbox WHERE file_id=:id",
            id=UUID(self.file["id"]),
        )[0]["count"]

    def setup_file(self):
        workspace = self.expect("/api/workspaces", "POST", status=201)
        self.token = workspace["access_key"]
        self.workspace = workspace["id"]
        self.outsider = self.expect("/api/workspaces", "POST", status=201)["access_key"]
        self.request_id = str(uuid4())
        self.file = self.expect(
            "/api/uploads",
            "POST",
            {
                "request_id": self.request_id,
                "name": f"recovery-{self.args.case}.bin",
                "size": self.fixture.size,
                "sha256": self.fixture.sha256,
                "media_type": "application/octet-stream",
            },
            status=201,
        )
        self.report["file_id"] = self.file["id"]
        self.report["part_size"] = self.file["part_size"]
        self.expect(f"/api/files/{self.file['id']}/download", status=409)
        self.expect(f"/api/files/{self.file['id']}/download", status=404, token=self.outsider)
        self.report["preverification_download_blocked"] = True
        self.report["wrong_workspace_download_blocked"] = True

    def upload_part(self, number):
        offset = (number - 1) * self.file["part_size"]
        chunk = self.fixture.read(offset, min(self.file["part_size"], self.fixture.size - offset))
        checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode()
        signed = self.expect(
            f"/api/uploads/{self.file['id']}/parts/{number}/sign", "POST", {"checksum": checksum}
        )
        if not signed["uploaded"]:
            self.part_puts.append(number)
            request = urllib.request.Request(
                signed["url"], data=chunk, headers=signed["headers"], method="PUT"
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                assert response.status == 200
                etag = response.headers["ETag"]
            self.expect(
                f"/api/uploads/{self.file['id']}/parts/{number}",
                "PUT",
                {"checksum": checksum, "etag": etag},
            )

    def upload_all(self, first=1):
        for number in range(first, self.file["part_count"] + 1):
            self.upload_part(number)

    def complete(self):
        self.completion_requests += 1
        return self.http(f"/api/uploads/{self.file['id']}/complete", "POST")

    def begin_complete(self):
        return self.pool.submit(self.complete)

    def arm(self, checkpoint, mode="pause"):
        path = self.control / f"arm-{checkpoint}-{self.file['id']}.json"
        path.write_text(json.dumps({"mode": mode, "timeout_s": 120}))
        path.chmod(0o666)

    def reached(self, checkpoint):
        path = self.control / f"reached-{checkpoint}-{self.file['id']}.json"
        return self.wait(
            lambda: json.loads(path.read_text()) if path.exists() else None, checkpoint, 60
        )

    def release(self, checkpoint):
        (self.control / f"release-{checkpoint}-{self.file['id']}").touch(mode=0o666)

    def hook_events(self):
        path = self.control / f"events-{self.file['id']}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def event(self, checkpoint):
        return next(
            (event for event in self.hook_events() if event["checkpoint"] == checkpoint), None
        )

    def stop(self, *services):
        self.managed.update(services)
        self.compose("stop", "--timeout", "2", *services)
        for service in services:
            container = self.compose("ps", "--all", "--quiet", service)
            state = json.loads(self.command("docker", "inspect", container))[0]["State"]
            assert not state["Running"]
            self.report["events"].append(
                {
                    "action": "container_stopped",
                    "service": service,
                    "container": container,
                    "exit_code": state["ExitCode"],
                    "elapsed_s": round(time.monotonic() - self.start, 6),
                }
            )

    def start_services(self, *services):
        self.request_restoration()
        self.compose("start", *services)
        self.managed.difference_update(services)

    def request_restoration(self):
        if self.restoration_requested is None:
            self.restoration_requested = time.monotonic()
            self.report["restoration_requested_elapsed_s"] = round(
                self.restoration_requested - self.start, 6
            )

    def kill_api(self):
        self.compose("kill", "--signal", "SIGKILL", "api")
        self.managed.add("api")
        state = json.loads(
            self.command("docker", "inspect", self.compose("ps", "--all", "--quiet", "api"))
        )[0]["State"]
        assert state["ExitCode"] == 137 and not state["Running"]
        self.report["fault_proof"]["api_exit_code"] = state["ExitCode"]

    def restore_clock(self):
        self.request_restoration()
        self.restored = time.monotonic()
        self.report["restoration_sequence_finished_elapsed_s"] = round(
            self.restored - self.start, 6
        )

    def queue_count(self):
        return int(self.compose("exec", "-T", "redis", "redis-cli", "LLEN", "celery"))

    def confirm_fault(self, **proof):
        self.report["fault_observed"] = True
        self.report["fault_proof"].update(proof)

    def assert_future_failed(self, future):
        try:
            status, _ = future.result(timeout=15)
            assert status >= 500, f"Expected interrupted request, got {status}"
            self.report["fault_proof"]["request_status"] = status
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            self.report["fault_proof"]["request_connection_lost"] = True

    def run_fault(self):
        case = self.args.case
        if case == "R10":
            self.upload_part(1)
            self.kill_api()
            self.start_services("api")
            self.ready_api()
            self.restore_clock()
            persisted = self.expect(f"/api/uploads/{self.file['id']}")
            assert len([p for p in persisted["parts"] if p["etag"]]) == 1
            self.confirm_fault(acknowledged_part_survived=1, mechanism="real_api_SIGKILL")
            self.upload_all(first=2)
            assert self.complete()[0] == 202
            return
        self.upload_all()
        if case in {"R11", "R14"}:
            if case == "R14":
                self.stop("beat", "worker")
            point = "finalizing_committed" if case == "R11" else "completion_committed"
            self.arm(point)
            future = self.begin_complete()
            marker = self.reached(point)
            before = self.metadata()["status"]
            assert before == ("FINALIZING" if case == "R11" else "PROCESSING")
            if case == "R14":
                assert self.outbox() == 1
                self.report["fault_proof"]["committed_outbox_rows_before_kill"] = 1
            self.kill_api()
            self.assert_future_failed(future)
            self.start_services("api")
            self.ready_api()
            self.restore_clock()
            self.confirm_fault(
                checkpoint=marker, durable_status=before, mechanism="real_api_SIGKILL"
            )
            if case == "R14":
                assert self.complete()[0] == 202
                self.start_services("worker", "beat")
                self.restore_clock()
            # R11 deliberately waits for the natural 30-second reconciler age.
            return
        if case in {"R12", "R13"}:
            point = "storage_completed" if case == "R12" else "outbox_before_commit"
            self.arm(point, "raise_storage" if case == "R12" else "raise_sql")
            status, _ = self.complete()
            marker = self.reached(point)
            assert status == 503
            metadata = self.metadata()
            assert metadata["status"] == "FINALIZING" and self.outbox() == 0
            assert (
                s3().head_object(Bucket=settings().s3_bucket, Key=metadata["object_key"])[
                    "ContentLength"
                ]
                == self.fixture.size
            )
            self.confirm_fault(
                checkpoint=marker,
                mechanism="function_boundary_exception_injection",
                request_status=status,
                durable_status=metadata["status"],
                committed_outbox_rows=0,
                completed_storage_object_present=True,
            )
            self.restore_clock()
            assert self.complete()[0] == 202
            return
        if case == "R15":
            self.stop("redis")
            assert self.complete()[0] == 202
            assert self.outbox() == 1 and self.metadata()["status"] == "PROCESSING"
            # Prove a dispatch attempt actually tried the unavailable broker.
            result = self.compose(
                "run",
                "--rm",
                "--no-deps",
                "worker",
                "python",
                "-c",
                "from clouddrive.worker import dispatch_pending; "
                "\ntry: dispatch_pending()\nexcept Exception as e: print(type(e).__name__)\nelse: raise SystemExit(2)",
                timeout=45,
            )
            assert "OperationalError" in result or "ConnectionError" in result
            assert self.outbox() == 1
            self.confirm_fault(
                mechanism="real_redis_container_outage",
                publication_error=result.splitlines()[-1],
                durable_outbox_rows=1,
            )
            self.start_services("redis")
            self.restore_clock()
            return
        if case in {"R16", "R17"}:
            self.stop("beat", "worker")
            assert (
                self.db("SELECT count(*) AS count FROM files WHERE status='PROCESSING'")[0]["count"]
                == 0
            )
            # The isolated stack's scheduled dispatch messages are unrelated to
            # this case. Remove that backlog before staging the one lost task.
            self.compose("exec", "-T", "redis", "redis-cli", "DEL", "celery")
            assert self.queue_count() == 0
            assert self.complete()[0] == 202 and self.outbox() == 1
            self.arm("outbox_published")
            self.oneoff = f"{self.args.project}-dispatcher-{case.lower()}-{uuid4().hex[:8]}"
            container = self.compose(
                "run",
                "--detach",
                "--no-deps",
                "--name",
                self.oneoff,
                "worker",
                "python",
                "-c",
                "from clouddrive.worker import dispatch_pending; dispatch_pending()",
            ).splitlines()[-1]
            marker = self.reached("outbox_published")
            count = self.queue_count()
            assert count == 1 and self.outbox() == 1
            if case == "R16":
                self.command("docker", "kill", "--signal", "SIGKILL", container)
                state = json.loads(self.command("docker", "inspect", container))[0]["State"]
                assert state["ExitCode"] == 137 and self.outbox() == 1
                self.confirm_fault(
                    mechanism="real_packaged_dispatcher_SIGKILL",
                    checkpoint=marker,
                    dispatcher_exit_code=137,
                    queue_messages=count,
                    durable_outbox_rows=1,
                )
            else:
                self.release("outbox_published")
                exit_code = self.command("docker", "wait", container)
                assert exit_code == "0" and self.outbox() == 0
                # LPOP removes an actual queued Celery message before any worker
                # can consume it. The payload is never printed or published.
                removed = self.compose("exec", "-T", "redis", "redis-cli", "LPOP", "celery")
                message = json.loads(removed)
                assert message["headers"]["task"] == "clouddrive.process"
                payload = json.loads(base64.b64decode(message["body"]))
                assert payload[0] == [self.file["id"]]
                assert self.queue_count() == count - 1
                self.confirm_fault(
                    mechanism="real_broker_message_removal",
                    checkpoint=marker,
                    removed_task=message["headers"]["task"],
                    outbox_rows_after_publish=0,
                    queue_before=count,
                    queue_after=count - 1,
                )
            self.command("docker", "rm", self.oneoff)
            self.oneoff = None
            self.start_services("worker", "beat")
            self.restore_clock()
            return
        if case in {"R18", "R19", "R20"}:
            point = "worker_result" if case == "R19" else "worker_streaming"
            self.arm(point)
            assert self.complete()[0] == 202
            marker = self.reached(point)
            metadata = self.metadata()
            assert metadata["claim"] is not None
            if case == "R18":
                # Kill the exact streaming pool child; Celery's main process
                # remains alive and replaces it. The file's lease stays intact.
                self.compose(
                    "exec",
                    "-T",
                    "worker",
                    "python",
                    "-c",
                    f"import os; os.kill({int(marker['pid'])}, 9)",
                )
                self.wait(
                    lambda: self.compose(
                        "exec",
                        "-T",
                        "worker",
                        "python",
                        "-c",
                        f"import os; print(os.path.exists('/proc/{int(marker['pid'])}'))",
                    )
                    == "False",
                    "killed worker child exit",
                )
                assert self.metadata()["claim"] == metadata["claim"]
                self.confirm_fault(
                    mechanism="real_worker_pool_child_SIGKILL",
                    checkpoint=marker,
                    persisted_claim=str(metadata["claim"]),
                    lease_until=metadata["lease_until"].isoformat(),
                )
                self.restore_clock()
            elif case == "R19":
                assert marker.get("backend_pid", 0) > 0
                self.stop("postgres")
                self.release(point)
                failed = self.wait(
                    lambda: self.event("worker_failed"), "worker final-transaction failure"
                )
                assert failed["error_type"] in {"OperationalError", "InterfaceError"}
                self.confirm_fault(
                    mechanism="real_postgresql_container_outage",
                    checkpoint=marker,
                    failed_attempt=failed,
                    streamed_bytes=self.fixture.size,
                )
                self.start_services("postgres")
                engine().dispose()
                self.ready_api()
                self.restore_clock()
            else:
                assert 0 < marker["bytes_read"] < self.fixture.size
                self.stop("minio")
                self.release(point)
                failed = self.wait(
                    lambda: self.event("worker_failed"),
                    "actual interrupted storage read",
                    timeout=50,
                )
                assert failed["error_type"] in {
                    "ResponseStreamingError",
                    "ReadTimeoutError",
                    "ConnectionClosedError",
                    "IncompleteReadError",
                }, failed
                self.confirm_fault(
                    mechanism="real_minio_container_disconnect_during_stream",
                    checkpoint=marker,
                    failed_attempt=failed,
                    source_bytes=self.fixture.size,
                )
                self.start_services("minio")
                self.ready_api()
                self.restore_clock()
            return
        raise AssertionError("Unknown case")

    def verify(self):
        assert self.restoration_requested is not None
        deadline = self.restoration_requested + 180
        while time.monotonic() < deadline:
            status, current = self.http(f"/api/uploads/{self.file['id']}")
            assert status == 200
            assert current["status"] not in {"FAILED", "DELETED", "DELETING", "EXPIRED"}, current[
                "status"
            ]
            if current["status"] == "READY":
                break
            download_status, _ = self.http(f"/api/files/{self.file['id']}/download")
            if download_status == 200:
                # Verification may commit between the preceding metadata read
                # and this request. Re-read before treating that as a failure.
                fresh = self.expect(f"/api/uploads/{self.file['id']}")
                assert fresh["status"] == "READY"
            else:
                assert download_status == 409
            time.sleep(0.5)
        else:
            raise AssertionError("Recovery did not reach READY within 180 seconds")
        assert current["sha256"] == self.fixture.sha256
        assert current["result"]["bytes_verified"] == self.fixture.size
        ready_observed = time.monotonic()
        assert self.restoration_requested is not None
        self.report["ready_observed_elapsed_s"] = round(ready_observed - self.start, 6)
        self.report["recovery_s"] = round(ready_observed - self.restoration_requested, 6)
        self.report["post_restoration_sequence_ready_observation_s"] = round(
            ready_observed - (self.restored or self.restoration_requested), 6
        )
        self.report["recovery_clock_definition"] = (
            "Restoration request (before Docker start or HTTP completion retry) to API READY observation; "
            "R18 begins after confirmed worker death and includes the natural lease wait. "
            "The separately labelled post-restoration sequence observation excludes startup/readiness probes."
        )
        url = self.expect(f"/api/files/{self.file['id']}/download")["url"]
        size, digest = 0, hashlib.sha256()
        with urllib.request.urlopen(url, timeout=30) as response:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        assert size == self.fixture.size and digest.hexdigest() == self.fixture.sha256
        self.expect(f"/api/files/{self.file['id']}/download", status=404, token=self.outsider)
        metadata_count = self.db(
            "SELECT count(*) AS count FROM files WHERE workspace_id=:workspace",
            workspace=UUID(self.workspace),
        )[0]["count"]
        objects = (
            s3()
            .list_objects_v2(Bucket=settings().s3_bucket, Prefix=f"{self.workspace}/")
            .get("Contents", [])
        )
        multiparts = (
            s3()
            .list_multipart_uploads(Bucket=settings().s3_bucket, Prefix=f"{self.workspace}/")
            .get("Uploads", [])
        )
        assert metadata_count == 1 and len(objects) == 1 and not multiparts
        assert current["result"] and self.metadata()["claim"] is None
        commits = self.wait(
            lambda: [
                event
                for event in self.hook_events()
                if event["checkpoint"] == "worker_result_committed"
                and event.get("outcome") == "ready"
            ],
            "committed result event",
            timeout=5,
        )
        assert len(commits) == 1, f"Expected one committed logical result, found {len(commits)}"
        events = self.hook_events()
        publications = sum(event["checkpoint"] == "outbox_published" for event in events)
        streams = sum(event["checkpoint"] == "worker_streaming" for event in events)
        if self.args.case in {"R16", "R17"}:
            assert (
                publications >= 2
            ), "Recovery must publish work again after the proven lost delivery"
        if self.args.case in {"R18", "R19", "R20"}:
            assert streams >= 2, "Recovery must reopen and verify the object from the beginning"
        if self.args.case in {"R12", "R13"}:
            assert sum(event["checkpoint"] == "storage_completed" for event in events) == 1
        self.report.update(
            status="passed",
            download={"bytes": size, "sha256": digest.hexdigest()},
            worker={"bytes": current["result"]["bytes_verified"], "sha256": current["sha256"]},
            final_status=current["status"],
            metadata_count=metadata_count,
            object_count=len(objects),
            multipart_count=len(multiparts),
            committed_result_count=len(commits),
            completion_requests=self.completion_requests,
            part_puts=self.part_puts,
            retried_parts={
                str(number): self.part_puts.count(number) - 1
                for number in set(self.part_puts)
                if self.part_puts.count(number) > 1
            },
            hook_events=events,
            task_attempts=[
                event for event in self.hook_events() if event["checkpoint"] == "task_received"
            ],
            publication_count=publications,
            stream_attempts=streams,
        )

    def cleanup(self):
        # Always unblock held processes before restoring dependencies. Never
        # remove another run's checkpoint files, containers or storage.
        if self.file:
            for path in self.control.glob(f"arm-*-{self.file['id']}.json"):
                path.unlink(missing_ok=True)
            for path in self.control.glob(f"claimed-*-{self.file['id']}.json"):
                point = path.name[len("claimed-") : -len(f"-{self.file['id']}.json")]
                self.release(point)
        if self.oneoff:
            subprocess.run(["docker", "rm", "-f", self.oneoff], capture_output=True)
        if self.managed:
            self.start_services(*sorted(self.managed))
        self.ready_api()
        if self.file:
            status, _ = self.http(f"/api/files/{self.file['id']}", "DELETE")
            self.report["cleanup_delete_status"] = status
            assert status == 204, f"Owned test object cleanup returned {status}"
        self.pool.shutdown(wait=False, cancel_futures=True)

    def run(self):
        try:
            self.ready_api()
            self.setup_file()
            self.run_fault()
            assert self.report["fault_observed"]
            self.verify()
        except Exception as error:
            self.report["status"] = "failed"
            self.report["error"] = {"type": type(error).__name__, "message": str(error)[:1200]}
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.report["cleanup_error"] = {"type": type(error).__name__}
                self.report["status"] = "failed"
            self.report["elapsed_s"] = round(time.monotonic() - self.start, 6)
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="clouddrive-sprint3-recovery")
    parser.add_argument("--base-url", default="http://localhost:18092")
    parser.add_argument("--control-dir", required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve first so a second invocation cannot overwrite prior evidence.
    with args.output.open("x") as output:
        try:
            result = Run(args).run()
        except Exception as error:
            result = {
                "id": args.case,
                "status": "failed",
                "fault_observed": False,
                "elapsed_s": 0,
                "error": {"type": type(error).__name__, "message": str(error)[:1200]},
            }
        json.dump(result, output, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {
                "id": args.case,
                "status": result["status"],
                "elapsed_s": result["elapsed_s"],
                "output": str(args.output),
            }
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
