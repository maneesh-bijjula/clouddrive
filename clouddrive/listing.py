"""Workspace-bound keyset cursors and compact file summaries."""

import base64
import hashlib
import json
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException

from .db import engine
from .service import describe, query


def decode_cursor(value, workspace_id, filters):
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if data["workspace"] != str(workspace_id) or data["filters"] != filters:
            raise ValueError()
        stamp = datetime.fromisoformat(data["created_at"])
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp, UUID(data["id"])
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(400, "Invalid cursor for this workspace and filter") from None


def list_files(workspace_id, limit=50, cursor=None, search="", view="all", compact=True):
    # Keep cursors bounded even for 240-character non-ASCII search strings.
    filters = hashlib.sha256(json.dumps({"search": search, "view": view}).encode()).hexdigest()
    params = {"workspace": workspace_id, "limit": limit + 1}
    conditions = ["workspace_id=:workspace", "status NOT IN ('DELETING','DELETED')"]
    if search:
        conditions.append("strpos(lower(name),lower(:search)) > 0")
        params["search"] = search
    if view == "uploads":
        conditions.append("status <> 'READY'")
    elif view == "recent":
        conditions.append("created_at >= now()-interval '7 days'")
    if cursor:
        params["stamp"], params["id"] = decode_cursor(cursor, workspace_id, filters)
        conditions.append("(created_at,id) < (:stamp,:id)")
    with engine().connect() as conn:
        rows = (
            query(
                conn,
                "SELECT * FROM files WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC,id DESC LIMIT :limit",
                **params,
            )
            .mappings()
            .all()
        )
        more = len(rows) > limit
        rows = rows[:limit]
        stats = dict(
            query(
                conn,
                """SELECT count(*) AS total,
            count(*) FILTER (WHERE status='READY') AS ready,
            coalesce(sum(size) FILTER (WHERE status='READY'),0) AS bytes,
            count(*) FILTER (WHERE status IN ('UPLOADING','FINALIZING','PROCESSING')) AS pending
            FROM files WHERE workspace_id=:workspace AND status NOT IN ('DELETING','DELETED')""",
                workspace=workspace_id,
            )
            .mappings()
            .one()
        )
        parts = {}
        if rows and not compact:
            for part in query(
                conn,
                "SELECT file_id,number,checksum,etag FROM upload_parts WHERE file_id=ANY(:ids) ORDER BY file_id,number",
                ids=[row["id"] for row in rows],
            ).mappings():
                parts.setdefault(part["file_id"], []).append(
                    {key: part[key] for key in ("number", "checksum", "etag")}
                )
        confirmed = {}
        if rows and compact:
            confirmed = dict(
                query(
                    conn,
                    "SELECT file_id,count(*) FILTER (WHERE etag IS NOT NULL) FROM upload_parts WHERE file_id=ANY(:ids) GROUP BY file_id",
                    ids=[row["id"] for row in rows],
                ).all()
            )
        files = []
        for row in rows:
            result = describe(conn, row, parts.get(row["id"], []))
            if compact:
                result.pop("parts")
                result["confirmed_parts"] = confirmed.get(row["id"], 0)
                result["result"] = None
            files.append(result)
        next_cursor = None
        if more:
            row = rows[-1]
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "workspace": str(workspace_id),
                            "filters": filters,
                            "created_at": row["created_at"].isoformat(),
                            "id": str(row["id"]),
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return {
            "files": files,
            "stats": stats,
            "limit": limit,
            "next_cursor": next_cursor,
            "compact": compact,
        }
