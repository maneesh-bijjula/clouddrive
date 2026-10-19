# Verification

The initial implementation was verified locally on macOS ARM with Python 3.12, Node.js 26, Google Chrome, and Docker. CI uses Python 3.12, Node.js 24, and Chromium on Ubuntu.

| Check                                                     | Result    |
| --------------------------------------------------------- | --------- |
| Unit tests                                                | 27 passed |
| PostgreSQL + S3 integration tests                         | 21 passed |
| Browser workflows with a real Celery worker and scheduler | 5 passed  |
| TypeScript check and Vite production build                | Passed    |
| Black and Prettier checks                                 | Passed    |
| Browser workflows against the packaged Docker stack       | 5 passed  |
| Actual Redis outage and restart with an 18.9 MB upload    | Passed    |

The multipart API test uploads more than 8 MiB, completes two parts, verifies the entire object, and compares downloaded bytes. The browser resume test uploads a file of 12 MiB plus 17 bytes, pauses with one confirmed part, reloads the page, and checks that only the missing part is sent. Screenshots show actual files uploaded by those browser tests.

The database failure test injects a failure after successful S3 completion and before the processing state update, then verifies recovery on retry. Broker failure coverage checks retained outbox work. Worker fencing coverage replaces a claim or deletes a file during streaming and verifies that the stale worker cannot commit a result.

To repeat the packaged broker outage check against the local Docker stack, activate the development virtual environment, then run:

```bash
docker compose stop redis
python scripts/smoke.py stage --state /tmp/clouddrive-smoke.json
docker compose start redis
python scripts/smoke.py verify --state /tmp/clouddrive-smoke.json
```

The smoke check uploads 18,900,011 bytes while the broker is stopped, confirms that download is blocked, then waits for verification and compares the downloaded size and SHA-256 after restart. The temporary state file contains a workspace access key, is created with restricted permissions, and is removed after successful verification. Use a fresh state path for each run.

These checks validate the implemented workflows with MinIO. They do not establish production availability, large-file performance, live AWS behavior, or a concurrency capacity.
