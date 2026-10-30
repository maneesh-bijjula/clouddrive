"""Offline portable backups, empty-target restores, and conservative MPU cleanup.

Run with ``python -m clouddrive.operations --help``. Archives contain private
metadata and object bytes; keep them off Git and encrypt them off-host.
"""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url

from .config import settings
from .db import engine
from .service import query
from .storage import missing, s3
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig

TABLES = ("workspaces", "files", "upload_parts", "processing_outbox")
VERSION = 1
CHUNK = 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, default=str, separators=(",", ":")) + "\n"


def records(path):
    with path.open() as handle:
        for line in handle:
            yield json.loads(line)


def hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def schemas(conn):
    return dict(query(conn, "SELECT name,checksum FROM schema_migrations ORDER BY name").all())


def locked(conn):
    # Acquire before the snapshot. SHARE conflicts with every application write
    # but allows readers. Do not hold a pre-lock repeatable-read snapshot.
    query(conn, "SET LOCAL lock_timeout='10s'")
    query(conn, "LOCK TABLE workspaces,files,upload_parts,processing_outbox IN SHARE MODE")


def backup(destination):
    started = now()
    clock = time.monotonic()
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    (destination / "objects").mkdir(mode=0o700)
    manifest = {
        "format": VERSION,
        "started_at": started,
        "tables": {},
        "objects": {},
        "policy": "READY preserved; committed pending objects reverified; MPU sessions expire",
    }
    client, bucket = s3(), settings().s3_bucket
    with engine().begin() as conn:
        locked(conn)
        manifest["recovery_point"] = query(conn, "SELECT clock_timestamp()").scalar().isoformat()
        manifest["schema_migrations"] = schemas(conn)
        manifest["source_database"] = make_url(settings().database_url).database
        manifest["source_bucket"] = bucket
        for table in TABLES:
            path = destination / f"{table}.jsonl"
            count = 0
            with path.open("x") as handle:
                rows = conn.execute(
                    text(f"SELECT row_to_json(t) FROM {table} t"),
                    execution_options={"yield_per": 100},
                )
                for row in rows.scalars():
                    handle.write(encoded(row))
                    count += 1
            manifest["tables"][table] = {**hash_file(path), "rows": count}
        with (destination / "objects.jsonl").open("x") as inventory:
            for file in records(destination / "files.jsonl"):
                if file["status"] not in {"READY", "PROCESSING", "FINALIZING", "FAILED"}:
                    continue
                try:
                    response = client.get_object(Bucket=bucket, Key=file["object_key"])
                except ClientError as exc:
                    if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"} and file[
                        "status"
                    ] in {"FINALIZING", "FAILED"}:
                        continue
                    raise
                path = destination / "objects" / str(UUID(file["id"]))
                try:
                    with path.open("xb") as handle:
                        while chunk := response["Body"].read(CHUNK):
                            handle.write(chunk)
                finally:
                    response["Body"].close()
                measured = hash_file(path)
                if response.get("Metadata", {}).get("file-id") != file["id"]:
                    raise ValueError("Backup object identity mismatch")
                if file["status"] == "READY" and (
                    measured["bytes"] != file["size"]
                    or measured["sha256"] != file["expected_sha256"]
                    or measured["sha256"] != file["sha256"]
                ):
                    raise ValueError("Backup READY integrity mismatch")
                inventory.write(encoded({"id": file["id"], "key": file["object_key"], **measured}))
        manifest["objects"] = hash_file(destination / "objects.jsonl")
        manifest["object_count"] = sum(1 for _ in records(destination / "objects.jsonl"))
        manifest["locked_until"] = now()
    manifest["finished_at"] = now()
    manifest["backup_seconds"] = time.monotonic() - clock
    # Completion marker is last. A failed or interrupted archive is unrestorable.
    with (destination / "manifest.json").open("x") as handle:
        handle.write(encoded(manifest))
    return manifest


def checked_path(directory, name):
    path = directory / name
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(directory.resolve())
    ):
        raise ValueError("Unsafe or missing archive member")
    return path


def validate_archive(directory):
    directory = Path(directory)
    manifest = json.loads(checked_path(directory, "manifest.json").read_text())
    if manifest["format"] != VERSION or set(manifest["tables"]) != set(TABLES):
        raise ValueError("Unsupported archive format")
    for table in TABLES:
        path = checked_path(directory, f"{table}.jsonl")
        expected = manifest["tables"][table]
        if hash_file(path) != {key: expected[key] for key in ("bytes", "sha256")}:
            raise ValueError("Metadata checksum mismatch")
        if sum(1 for _ in records(path)) != expected["rows"]:
            raise ValueError("Metadata count mismatch")
    if hash_file(checked_path(directory, "objects.jsonl")) != manifest["objects"]:
        raise ValueError("Object inventory checksum mismatch")
    objects = {}
    for obj in records(directory / "objects.jsonl"):
        id = str(UUID(obj["id"]))
        path = checked_path(directory, f"objects/{id}")
        if id in objects or hash_file(path) != {key: obj[key] for key in ("bytes", "sha256")}:
            raise ValueError("Object checksum or identity mismatch")
        objects[id] = obj
    if len(objects) != manifest["object_count"]:
        raise ValueError("Object count mismatch")
    file_ids = set()
    for file in records(directory / "files.jsonl"):
        id = str(UUID(file["id"]))
        file_ids.add(id)
        obj = objects.get(id)
        if obj and obj["key"] != file["object_key"]:
            raise ValueError("Object key mismatch")
        if file["status"] in {"READY", "PROCESSING"} and not obj:
            raise ValueError("Missing committed object")
        if file["status"] == "READY" and (
            obj["bytes"] != file["size"]
            or obj["sha256"] != file["expected_sha256"]
            or obj["sha256"] != file["sha256"]
        ):
            raise ValueError("READY manifest integrity mismatch")
    if not objects.keys() <= file_ids:
        raise ValueError("Unreferenced archive object")
    return manifest, objects


def restored_file(file, objects):
    file = dict(file)
    status = file["status"]
    file.update(upload_id=None, claim=None, lease_until=None)
    if status in {"UPLOADING", "FINALIZING"} and file["id"] not in objects:
        file.update(
            status="EXPIRED",
            result=None,
            sha256=None,
            error="Restored unfinished upload. Select the original file to restart.",
        )
    elif status in {"PROCESSING", "FINALIZING"}:
        file.update(status="PROCESSING", result=None, sha256=None, error=None)
    elif status == "DELETING":
        file.update(status="DELETED", result=None)
    return file


def restore(directory, confirm_database, confirm_bucket):
    clock, started = time.monotonic(), now()
    config = settings()
    database = make_url(config.database_url).database
    if confirm_database != database or confirm_bucket != config.s3_bucket:
        raise ValueError("Confirm exact destination database and bucket")
    manifest, objects = validate_archive(directory)
    directory = Path(directory)
    client, bucket = s3(), config.s3_bucket
    # The caller creates a NEW empty bucket. Never create, empty or overwrite one.
    client.head_bucket(Bucket=bucket)
    if client.list_objects_v2(Bucket=bucket, MaxKeys=1).get(
        "KeyCount", 0
    ) or client.list_multipart_uploads(Bucket=bucket, MaxUploads=1).get("Uploads"):
        raise ValueError("Restore requires an empty destination bucket")
    with engine().begin() as conn:
        query(conn, "SET LOCAL lock_timeout='10s'")
        query(conn, "LOCK TABLE workspaces,files,upload_parts,processing_outbox IN EXCLUSIVE MODE")
        if schemas(conn) != manifest["schema_migrations"]:
            raise ValueError("Restore schema does not match archive")
        if any(query(conn, f"SELECT EXISTS(SELECT 1 FROM {table})").scalar() for table in TABLES):
            raise ValueError("Restore requires an empty destination database")
        for table in ("workspaces", "files"):
            for row in records(directory / f"{table}.jsonl"):
                if table == "files":
                    row = restored_file(row, objects)
                query(
                    conn,
                    f"INSERT INTO {table} SELECT * FROM json_populate_record(NULL::{table},CAST(:row AS json))",
                    row=json.dumps(row),
                )
        for id, obj in objects.items():
            # An explicit empty-target precondition and an offline destination
            # are mandatory: S3 has no cross-system transaction with PostgreSQL.
            with checked_path(directory, f"objects/{id}").open("rb") as body:
                client.upload_fileobj(
                    body,
                    bucket,
                    obj["key"],
                    ExtraArgs={
                        "ContentType": "application/octet-stream",
                        "Metadata": {"file-id": id},
                    },
                    Config=TransferConfig(
                        multipart_threshold=8 * CHUNK,
                        multipart_chunksize=8 * CHUNK,
                        max_concurrency=1,
                        use_threads=False,
                    ),
                )
            response = client.get_object(Bucket=bucket, Key=obj["key"])
            digest, size = hashlib.sha256(), 0
            try:
                while chunk := response["Body"].read(CHUNK):
                    size += len(chunk)
                    digest.update(chunk)
            finally:
                response["Body"].close()
            if size != obj["bytes"] or digest.hexdigest() != obj["sha256"]:
                raise ValueError("Restored storage integrity mismatch")
        query(
            conn,
            "INSERT INTO processing_outbox(file_id) SELECT id FROM files WHERE status='PROCESSING'",
        )
        counts = dict(query(conn, "SELECT status,count(*) FROM files GROUP BY status").all())
    return {
        "status": "passed",
        "started_at": started,
        "finished_at": now(),
        "restore_seconds": time.monotonic() - clock,
        "recovery_point": manifest["recovery_point"],
        "objects_verified": len(objects),
        "files_by_status": counts,
    }


def managed_key(key):
    try:
        workspace, file = key.split("/")
        return str(UUID(workspace)) == workspace and str(UUID(file)) == file
    except (ValueError, AttributeError):
        return False


def cleanup_multipart(*, apply=False, older_than_seconds=86400, limit=100):
    if older_than_seconds < 86400 or not 1 <= limit <= 1000:
        raise ValueError("Cleanup requires at least 24 hours grace and limit 1..1000")
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    client, bucket = s3(), settings().s3_bucket
    outcomes = []
    for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
        for upload in page.get("Uploads", []):
            if len(outcomes) >= limit:
                return {"apply": apply, "outcomes": outcomes, "truncated": True}
            if upload["Initiated"] >= cutoff or not managed_key(upload["Key"]):
                continue
            with engine().begin() as conn:
                query(conn, "SET LOCAL lock_timeout='5s'")
                # An initializer creates MPU while holding a table write lock.
                # Wait for it to commit before deciding that its MPU is orphaned.
                query(conn, "LOCK TABLE files IN SHARE MODE")
                file = (
                    query(
                        conn,
                        "SELECT status,upload_id FROM files WHERE object_key=:key",
                        key=upload["Key"],
                    )
                    .mappings()
                    .first()
                )
                protected = file and (file["status"] not in {"DELETED", "EXPIRED"})
                action = "protected" if protected else "would_abort"
                if apply and not protected:
                    try:
                        client.abort_multipart_upload(
                            Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                        )
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] != "NoSuchUpload":
                            raise
                    action = "aborted"
                outcomes.append(
                    {
                        "action": action,
                        "initiated_at": upload["Initiated"].isoformat(),
                        "object_key": upload["Key"],
                        "upload_id_sha256": hashlib.sha256(upload["UploadId"].encode()).hexdigest(),
                    }
                )
    return {"apply": apply, "outcomes": outcomes, "truncated": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        command = sub.add_parser(name)
        command.add_argument("directory", type=Path)
        command.add_argument(
            "--offline-confirmed",
            action="store_true",
            required=True,
            help="Application writers stopped and ingress disabled; required",
        )
        if name == "restore":
            command.add_argument("--confirm-database", required=True)
            command.add_argument("--confirm-bucket", required=True)
    verify = sub.add_parser("verify", help="Check archive checksums without accessing any server")
    verify.add_argument("directory", type=Path)
    clean = sub.add_parser("cleanup")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--older-than-seconds", type=int, default=86400)
    clean.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "backup":
        result = backup(args.directory)
    elif args.command == "restore":
        result = restore(args.directory, args.confirm_database, args.confirm_bucket)
    elif args.command == "verify":
        manifest, objects = validate_archive(args.directory)
        result = {
            "status": "passed",
            "recovery_point": manifest["recovery_point"],
            "object_count": len(objects),
            "object_bytes": sum(obj["bytes"] for obj in objects.values()),
        }
    else:
        result = cleanup_multipart(
            apply=args.apply, older_than_seconds=args.older_than_seconds, limit=args.limit
        )
    # Do not print private metadata or endpoints/credentials.
    print(
        encoded(
            {
                key: value
                for key, value in result.items()
                if key not in {"tables", "schema_migrations", "source_database", "source_bucket"}
            }
        ),
        end="",
    )


if __name__ == "__main__":
    main()
