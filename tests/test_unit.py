import base64
import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from clouddrive.models import NewUpload, PartChecksum, part_count, part_length


@pytest.mark.parametrize(
    "size,expected", [(1, 1), (8 * 1024**2, 1), (8 * 1024**2 + 1, 2), (256 * 1024**2, 32)]
)
def test_part_count(size, expected):
    assert part_count(size, 8 * 1024**2) == expected


@pytest.mark.parametrize(
    "size,number,expected", [(1, 1, 1), (16 * 1024**2, 2, 8 * 1024**2), (8 * 1024**2 + 17, 2, 17)]
)
def test_part_length(size, number, expected):
    assert part_length(size, 8 * 1024**2, number) == expected


@pytest.mark.parametrize("number", [-1, 0, 3])
def test_part_out_of_range(number):
    with pytest.raises(ValueError):
        part_length(9 * 1024**2, 8 * 1024**2, number)


@pytest.mark.parametrize(
    "name", ["", " ", "../secret", "path\\file", "line\nbreak", "null\x00byte", "a" * 221]
)
def test_reject_unsafe_filename(name):
    with pytest.raises(ValidationError):
        NewUpload(request_id=uuid4(), name=name, size=1, sha256="0" * 64)


def test_unicode_filename_and_untrusted_media_type():
    upload = NewUpload(
        request_id=uuid4(),
        name="résumé 🗂.txt",
        size=1,
        sha256="0" * 64,
        media_type="text/html\r\nInjected: yes",
    )
    assert upload.name == "résumé 🗂.txt"
    assert upload.media_type == "application/octet-stream"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0000",
        "!" * 44,
        base64.b64encode(b"a" * 31).decode(),
        base64.b64encode(b"a" * 33).decode(),
    ],
)
def test_reject_invalid_part_checksum(value):
    with pytest.raises(ValidationError):
        PartChecksum(checksum=value)


def test_sha256_checksum():
    value = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    assert PartChecksum(checksum=value).checksum == value


@pytest.mark.parametrize("value", ["A" * 64, "0" * 63, "z" * 64])
def test_full_hash_validation(value):
    with pytest.raises(ValidationError):
        NewUpload(request_id=uuid4(), name="test.txt", size=1, sha256=value)
