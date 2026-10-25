"""Real-service upload helpers shared by integration suites."""

import base64
import hashlib
from uuid import uuid4
import httpx


def begin(client, auth, content=b"Hello, CloudDrive!\n", **changes):
    data = {
        "request_id": str(uuid4()),
        "name": "notes.txt",
        "size": len(content),
        "media_type": "text/plain",
        "sha256": hashlib.sha256(content).hexdigest(),
        **changes,
    }
    response = client.post("/api/uploads", headers=auth, json=data)
    assert response.status_code == 201, response.text
    return response.json(), data


def send_part(client, auth, upload, number, chunk, acknowledge=True):
    checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode()
    path = f"/api/uploads/{upload['id']}/parts/{number}"
    signed = client.post(path + "/sign", headers=auth, json={"checksum": checksum})
    assert signed.status_code == 200, signed.text
    signed = signed.json()
    if signed["uploaded"]:
        return checksum, signed["etag"]
    response = httpx.put(signed["url"], content=chunk, headers=signed["headers"], timeout=30)
    assert response.is_success, response.text
    etag = response.headers["ETag"]
    if acknowledge:
        receipt = client.put(path, headers=auth, json={"checksum": checksum, "etag": etag})
        assert receipt.status_code == 200, receipt.text
    return checksum, etag


def uploaded(client, auth, content=b"Hello, CloudDrive!\n", **changes):
    upload, data = begin(client, auth, content, **changes)
    for index in range(upload["part_count"]):
        send_part(
            client,
            auth,
            upload,
            index + 1,
            content[index * upload["part_size"] : (index + 1) * upload["part_size"]],
        )
    result = client.post(f"/api/uploads/{upload['id']}/complete", headers=auth)
    assert result.status_code == 202, result.text
    return result.json(), data
