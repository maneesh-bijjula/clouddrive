import os

import pytest

os.environ.setdefault(
    "CLOUDDRIVE_DATABASE_URL",
    "postgresql+psycopg://clouddrive:clouddrive@localhost:5436/clouddrive_test",
)
os.environ.setdefault("CLOUDDRIVE_REDIS_URL", "redis://localhost:6383/0")
os.environ.setdefault("CLOUDDRIVE_S3_ENDPOINT", "http://localhost:9010")
os.environ.setdefault("CLOUDDRIVE_S3_PUBLIC_ENDPOINT", "http://localhost:9010")
os.environ.setdefault("CLOUDDRIVE_S3_BUCKET", "clouddrive-test")


def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", help="Run isolated infrastructure tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--integration"):
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="Pass --integration with test services running")
                )


@pytest.fixture
def client():
    from sqlalchemy.engine import make_url
    from fastapi.testclient import TestClient
    from clouddrive.config import settings
    from clouddrive.db import engine, migrate
    from clouddrive.service import query
    from clouddrive.storage import s3
    from clouddrive.api import app

    if not (make_url(settings().database_url).database or "").endswith(
        "_test"
    ) or not settings().s3_bucket.endswith("-test"):
        pytest.fail("Integration cleanup requires a database ending _test and bucket ending -test")
    migrate()
    with engine().begin() as conn:
        query(conn, "TRUNCATE workspaces,files,upload_parts,processing_outbox CASCADE")
    with TestClient(app) as client:
        yield client
    bucket = settings().s3_bucket
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket):
        if page.get("Contents"):
            s3().delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": item["Key"]} for item in page["Contents"]]},
            )
    for upload in s3().list_multipart_uploads(Bucket=bucket).get("Uploads", []):
        s3().abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])


@pytest.fixture
def auth(client):
    response = client.post("/api/workspaces")
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["access_key"]}
