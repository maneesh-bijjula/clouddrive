"""Abort one multipart session for browser expiry tests; refuse non-test storage."""

import argparse
import os
from uuid import UUID

# Match the browser runner's isolated defaults. Explicit overrides support the
# separate packaged test project; the safety guard still applies to overrides.
os.environ.setdefault(
    "CLOUDDRIVE_DATABASE_URL",
    "postgresql+psycopg://clouddrive:clouddrive@localhost:5436/clouddrive_test",
)
os.environ.setdefault("CLOUDDRIVE_S3_ENDPOINT", "http://localhost:9010")
os.environ.setdefault("CLOUDDRIVE_S3_BUCKET", "clouddrive-test")

from sqlalchemy.engine import make_url

from clouddrive.config import settings
from clouddrive.db import engine
from clouddrive.service import query
from clouddrive.storage import s3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", type=UUID)
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
        s3().abort_multipart_upload(
            Bucket=config.s3_bucket, Key=row["object_key"], UploadId=row["upload_id"]
        )
    print("Test multipart session aborted")


if __name__ == "__main__":
    main()
