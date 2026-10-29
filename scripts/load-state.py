"""Secret-free, read-only snapshots of the dedicated load-test database/storage."""

import json
import sys
from uuid import UUID
from urllib.parse import urlsplit

from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url

from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.storage import s3


def snapshot(command):
    config = settings()
    db = make_url(config.database_url)
    if (
        db.database != "clouddrive_load_test"
        or db.host not in {"localhost", "127.0.0.1"}
        or db.port != 15439
        or config.s3_bucket != "clouddrive-load-test"
        or urlsplit(config.s3_endpoint).hostname not in {"localhost", "127.0.0.1"}
        or urlsplit(config.s3_endpoint).port != 19014
    ):
        raise RuntimeError("Load observer requires the dedicated loopback test database/bucket")
    broker = urlsplit(config.redis_url)
    if (
        broker.hostname not in {"localhost", "127.0.0.1"}
        or broker.port != 16386
        or broker.path != "/0"
    ):
        raise RuntimeError("Load observer requires its dedicated loopback Redis")
    client = Redis.from_url(config.redis_url, socket_timeout=3)
    try:
        result = {
            "broker": {
                "scope": "Dedicated Redis database 0; default Celery queue, priority lists and Kombu unacked hash/index",
                "queued_messages": sum(
                    client.llen(k)
                    for k in [b"celery", b"celery\x06\x163", b"celery\x06\x166", b"celery\x06\x169"]
                ),
                "unacked_messages": client.hlen("unacked"),
                "unacked_index": client.zcard("unacked_index"),
            }
        }
    finally:
        client.close()
    with engine().begin() as conn:
        conn.exec_driver_sql("SET application_name='clouddrive-load-observer'")
        result["connections"] = dict(conn.execute(text("""SELECT count(*) AS total,
                    count(*) FILTER (WHERE state='active') AS active,
                    count(*) FILTER (WHERE wait_event_type='Lock') AS lock_waiting,
                    count(*) FILTER (WHERE state='idle in transaction') AS idle_in_transaction
                    FROM pg_stat_activity WHERE datname=current_database()""")).mappings().one())
        result["ungranted_locks"] = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
            )
        ).scalar_one()
        result["database"] = dict(
            conn.execute(
                text(
                    "SELECT xact_commit,xact_rollback,deadlocks,temp_bytes,blks_read,blks_hit FROM pg_stat_database WHERE datname=current_database()"
                )
            )
            .mappings()
            .one()
        )
        result["work"] = dict(
            conn.execute(text("""SELECT count(*) FILTER (WHERE status='PROCESSING') AS processing,
                count(*) FILTER (WHERE claim IS NOT NULL AND lease_until>now()) AS active_claims,
                count(*) FILTER (WHERE claim IS NOT NULL AND lease_until<=now()) AS expired_claims
                FROM files""")).mappings().one()
        )
        result["outbox"] = dict(
            conn.execute(
                text(
                    "SELECT count(*) AS count,coalesce(extract(epoch FROM now()-min(created_at)),0)::float AS oldest_age_seconds FROM processing_outbox"
                )
            )
            .mappings()
            .one()
        )
        if command.get("workspace"):
            ws = UUID(command["workspace"])
            rows = (
                conn.execute(
                    text(
                        "SELECT id,name,size,status,sha256,result,claim FROM files WHERE workspace_id=:ws ORDER BY name"
                    ),
                    {"ws": ws},
                )
                .mappings()
                .all()
            )
            result["files"] = [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "size": r["size"],
                    "status": r["status"],
                    "sha256": r["sha256"],
                    "bytes_verified": (r["result"] or {}).get("bytes_verified"),
                    "result_count": int(r["result"] is not None),
                    "claim_active": r["claim"] is not None,
                }
                for r in rows
            ]
    if command.get("inventory"):
        prefix = str(UUID(command["workspace"])) + "/" if command.get("workspace") else ""
        objects = [
            obj
            for page in s3()
            .get_paginator("list_objects_v2")
            .paginate(Bucket=config.s3_bucket, Prefix=prefix)
            for obj in page.get("Contents", [])
        ]
        uploads = [
            u
            for page in s3()
            .get_paginator("list_multipart_uploads")
            .paginate(Bucket=config.s3_bucket, Prefix=prefix)
            for u in page.get("Uploads", [])
        ]
        result.update(
            object_count=len(objects),
            object_bytes=sum(o["Size"] for o in objects),
            multipart_count=len(uploads),
        )
    return result


try:
    for line in sys.stdin:
        try:
            print(json.dumps(snapshot(json.loads(line))), flush=True)
        except Exception as exc:
            print(json.dumps({"error_type": type(exc).__name__}), flush=True)
finally:
    engine().dispose()
