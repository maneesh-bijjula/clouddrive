from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import settings


@lru_cache
def s3(public=False):
    config = settings()
    return boto3.client(
        "s3",
        endpoint_url=config.s3_public_endpoint if public else config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
        region_name=config.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


def missing(exc: ClientError):
    return exc.response["Error"]["Code"] in {
        "404",
        "NoSuchKey",
        "NoSuchUpload",
        "NotFound",
        "NoSuchBucket",
    }


def bootstrap():
    bucket = settings().s3_bucket
    try:
        s3().head_bucket(Bucket=bucket)
    except ClientError as exc:
        if not missing(exc):
            raise
        params = {"Bucket": bucket}
        if settings().s3_region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": settings().s3_region}
        s3().create_bucket(**params)


def head(key):
    try:
        return s3().head_object(Bucket=settings().s3_bucket, Key=key)
    except ClientError as exc:
        if missing(exc):
            return None
        raise


def remote_part(file, number):
    response = s3().list_parts(
        Bucket=settings().s3_bucket,
        Key=file["object_key"],
        UploadId=file["upload_id"],
        PartNumberMarker=number - 1,
        MaxParts=1,
    )
    return next((part for part in response.get("Parts", []) if part["PartNumber"] == number), None)
