"""Exercise durable processing while Redis is stopped, then verify recovery.

Run `stage` with the broker stopped and `verify` after restarting it.
The state file contains a secret workspace key; it is created with mode 0600.
"""

import argparse
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["stage", "verify"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--base-url", default="http://localhost:8090")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        if args.mode == "stage":
            workspace = client.post("/api/workspaces")
            workspace.raise_for_status()
            key = workspace.json()["access_key"]
            client.headers["Authorization"] = "Bearer " + key
            content = (b"CloudDrive broker recovery\n" * 700000) + b"final bytes"
            digest = hashlib.sha256(content).hexdigest()
            response = client.post(
                "/api/uploads",
                json={
                    "request_id": str(uuid4()),
                    "name": "broker-recovery.txt",
                    "size": len(content),
                    "sha256": digest,
                },
            )
            response.raise_for_status()
            upload = response.json()
            id = upload["id"]
            for number in range(1, upload["part_count"] + 1):
                chunk = content[(number - 1) * upload["part_size"] : number * upload["part_size"]]
                checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode()
                path = f"/api/uploads/{id}/parts/{number}"
                response = client.post(path + "/sign", json={"checksum": checksum})
                response.raise_for_status()
                signed = response.json()
                put = httpx.put(signed["url"], headers=signed["headers"], content=chunk, timeout=30)
                put.raise_for_status()
                ack = client.put(path, json={"checksum": checksum, "etag": put.headers["ETag"]})
                ack.raise_for_status()
            complete = client.post(f"/api/uploads/{id}/complete")
            complete.raise_for_status()
            assert complete.json()["status"] == "PROCESSING"
            assert client.get(f"/api/files/{id}/download").status_code == 409
            descriptor = os.open(args.state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(
                    {"id": id, "access_key": key, "sha256": digest, "size": len(content)}, output
                )
            print(
                f"PASS: {len(content)} bytes uploaded while broker is unavailable; download remains blocked."
            )
        else:
            state = json.loads(args.state.read_text())
            client.headers["Authorization"] = "Bearer " + state["access_key"]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                response = client.get(f"/api/uploads/{state['id']}")
                response.raise_for_status()
                file = response.json()
                if file["status"] == "READY":
                    break
                assert file["status"] != "FAILED", file["error"]
                time.sleep(1)
            else:
                raise AssertionError("Processing did not recover within 60 seconds")
            signed = client.get(f"/api/files/{state['id']}/download")
            signed.raise_for_status()
            response = httpx.get(signed.json()["url"], timeout=30)
            response.raise_for_status()
            assert len(response.content) == state["size"]
            assert hashlib.sha256(response.content).hexdigest() == state["sha256"]
            client.delete(f"/api/files/{state['id']}").raise_for_status()
            args.state.unlink()
            print(
                "PASS: worker recovered after broker restart; downloaded bytes and SHA-256 match; test file deleted."
            )


if __name__ == "__main__":
    main()
