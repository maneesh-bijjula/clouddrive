"""Exercise multipart or signed-URL expiry; refuse non-test storage."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

# Match the browser runner's isolated defaults. Explicit overrides support the
# separate packaged test project; the safety guard still applies to overrides.
os.environ.setdefault(
    "CLOUDDRIVE_DATABASE_URL",
    "postgresql+psycopg://clouddrive:clouddrive@localhost:5436/clouddrive_test",
)
os.environ.setdefault("CLOUDDRIVE_S3_ENDPOINT", "http://localhost:9010")
os.environ.setdefault("CLOUDDRIVE_S3_BUCKET", "clouddrive-test")
os.environ.setdefault("CLOUDDRIVE_S3_PUBLIC_ENDPOINT", os.environ["CLOUDDRIVE_S3_ENDPOINT"])

from sqlalchemy.engine import make_url

from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.models import PartChecksum, part_length
from clouddrive.service import query
from clouddrive.storage import s3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", type=UUID)
    parser.add_argument("--expired-part", type=int)
    parser.add_argument("--checksum")
    args = parser.parse_args()
    config = settings()
    if not (make_url(config.database_url).database or "").endswith(
        "_test"
    ) or not config.s3_bucket.endswith("-test"):
        raise SystemExit("Refusing to abort an upload outside an isolated test database/bucket")
    with engine().begin() as conn:
        row = (
            query(conn, "SELECT * FROM files WHERE id=:id FOR UPDATE", id=args.id).mappings().one()
        )
        if row["status"] != "UPLOADING":
            raise SystemExit("Only an incomplete test upload can be expired")
        if args.expired_part is not None:
            checksum = PartChecksum(checksum=args.checksum).checksum
            length = part_length(row["size"], row["part_size"], args.expired_part)
            # A genuinely expired signature, not an invalid signature or a
            # fabricated storage response. Never alter the host/provider clock.
            past = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
            with patch("botocore.auth.get_current_datetime", return_value=past):
                url = s3(True).generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": config.s3_bucket,
                        "Key": row["object_key"],
                        "UploadId": row["upload_id"],
                        "PartNumber": args.expired_part,
                        "ChecksumSHA256": checksum,
                        "ContentLength": length,
                    },
                    ExpiresIn=60,
                )
            print(json.dumps({"url": url}))
            return
        s3().abort_multipart_upload(
            Bucket=config.s3_bucket, Key=row["object_key"], UploadId=row["upload_id"]
        )
    print("Test multipart session aborted")


if __name__ == "__main__":
    main()
