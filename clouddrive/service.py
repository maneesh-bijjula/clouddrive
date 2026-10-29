import hashlib
import secrets
from contextlib import contextmanager
from urllib.parse import quote
from uuid import uuid4

from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import text

from .config import settings
from .db import engine
from .models import part_count, part_length
from .observability import event
from .recovery_faults import checkpoint
from .storage import head, missing, remote_part, s3


def query(conn, sql, **params):
    return conn.execute(text(sql), params)


def workspace(token):
    if not token or len(token) > 200:
        raise HTTPException(401, "A workspace access key is required")
    with engine().connect() as conn:
        row = (
            query(
                conn,
                "SELECT * FROM workspaces WHERE token_hash=:hash",
                hash=hashlib.sha256(token.encode()).hexdigest(),
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(401, "Workspace access key is invalid")
    return row


def create_workspace():
    token = secrets.token_urlsafe(32)
    id = uuid4()
    with engine().begin() as conn:
        query(
            conn,
            "INSERT INTO workspaces(id,token_hash) VALUES (:id,:hash)",
            id=id,
            hash=hashlib.sha256(token.encode()).hexdigest(),
        )
    return {"id": id, "access_key": token}


def get_file(conn, workspace_id, id, lock=False):
    row = (
        query(
            conn,
            "SELECT * FROM files WHERE id=:id AND workspace_id=:workspace"
            + (" FOR UPDATE" if lock else ""),
            id=id,
            workspace=workspace_id,
        )
        .mappings()
        .first()
    )
    if not row or row["status"] in {"DELETING", "DELETED"}:
        raise HTTPException(404, "File not found")
    return row


@contextmanager
def upload_transaction(workspace_id, id):
    """Commit permanent storage expiry before returning an error to the client.

    Keep the file lock until expiry is committed: a concurrent completion or
    deletion must not race an expiry update from a stale snapshot. Only an
    explicit NoSuchUpload is terminal; outages and access errors remain retryable.
    """
    expired = False
    with engine().begin() as conn:
        file = get_file(conn, workspace_id, id, True)
        try:
            yield conn, file
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload" or file["status"] not in {
                "UPLOADING",
                "FINALIZING",
            }:
                raise
            query(
                conn,
                "UPDATE files SET status='EXPIRED',error=:error,updated_at=now() WHERE id=:id",
                id=id,
                error="Upload expired in storage. Select the original file to restart the upload.",
            )
            expired = True
    if expired:
        raise HTTPException(
            409,
            {
                "code": "upload_expired",
                "message": "Upload expired in storage. Select the original file to restart the upload.",
            },
        )


def describe(conn, file, parts=None):
    return {
        **{
            key: file[key]
            for key in (
                "id",
                "name",
                "size",
                "media_type",
                "status",
                "expected_sha256",
                "sha256",
                "result",
                "error",
                "created_at",
                "updated_at",
                "part_size",
            )
        },
        "part_count": part_count(file["size"], file["part_size"]),
        "parts": (
            parts
            if parts is not None
            else [
                dict(row)
                for row in query(
                    conn,
                    "SELECT number,checksum,etag FROM upload_parts WHERE file_id=:id ORDER BY number",
                    id=file["id"],
                ).mappings()
            ]
        ),
    }


def admission_lock(conn):
    # A single transaction lock makes workspace and global counts atomic across
    # API processes. It covers only new admissions, never an idempotent replay.
    query(
        conn,
        "SELECT set_config('lock_timeout',:timeout,true)",
        timeout=f"{int(settings().admission_lock_timeout_seconds * 1000)}ms",
    )
    query(conn, "SELECT pg_advisory_xact_lock(1987052404)")


def check_capacity(conn, workspace_id):
    counts = (
        query(
            conn,
            """SELECT count(*) AS total,
        count(*) FILTER (WHERE workspace_id=:workspace) AS workspace
        FROM files WHERE status IN ('UPLOADING','FINALIZING','PROCESSING')""",
            workspace=workspace_id,
        )
        .mappings()
        .one()
    )
    config = settings()
    if (
        counts["total"] >= config.max_active_uploads_global
        or counts["workspace"] >= config.max_active_uploads_per_workspace
    ):
        raise HTTPException(
            429,
            {
                "code": "upload_capacity",
                "message": "Upload capacity is full. Resume an existing upload or wait for a transfer to finish.",
            },
            headers={"Retry-After": "1"},
        )


def initiate(workspace_id, data):
    if data.size > settings().max_file_bytes:
        raise HTTPException(413, f"File exceeds the {settings().max_file_bytes} byte limit")
    id = uuid4()
    with engine().begin() as conn:
        existing = query(
            conn,
            "SELECT id FROM files WHERE workspace_id=:workspace AND request_id=:request FOR UPDATE",
            workspace=workspace_id,
            request=data.request_id,
        ).first()
        if not existing:
            admission_lock(conn)
            # Another initiator may have committed this same request while the
            # lock was awaited. Replays remain valid even at full capacity.
            existing = query(
                conn,
                "SELECT id FROM files WHERE workspace_id=:workspace AND request_id=:request FOR UPDATE",
                workspace=workspace_id,
                request=data.request_id,
            ).first()
            if not existing:
                check_capacity(conn, workspace_id)
        if not existing:
            query(
                conn,
                """INSERT INTO files(id,workspace_id,request_id,name,size,media_type,expected_sha256,part_size,object_key,status)
          VALUES (:id,:workspace,:request,:name,:size,:type,:hash,:part_size,:key,'UPLOADING')
          ON CONFLICT(workspace_id,request_id) DO NOTHING""",
                id=id,
                workspace=workspace_id,
                request=data.request_id,
                name=data.name,
                size=data.size,
                type=data.media_type,
                hash=data.sha256,
                part_size=settings().part_bytes,
                key=f"{workspace_id}/{id}",
            )
        file = (
            query(
                conn,
                "SELECT * FROM files WHERE workspace_id=:workspace AND request_id=:request FOR UPDATE",
                workspace=workspace_id,
                request=data.request_id,
            )
            .mappings()
            .one()
        )
        if any(
            file[key] != value
            for key, value in (
                ("name", data.name),
                ("size", data.size),
                ("media_type", data.media_type),
                ("expected_sha256", data.sha256),
            )
        ):
            raise HTTPException(409, "This request ID belongs to a different file")
        if file["status"] in {"DELETING", "DELETED"}:
            raise HTTPException(
                409,
                {
                    "code": "upload_deleted",
                    "message": "This upload was deleted; use a new request ID",
                },
            )
        if not file["upload_id"]:
            response = s3().create_multipart_upload(
                Bucket=settings().s3_bucket,
                Key=file["object_key"],
                ContentType="application/octet-stream",
                ChecksumAlgorithm="SHA256",
                Metadata={"file-id": str(file["id"])},
            )
            query(
                conn,
                "UPDATE files SET upload_id=:upload WHERE id=:id",
                upload=response["UploadId"],
                id=file["id"],
            )
        result = describe(conn, file)
    event("upload_initiated", file_id=str(file["id"]), replay=bool(existing))
    return result


def sign_part(workspace_id, id, number, checksum):
    with upload_transaction(workspace_id, id) as (conn, file):
        if file["status"] != "UPLOADING":
            raise HTTPException(409, "Upload is no longer accepting parts")
        try:
            size = part_length(file["size"], file["part_size"], number)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        stored = (
            query(
                conn,
                "SELECT * FROM upload_parts WHERE file_id=:id AND number=:number",
                id=id,
                number=number,
            )
            .mappings()
            .first()
        )
        if stored and stored["checksum"] != checksum:
            raise HTTPException(409, "Part contents differ from the original upload")
        # Also check new parts: signing a URL alone cannot detect expired storage.
        remote = remote_part(file, number)
        if not stored:
            query(
                conn,
                "INSERT INTO upload_parts(file_id,number,checksum) VALUES (:id,:number,:checksum)",
                id=id,
                number=number,
                checksum=checksum,
            )
        else:
            # A client can lose the acknowledgement after S3 accepts a PUT.
            if remote and remote["Size"] == size and remote.get("ChecksumSHA256") == checksum:
                query(
                    conn,
                    "UPDATE upload_parts SET etag=:etag WHERE file_id=:id AND number=:number",
                    etag=remote["ETag"],
                    id=id,
                    number=number,
                )
                return {"uploaded": True, "etag": remote["ETag"]}
        url = s3(True).generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings().s3_bucket,
                "Key": file["object_key"],
                "UploadId": file["upload_id"],
                "PartNumber": number,
                "ChecksumSHA256": checksum,
                "ContentLength": size,
            },
            ExpiresIn=settings().signed_url_seconds,
        )
        return {
            "uploaded": False,
            "url": url,
            "headers": {"x-amz-checksum-sha256": checksum},
            "size": size,
        }


def acknowledge(workspace_id, id, number, receipt):
    with upload_transaction(workspace_id, id) as (conn, file):
        if file["status"] != "UPLOADING":
            raise HTTPException(409, "Upload is no longer accepting parts")
        stored = (
            query(
                conn,
                "SELECT * FROM upload_parts WHERE file_id=:id AND number=:number",
                id=id,
                number=number,
            )
            .mappings()
            .first()
        )
        if not stored or stored["checksum"] != receipt.checksum:
            raise HTTPException(409, "Part must be signed before acknowledgement")
        remote = remote_part(file, number)
        if (
            not remote
            or remote["Size"] != part_length(file["size"], file["part_size"], number)
            or remote["ETag"] != receipt.etag
            or remote.get("ChecksumSHA256") != receipt.checksum
        ):
            raise HTTPException(409, "Storage has not confirmed this part and checksum")
        query(
            conn,
            "UPDATE upload_parts SET etag=:etag WHERE file_id=:id AND number=:number",
            id=id,
            number=number,
            etag=receipt.etag,
        )
        return {"number": number, "etag": receipt.etag}


def finalize(workspace_id, id):
    # Persist intent before crossing the PostgreSQL/S3 boundary. A retry can
    # recover a completed S3 upload even if the following DB transaction fails.
    with engine().begin() as conn:
        file = get_file(conn, workspace_id, id, True)
        if file["status"] in {"PROCESSING", "READY", "FAILED", "EXPIRED"}:
            return describe(conn, file)
        if file["status"] == "UPLOADING":
            count = query(
                conn,
                "SELECT count(*) FROM upload_parts WHERE file_id=:id AND etag IS NOT NULL",
                id=id,
            ).scalar_one()
            if count != part_count(file["size"], file["part_size"]):
                raise HTTPException(409, "Every part must be uploaded and acknowledged first")
            query(conn, "UPDATE files SET status='FINALIZING',updated_at=now() WHERE id=:id", id=id)
    checkpoint("finalizing_committed", id)
    with upload_transaction(workspace_id, id) as (conn, file):
        if file["status"] != "FINALIZING":
            return describe(conn, file)
        existing = head(file["object_key"])
        if not existing:
            manifest = [
                {"PartNumber": p["number"], "ETag": p["etag"], "ChecksumSHA256": p["checksum"]}
                for p in query(
                    conn, "SELECT * FROM upload_parts WHERE file_id=:id ORDER BY number", id=id
                ).mappings()
            ]
            try:
                s3().complete_multipart_upload(
                    Bucket=settings().s3_bucket,
                    Key=file["object_key"],
                    UploadId=file["upload_id"],
                    MultipartUpload={"Parts": manifest},
                )
                checkpoint("storage_completed", id)
            except ClientError as exc:
                if not missing(exc) or not head(file["object_key"]):
                    raise
            existing = head(file["object_key"])
        if (
            not existing
            or existing["ContentLength"] != file["size"]
            or existing.get("Metadata", {}).get("file-id") != str(id)
        ):
            raise HTTPException(409, "Stored object does not match the upload")
        query(
            conn,
            "UPDATE files SET status='PROCESSING',updated_at=now(),error=NULL WHERE id=:id",
            id=id,
        )
        query(
            conn,
            "INSERT INTO processing_outbox(file_id) VALUES (:id) ON CONFLICT DO NOTHING",
            id=id,
        )
        checkpoint("outbox_before_commit", id)
        return describe(conn, get_file(conn, workspace_id, id))


def delete_file(workspace_id, id):
    # Commit intent and revoke worker claims BEFORE crossing the storage boundary.
    # A crash after any storage effect leaves recoverable DELETING metadata.
    with engine().begin() as conn:
        # Repeating DELETE is safe, including a lost response from an earlier call.
        file = (
            query(
                conn,
                "SELECT * FROM files WHERE id=:id AND workspace_id=:workspace FOR UPDATE",
                id=id,
                workspace=workspace_id,
            )
            .mappings()
            .first()
        )
        if not file:
            raise HTTPException(404, "File not found")
        if file["status"] == "DELETED":
            return
        if file["status"] != "DELETING":
            query(
                conn,
                "UPDATE files SET status='DELETING',claim=NULL,lease_until=NULL,error=NULL,updated_at=now() WHERE id=:id",
                id=id,
            )
        query(conn, "DELETE FROM processing_outbox WHERE file_id=:id", id=id)
    finish_delete(workspace_id, id)


def finish_delete(workspace_id, id):
    with engine().begin() as conn:
        file = (
            query(
                conn,
                "SELECT * FROM files WHERE id=:id AND workspace_id=:workspace FOR UPDATE",
                id=id,
                workspace=workspace_id,
            )
            .mappings()
            .first()
        )
        if not file or file["status"] != "DELETING":
            return
        if file["upload_id"]:
            try:
                s3().abort_multipart_upload(
                    Bucket=settings().s3_bucket, Key=file["object_key"], UploadId=file["upload_id"]
                )
            except ClientError as exc:
                if not missing(exc):
                    raise
        try:
            s3().delete_object(Bucket=settings().s3_bucket, Key=file["object_key"])
        except ClientError as exc:
            if not missing(exc):
                raise
        query(
            conn,
            "UPDATE files SET status='DELETED',claim=NULL,lease_until=NULL,updated_at=now() WHERE id=:id",
            id=id,
        )
        query(conn, "DELETE FROM processing_outbox WHERE file_id=:id", id=id)


def retry_processing(workspace_id, id):
    with engine().begin() as conn:
        admission_lock(conn)
        file = get_file(conn, workspace_id, id, True)
        if file["status"] != "FAILED":
            raise HTTPException(409, "Only failed processing can be retried")
        check_capacity(conn, workspace_id)
        query(
            conn,
            "UPDATE files SET status='PROCESSING',claim=NULL,lease_until=NULL,error=NULL,updated_at=now() WHERE id=:id",
            id=id,
        )
        query(
            conn,
            "INSERT INTO processing_outbox(file_id) VALUES (:id) ON CONFLICT DO NOTHING",
            id=id,
        )
        return describe(conn, get_file(conn, workspace_id, id))


def download(workspace_id, id):
    with engine().connect() as conn:
        file = get_file(conn, workspace_id, id)
        if file["status"] != "READY":
            raise HTTPException(409, "File must pass verification before download")
        disposition = "attachment; filename=\"download\"; filename*=UTF-8''" + quote(
            file["name"], safe=""
        )
        url = s3(True).generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings().s3_bucket,
                "Key": file["object_key"],
                "ResponseContentDisposition": disposition,
                "ResponseContentType": "application/octet-stream",
            },
            ExpiresIn=60,
        )
        return {"url": url, "expires_in": 60}
