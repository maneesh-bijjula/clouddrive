"""Read only, secret-free invariants for a disposable recovery workspace."""

import argparse
import json
from uuid import UUID

from botocore.exceptions import ClientError
from sqlalchemy.engine import make_url

from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.service import query
from clouddrive.storage import s3


def inspect_file(id):
    config = settings()
    if not (make_url(config.database_url).database or "").endswith(
        "_test"
    ) or not config.s3_bucket.endswith("-test"):
        raise RuntimeError("Recovery inspection requires an isolated test database/bucket")
    with engine().connect() as conn:
        file = query(conn, "SELECT * FROM files WHERE id=:id", id=id).mappings().one()
        metadata_count = query(
            conn, "SELECT count(*) FROM files WHERE workspace_id=:ws", ws=file["workspace_id"]
        ).scalar_one()
        confirmed = (
            query(
                conn,
                "SELECT number FROM upload_parts WHERE file_id=:id AND etag IS NOT NULL ORDER BY number",
                id=id,
            )
            .scalars()
            .all()
        )
        outbox = query(
            conn, "SELECT count(*) FROM processing_outbox WHERE file_id=:id", id=id
        ).scalar_one()
    prefix = str(file["workspace_id"]) + "/"
    objects = [
        obj
        for page in s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=config.s3_bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]
    uploads = [
        upload
        for page in s3()
        .get_paginator("list_multipart_uploads")
        .paginate(Bucket=config.s3_bucket, Prefix=prefix)
        for upload in page.get("Uploads", [])
    ]
    remote = []
    if file["upload_id"]:
        try:
            remote = [
                {"number": part["PartNumber"], "bytes": part["Size"]}
                for page in s3()
                .get_paginator("list_parts")
                .paginate(
                    Bucket=config.s3_bucket, Key=file["object_key"], UploadId=file["upload_id"]
                )
                for part in page.get("Parts", [])
            ]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload":
                raise
    return {
        "file_id": str(file["id"]),
        "workspace_id": str(file["workspace_id"]),
        "status": file["status"],
        "size": file["size"],
        "sha256": file["sha256"],
        "bytes_verified": (file["result"] or {}).get("bytes_verified"),
        "metadata_count": metadata_count,
        "object_count": len(objects),
        "object_bytes": sum(o["Size"] for o in objects),
        "multipart_count": len(uploads),
        "result_count": int(file["result"] is not None),
        "outbox_count": outbox,
        "claim_active": file["claim"] is not None,
        "confirmed_parts": confirmed,
        "remote_parts": [part["number"] for part in remote],
        "remote_part_sizes": remote,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=UUID, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect_file(args.file)))
