"""Hosted-only source -> backup -> NEW Compose project disaster-recovery proof.

Private backup archives and workspace keys are never included in the public
report. No performance fixtures, normal stacks, or other projects are touched.
"""

import argparse
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def utc():
    return datetime.now(timezone.utc).isoformat()


class Side:
    def __init__(self, name, api_port, storage_port, archive):
        self.project = f"clouddrive-ci-ops-{name}"
        self.base = f"http://localhost:{api_port}"
        self.env = {
            **os.environ,
            "OPS_DATABASE": f"clouddrive_ops_{name}_test",
            "OPS_BUCKET": f"clouddrive-ops-{name}-test",
            "OPS_API_PORT": str(api_port),
            "OPS_STORAGE_PORT": str(storage_port),
            "CLOUDDRIVE_BACKUP_DIRECTORY": str(archive),
        }

    def compose(self, *args, check=True):
        return subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", "compose.operations.yaml", *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    def maintenance(self, *args, check=True):
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "maintenance",
            *args,
            check=check,
        )

    def python(self, code):
        return self.compose(
            "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "maintenance", "-c", code
        )

    def api(self, method, path, key=None, **kwargs):
        response = httpx.request(
            method,
            self.base + "/api" + path,
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=60,
            **kwargs,
        )
        response.raise_for_status()
        return response.json() if response.content else None


def upload(side, key, name, content, complete=True, parts=None):
    file = side.api(
        "POST",
        "/uploads",
        key,
        json={
            "request_id": str(uuid4()),
            "name": name,
            "size": len(content),
            "media_type": "application/octet-stream",
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    for index, start in enumerate(range(0, len(content), file["part_size"])):
        if parts is not None and index >= parts:
            break
        chunk = content[start : start + file["part_size"]]
        checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode()
        path = f"/uploads/{file['id']}/parts/{index+1}"
        signed = side.api("POST", path + "/sign", key, json={"checksum": checksum})
        response = httpx.put(signed["url"], headers=signed["headers"], content=chunk, timeout=60)
        response.raise_for_status()
        side.api("PUT", path, key, json={"checksum": checksum, "etag": response.headers["ETag"]})
    if complete:
        side.api("POST", f"/uploads/{file['id']}/complete", key)
    return file


def wait_ready(side, key, ids):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        files = [side.api("GET", f"/uploads/{id}", key) for id in ids]
        if all(file["status"] == "READY" for file in files):
            return files
        if any(file["status"] == "FAILED" for file in files):
            raise RuntimeError("Restored worker verification failed")
        time.sleep(1)
    raise TimeoutError("Restored processing did not finish within 120 seconds")


def verify_download(side, key, file, content):
    signed = side.api("GET", f"/files/{file['id']}/download", key)
    digest, size = hashlib.sha256(), 0
    with httpx.stream("GET", signed["url"], timeout=60) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            digest.update(chunk)
            size += len(chunk)
    assert size == len(content) and digest.hexdigest() == hashlib.sha256(content).hexdigest()
    assert file["sha256"] == digest.hexdigest()
    return {"file_id": file["id"], "bytes": size, "sha256": digest.hexdigest(), "status": "passed"}


def inventory(side):
    return json.loads(side.python("""
import json
from clouddrive.db import engine
from clouddrive.service import query
from clouddrive.storage import s3
from clouddrive.config import settings
with engine().connect() as conn:
 result = {
  'workspaces': query(conn, 'SELECT count(*) FROM workspaces').scalar(),
  'files_by_status': dict(query(conn, 'SELECT status,count(*) FROM files GROUP BY status').all()),
  'active_claims': query(conn, 'SELECT count(*) FROM files WHERE claim IS NOT NULL').scalar(),
  'outbox': query(conn, 'SELECT count(*) FROM processing_outbox').scalar(),
 }
result['objects'] = sum(len(p.get('Contents', [])) for p in s3().get_paginator('list_objects_v2').paginate(Bucket=settings().s3_bucket))
result['multipart_sessions'] = sum(len(p.get('Uploads', [])) for p in s3().get_paginator('list_multipart_uploads').paginate(Bucket=settings().s3_bucket))
print(json.dumps(result))
""").stdout)


def exercise(report, archive):
    source, target = Side("source", 18094, 19015, archive), Side("target", 18095, 19016, archive)
    for side in (source, target):
        # Freshness is required; never commandeer an existing project's volumes.
        assert not json.loads(side.compose("ps", "--all", "--format", "json").stdout or "[]")
        volumes = subprocess.run(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={side.project}",
                "--format",
                "{{.Name}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert not volumes.stdout.strip(), "Refuse existing project volumes"
    source.compose("build", "api", "minio")
    source.compose("up", "--no-build", "-d", "--wait", "api", "worker", "beat")
    owner = source.api("POST", "/workspaces")
    other = source.api("POST", "/workspaces")
    key = owner["access_key"]
    payloads = [b"Restore every committed byte.\n", bytes(range(256)) * 65537]
    ready = [
        upload(source, key, f"ready-{index}.bin", content) for index, content in enumerate(payloads)
    ]
    ready = wait_ready(source, key, [file["id"] for file in ready])
    source.compose("stop", "worker", "beat")
    pending_bytes = b"durable pending processing\n" * 1000
    pending = upload(source, key, "pending.bin", pending_bytes)
    unfinished_bytes = b"original unfinished bytes" * 500000
    unfinished = upload(source, key, "unfinished.bin", unfinished_bytes, complete=False, parts=1)
    deleted = upload(source, key, "deleted.bin", b"explicitly deleted")
    source.api("DELETE", f"/files/{deleted['id']}", key)
    # The actual source remains offline and intact after backup. Restore uses a
    # separate DB, bucket, network, Redis and all-new project-scoped volumes.
    report["last_source_write_acknowledged_at"] = utc()
    report["source_inventory"] = inventory(source)
    assert report["source_inventory"] == {
        "workspaces": 2,
        "files_by_status": {"READY": 2, "PROCESSING": 1, "UPLOADING": 1, "DELETED": 1},
        "active_claims": 0,
        "outbox": 1,
        "objects": 3,
        "multipart_sessions": 1,
    }
    source.compose("stop", "api")
    report["source_quiesced_at"] = utc()
    backup = json.loads(
        source.maintenance("backup", "/backups/snapshot", "--offline-confirmed").stdout
    )
    report["archive_validation"] = json.loads(
        source.maintenance("verify", "/backups/snapshot").stdout
    )
    report["backup"] = {
        k: backup[k]
        for k in ("started_at", "finished_at", "backup_seconds", "recovery_point", "object_count")
    }
    target.compose("up", "--no-build", "-d", "--wait", "postgres", "redis", "minio")
    target.python(
        "from clouddrive.db import migrate; from clouddrive.storage import bootstrap; migrate(); bootstrap()"
    )
    restore = json.loads(
        target.maintenance(
            "restore",
            "/backups/snapshot",
            "--offline-confirmed",
            "--confirm-database",
            target.env["OPS_DATABASE"],
            "--confirm-bucket",
            target.env["OPS_BUCKET"],
        ).stdout
    )
    report["restore"] = restore
    report["restore_archive_sha256"] = hashlib.sha256(
        (archive / "snapshot/manifest.json").read_bytes()
    ).hexdigest()
    refusal = target.maintenance(
        "restore",
        "/backups/snapshot",
        "--offline-confirmed",
        "--confirm-database",
        target.env["OPS_DATABASE"],
        "--confirm-bucket",
        target.env["OPS_BUCKET"],
        check=False,
    )
    assert refusal.returncode != 0
    report["populated_target_refused"] = True
    target.compose("up", "--no-build", "-d", "--wait", "api")
    assert target.api("GET", "/workspace", key)["id"] == owner["id"]
    assert target.api("GET", "/workspace", other["access_key"])["id"] == other["id"]
    report["workspace_keys_verified"] = 2
    outcomes = []
    for file, content in zip(ready, payloads):
        restored = target.api("GET", f"/uploads/{file['id']}", key)
        outcomes.append(verify_download(target, key, restored, content))
        response = httpx.get(
            target.base + f"/api/files/{file['id']}/download",
            headers={"Authorization": f"Bearer {other['access_key']}"},
        )
        assert response.status_code == 404
    resumed = target.api("GET", f"/uploads/{unfinished['id']}", key)
    assert resumed["status"] == "EXPIRED" and resumed["parts"] == []
    assert target.api("GET", f"/uploads/{pending['id']}", key)["status"] == "PROCESSING"
    target.compose("up", "--no-build", "-d", "--wait", "worker", "beat")
    processed = wait_ready(target, key, [pending["id"]])[0]
    outcomes.append(verify_download(target, key, processed, pending_bytes))
    report["verified_downloads"] = outcomes
    report["unfinished_policy"] = {
        "status": resumed["status"],
        "receipt_count": len(resumed["parts"]),
    }
    report["destination_inventory_before_browser_restart"] = inventory(target)
    assert report["destination_inventory_before_browser_restart"] == {
        "workspaces": 2,
        "files_by_status": {"READY": 3, "EXPIRED": 1, "DELETED": 1},
        "active_claims": 0,
        "outbox": 0,
        "objects": 3,
        "multipart_sessions": 0,
    }
    report["committed_object_outcomes"] = {"expected": 3, "verified": len(outcomes), "lost": 0}
    report["rpo_scope"] = (
        "All three committed source objects at the offline recovery point recovered. One unfinished MPU requires source reselection. No accepted writes during snapshot. No contractual RPO."
    )
    report["fresh_projects"] = [source.project, target.project]
    # Real packaged browser imports the original owner key and downloads READY
    # content, then restarts the expired upload with the original file bytes.
    browser = subprocess.run(
        ["node", "scripts/verify-restore-browser.mjs"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        input=json.dumps(
            {
                "base": target.base,
                "workspace": owner["id"],
                "key": key,
                "ready_sha256": hashlib.sha256(payloads[0]).hexdigest(),
                "unfinished_base64": base64.b64encode(unfinished_bytes).decode(),
            }
        ),
        check=True,
    )
    report["packaged_browser"] = json.loads(browser.stdout)
    listing = target.api("GET", "/files", key)["files"]
    restarted = next(file for file in listing if file["name"] == "unfinished.bin")
    assert restarted["id"] != unfinished["id"]
    report["restarted_download"] = verify_download(target, key, restarted, unfinished_bytes)
    cleanup = json.loads(target.maintenance("cleanup").stdout)
    report["cleanup_dry_run"] = cleanup
    report["final_inventory"] = inventory(target)
    assert report["final_inventory"]["objects"] == 4
    assert report["final_inventory"]["multipart_sessions"] == 0
    assert report["final_inventory"]["files_by_status"] == {"READY": 4, "DELETED": 2}
    report["source_unchanged_after_restore"] = inventory(source) == report["source_inventory"]
    assert report["source_unchanged_after_restore"]
    image_output = target.compose("images", "--format", "json").stdout
    try:
        report["image_ids"] = json.loads(image_output)
    except json.JSONDecodeError:
        report["image_ids"] = [
            json.loads(line) for line in image_output.splitlines() if line.strip()
        ]
    report["source_hashes_unchanged"] = all(
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
        for name, digest in report["source_sha256"].items()
    )
    assert report["source_hashes_unchanged"]
    # Never publish keys, signed URLs, private archives or raw exception text.
    report["status"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "test-results/operations/results.json"
    )
    args = parser.parse_args()
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise RuntimeError("This exercise runs only on a fresh GitHub-hosted runner")
    archive = ROOT / "test-results/private-backup"
    archive.mkdir(mode=0o700, parents=True, exist_ok=False)
    # One-shot backup runs as the host UID; normal application containers never
    # mount this directory. Metadata and keys are not uploaded as CI artifacts.
    os.chmod(archive, 0o700)
    report = {
        "status": "running",
        "started_at": utc(),
        "workflow": {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
        },
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "clouddrive/operations.py",
                "clouddrive/listing.py",
                "clouddrive/api.py",
                "clouddrive/service.py",
                "clouddrive/db.py",
                "clouddrive/storage.py",
                "clouddrive/worker.py",
                "clouddrive/config.py",
                *sorted(
                    str(path.relative_to(ROOT))
                    for path in (ROOT / "clouddrive/migrations").glob("*.sql")
                ),
                "scripts/verify-restore.py",
                "scripts/verify-restore-browser.mjs",
                "compose.operations.yaml",
                "Dockerfile",
                "Dockerfile.minio",
                "frontend/src/main.ts",
                "frontend/src/scheduler.ts",
                "frontend/src/hash-worker.ts",
                "frontend/index.html",
                "package.json",
                "package-lock.json",
                "requirements.txt",
                ".github/workflows/operations.yml",
            )
        },
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "environment": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "provider": "MinIO",
            "runner": "github-hosted",
        },
        "scope": "Small functional offline backup/restore exercise; no RPO/RTO guarantee or live AWS measurement",
    }
    try:
        exercise(report, archive)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        print(f"Restore exercise failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        report["finished_at"] = utc()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
