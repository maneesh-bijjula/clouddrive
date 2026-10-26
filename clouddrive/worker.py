import codecs
import hashlib
import json
import logging
import time
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from celery import Celery
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .db import engine, migrate
from .service import finalize, finish_delete, query
from .storage import s3

logger = logging.getLogger(__name__)
celery = Celery("clouddrive", broker=settings().redis_url)
celery.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 300},
    task_publish_retry=False,
    beat_schedule={"dispatch-durable-work": {"task": "clouddrive.dispatch", "schedule": 5.0}},
    timezone="UTC",
)


class ClaimLost(Exception):
    pass


def process_file(id):
    """Stream and verify bytes, using a renewable claim to fence stale workers."""
    claim = uuid4()
    with engine().begin() as conn:
        file = (
            query(
                conn,
                "SELECT *,lease_until > now() AS active FROM files WHERE id=:id FOR UPDATE",
                id=id,
            )
            .mappings()
            .first()
        )
        if not file or file["status"] != "PROCESSING" or file["active"]:
            return "skipped"
        query(
            conn,
            "UPDATE files SET claim=:claim,lease_until=now()+interval '90 seconds',updated_at=now() WHERE id=:id",
            id=id,
            claim=claim,
        )
    verification_started = time.monotonic()
    try:
        response = s3().get_object(Bucket=settings().s3_bucket, Key=file["object_key"])
        digest = hashlib.sha256()
        size = 0
        preview = bytearray()
        last_heartbeat = time.monotonic()
        try:
            for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if len(preview) < 65536:
                    preview.extend(chunk[: 65536 - len(preview)])
                if time.monotonic() - last_heartbeat > 10:
                    with engine().begin() as conn:
                        result = query(
                            conn,
                            "UPDATE files SET lease_until=now()+interval '90 seconds' WHERE id=:id AND claim=:claim AND status='PROCESSING'",
                            id=id,
                            claim=claim,
                        )
                        if result.rowcount != 1:
                            raise ClaimLost()
                    last_heartbeat = time.monotonic()
        finally:
            response["Body"].close()
        sha256 = digest.hexdigest()
        matches = size == file["size"] and sha256 == file["expected_sha256"]
        result = {
            "bytes_verified": size,
            "checksum_algorithm": "SHA-256",
            "stream_verification_seconds": time.monotonic() - verification_started,
            "preview": None,
        }
        # Preview only valid UTF-8 text, never HTML rendered as markup.
        if b"\x00" not in preview:
            try:
                decoded = codecs.getincrementaldecoder("utf-8")().decode(
                    bytes(preview), final=size <= len(preview)
                )
                result["preview"] = decoded[:2000]
                result["preview_truncated"] = size > len(preview) or len(decoded) > 2000
            except UnicodeDecodeError:
                pass
        with engine().begin() as conn:
            changed = query(
                conn,
                """UPDATE files SET status=:status,sha256=:sha,result=CAST(:result AS jsonb),error=:error,
                claim=NULL,lease_until=NULL,updated_at=now() WHERE id=:id AND claim=:claim AND status='PROCESSING'""",
                id=id,
                claim=claim,
                status="READY" if matches else "FAILED",
                sha=sha256,
                result=json.dumps(result) if matches else None,
                error=(
                    None
                    if matches
                    else "File integrity check failed: size or SHA-256 differs from the upload."
                ),
            )
            return ("ready" if matches else "failed") if changed.rowcount else "skipped"
    except ClaimLost:
        return "skipped"
    except Exception:
        with engine().begin() as conn:
            query(
                conn,
                "UPDATE files SET claim=NULL,lease_until=NULL WHERE id=:id AND claim=:claim",
                id=id,
                claim=claim,
            )
        raise


@celery.task(name="clouddrive.process", bind=True, max_retries=5)
def process(self, id):
    try:
        return process_file(UUID(id))
    except (ClientError, BotoCoreError, SQLAlchemyError, OSError) as exc:
        if self.request.retries >= self.max_retries:
            with engine().begin() as conn:
                query(
                    conn,
                    """UPDATE files SET status='FAILED',error='Processing could not reach storage. You can retry.',updated_at=now()
                  WHERE id=:id AND status='PROCESSING' AND claim IS NULL""",
                    id=UUID(id),
                )
            return "failed"
        raise self.retry(exc=exc, countdown=min(2**self.request.retries, 30))


def dispatch_pending(publish=None):
    publish = publish or (lambda id: process.delay(str(id)))
    with engine().begin() as conn:
        # Recover work whose broker message was lost, or whose worker died.
        query(
            conn,
            """INSERT INTO processing_outbox(file_id)
          SELECT id FROM files WHERE status='PROCESSING' AND
          ((claim IS NULL AND updated_at < now()-interval '30 seconds') OR lease_until < now())
          ON CONFLICT DO NOTHING""",
        )
        rows = (
            query(
                conn,
                "SELECT file_id FROM processing_outbox ORDER BY created_at LIMIT 20 FOR UPDATE SKIP LOCKED",
            )
            .scalars()
            .all()
        )
        for id in rows:
            publish(id)
            query(conn, "DELETE FROM processing_outbox WHERE file_id=:id", id=id)
    return len(rows)


def reconcile_files():
    # Separate budgets prevent failing deletions from starving finalizations.
    # Each file has its own transaction, so one failure cannot roll back others.
    for status, recover in (("DELETING", finish_delete), ("FINALIZING", finalize)):
        with engine().connect() as conn:
            rows = (
                query(
                    conn,
                    """SELECT id,workspace_id FROM files WHERE status=:status
                AND updated_at < now()-interval '30 seconds'
                ORDER BY updated_at,id LIMIT 20""",
                    status=status,
                )
                .mappings()
                .all()
            )
        for row in rows:
            try:
                recover(row["workspace_id"], row["id"])
            except Exception:
                logger.exception("%s recovery failed for %s", status, row["id"])


@celery.task(name="clouddrive.dispatch")
def dispatch():
    # Storage reconciliation does not depend on successful outbox publication.
    reconcile_files()
    count = dispatch_pending()
    return count


if __name__ == "__main__":
    migrate()
