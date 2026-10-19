import base64
import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NewUpload(BaseModel):
    request_id: UUID
    name: str = Field(min_length=1, max_length=220)
    size: int = Field(gt=0)
    media_type: str = Field(default="application/octet-stream", max_length=150)
    sha256: str

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if (
            not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or "/" in value
            or "\\" in value
        ):
            raise ValueError("Use a filename without paths or control characters")
        return value

    @field_validator("sha256")
    @classmethod
    def hex_hash(cls, value):
        if not re.fullmatch("[0-9a-f]{64}", value):
            raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value):
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", value):
            return "application/octet-stream"
        return value


class PartChecksum(BaseModel):
    checksum: str

    @field_validator("checksum")
    @classmethod
    def checksum_hash(cls, value):
        try:
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 32 or base64.b64encode(decoded).decode() != value:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Expected a base64 SHA-256 checksum")
        return value


class PartReceipt(PartChecksum):
    etag: str = Field(min_length=1, max_length=150)


def part_count(size, part_size):
    return (size + part_size - 1) // part_size


def part_length(size, part_size, number):
    if number < 1 or number > part_count(size, part_size):
        raise ValueError("Part number is outside the file")
    return min(part_size, size - (number - 1) * part_size)
