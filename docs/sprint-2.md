# Sprint 2: decimal 5 GB uploads and measured resume

Sprint 2 extends Sprint 1 on `main`. This report distinguishes implemented limits, small-fixture regression tests, and the opt-in target-size measurement.

## Implementation

The default inclusive limit is **5,000,000,000 bytes (5 GB)**. `CLOUDDRIVE_MAX_FILE_BYTES` can reduce that limit; the supported maximum is decimal 5 GB rather than 5 GiB. The browser reads the authoritative workspace limit from the API and formats decimal kB/MB/GB with powers of 1000. Part sizes remain binary: 8 MiB by default, configurable from 5 MiB to 64 MiB. A 5 GB file has 597 default parts, with a final 389,632-byte part. Existing stored objects and migration checksums are unchanged.

Full-file browser hashing runs in a cancellable module worker with one 8 MiB file read at a time. Uploads still use at most three part loops per file. Pausing cancels hashing, active HTTP requests, and retry timers. Small per-part hashes are computed over bounded part buffers; no full-file ArrayBuffer is constructed. These are per-file bounds, not the global admission/resource controls planned for Sprint 4.

A part operation has at most four attempts, with 250/500/1000 ms backoff between attempts. Each retry re-signs and reconciles storage before sending bytes. Lost PUT/acknowledgement replies therefore reuse accepted parts. Network interruptions, timeouts, 408/429/5xx responses, and recognized expired-URL responses can retry; permanent access/checksum errors stop the transfer. `NoSuchUpload` is reconciled through the API and remains a terminal expiry rather than an endlessly retried upload. API requests time out at 30 seconds and individual storage PUTs at 120 seconds. Exhaustion preserves the session for explicit resume.

The runner's small-fixture test exposed a sparse/out-of-order MinIO ListParts issue: requesting a marker calculated from `part number - 1` could skip an existing part when earlier parts were still missing. Receipt lookup now follows the provider's returned pagination tokens from the first page. Real-service and browser regressions force the last part to finish first; acknowledgements still validate stored size, checksum, and ETag.

The worker records `stream_verification_seconds` for a successful stream/hash attempt, excluding queue wait and the final metadata transaction. This provides a measured verification phase, not a processing-latency guarantee.

## Reproduce the opt-in measurement

Use a dedicated local test project and at least 25-30 GB of free host disk. The runner checks capacity, loopback API port ownership, and `_test`/`-test` database/bucket suffixes before creating its workspace. The disk-backed profile avoids allocating a 5 GB object in the normal small-test stack's tmpfs.

```bash
docker compose -p clouddrive-sprint2-large -f compose.large.yaml up --build -d --wait
npm ci
npx playwright install chromium
npm run bench:upload -- --output test-results/large-file/unique-run.json
# On a machine with installed Chrome, add --channel chrome.
# Optional development smoke: --bytes 67108881 (this is not 5 GB evidence).

# After inspecting evidence, remove only this disposable project's storage.
docker compose -p clouddrive-sprint2-large -f compose.large.yaml down -v
```

Every output path must be new; the runner refuses to overwrite prior results. Source files use deterministic AES-256-CTR bytes with a documented seed and 4 MiB generator chunks. Source and downloaded hashes use independent streaming Node SHA-256. The source file is selected through the real browser file picker; the application hashes, signs, uploads, completes, and verifies it through real services. After three parts are acknowledged, the runner pauses, reloads, reselects the source, and proves those confirmed parts are not resent. It also checks API and browser rejection of 5 GB + 1 byte using a sparse file. The successful object and generated/downloaded fixtures are removed after verification.

JSON evidence contains source size/hash, exact runtime/image/source identities, accepted/resumed part outcomes, phase clocks, and resource samples. Keys and signed URLs are excluded. Browser memory is the sum of Chrome process RSS samples and can double-count shared pages; Docker memory is the CLI's reported usage. Samples are not instantaneous allocation maxima. Active transfer-window throughput includes signing/receipts/retries, excludes both prehashes, intentional pause/reload, processing, and download, and must not be presented as end-to-end throughput.

The manual `large-upload.yml` workflow runs the same procedure and retains evidence for 30 days. It is separate from ordinary CI because the full run consumes substantial disk and time. Capacity failure is a failure, not a skipped success; a sufficiently provisioned runner is required.

## Measurement status

The target-size run is performed after committing the implementation so its report can identify a clean, immutable tested code revision. The final results and scoped acceptance evidence are added in the subsequent evidence commit. Until that measurement passes, a configured 5 GB limit and small-fixture tests alone do not verify the 5 GB requirement.

Sprint 3's named 20-case recovery matrix and Sprint 4's ten concurrent 500 MB transfers remain separate requirements. Browser workflow counts must not be substituted for the recovery denominator. No live AWS or production-readiness claim is made by MinIO execution.
