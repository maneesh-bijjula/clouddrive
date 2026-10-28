"""Opt-in, local-file synchronization for the isolated recovery test profile.

There is no network control endpoint. Normal deployments do not set the control
directory, so checkpoints immediately return without doing filesystem I/O.
"""

import json
import os
import time
from pathlib import Path
from uuid import UUID

from botocore.exceptions import EndpointConnectionError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from .config import settings


def checkpoint(name, file_id, **details):
    control = os.environ.get("CLOUDDRIVE_RECOVERY_CONTROL_DIR")
    if not control:
        return
    config = settings()
    if not (make_url(config.database_url).database or "").endswith(
        "_test"
    ) or not config.s3_bucket.endswith("-test"):
        raise RuntimeError("Recovery checkpoints require an isolated test database and bucket")
    file_id = str(UUID(str(file_id)))
    if not name.replace("_", "").isalnum():
        raise ValueError("Invalid recovery checkpoint")
    connection = details.pop("connection", None)
    if connection is not None:
        # Establish the final-result transaction before pausing it. The query
        # only executes in the explicitly enabled test profile.
        details["backend_pid"] = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
    directory = Path(control)
    event = {
        "checkpoint": name,
        "file_id": file_id,
        "pid": os.getpid(),
        "time_unix": time.time(),
        **{
            key: value
            for key, value in details.items()
            if key
            in {
                "bytes_read",
                "error_type",
                "backend_pid",
                "claim",
                "outcome",
                "retries",
                "redelivered",
                "task_id",
            }
            and isinstance(value, (str, int, float, bool))
        },
    }
    # One short O_APPEND write preserves event boundaries across API/worker
    # processes. These records never contain credentials, source data or URLs.
    fd = os.open(
        directory / f"events-{file_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666
    )
    try:
        os.write(fd, (json.dumps(event) + "\n").encode())
    finally:
        os.close(fd)
    arm = directory / f"arm-{name}-{file_id}.json"
    claimed = directory / f"claimed-{name}-{file_id}.json"
    try:
        arm.rename(claimed)
    except FileNotFoundError:
        return
    command = json.loads(claimed.read_text())
    marker = directory / f"reached-{name}-{file_id}.json"
    temporary = directory / f".{name}-{file_id}-{os.getpid()}.tmp"
    temporary.write_text(json.dumps(event))
    temporary.replace(marker)
    mode = command.get("mode", "pause")
    if mode == "raise_storage":
        raise EndpointConnectionError(endpoint_url="recovery-test-storage-reply")
    if mode == "raise_sql":
        raise OperationalError("Recovery test transaction boundary", {}, RuntimeError("injected"))
    if mode != "pause":
        raise ValueError("Unknown recovery checkpoint mode")
    deadline = time.monotonic() + min(float(command.get("timeout_s", 120)), 180)
    release = directory / f"release-{name}-{file_id}"
    while not release.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Recovery checkpoint timed out: {name}")
        time.sleep(0.05)
