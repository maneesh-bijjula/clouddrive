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

## Measured target-size result

**Passed October 26, 2026**, on clean implementation commit [`b82831e93e679170dd5c49d3bbd0d4dedb976fa2`](https://github.com/maneesh-bijjula/clouddrive/commit/b82831e93e679170dd5c49d3bbd0d4dedb976fa2). The evidence commit adds reports/documentation without changing that measured implementation. Source digests and immutable image IDs are included in the [JSON run record](evidence/sprint-2/5gb-run-1.json).

The denominator is **one target-size file, one full-size attempt, passed**. The file contained exactly **5,000,000,000 bytes**. All **597 parts** were confirmed. Parts **1, 2, and 3** were acknowledged before pause/reload; none were requested again during resume. The source hash, worker hash, and streamed downloaded-file hash all equal:

```text
3d38c1add12fbf81b468f29b946c4a30d092b8c2f5310eb89d1c8cbe62711817
```

The API returned 413 for 5,000,000,001 bytes, and the browser rejected a sparse file of that size without creating upload metadata. The successfully verified benchmark object was deleted afterward.

| Phase                                                                                                                                    | Observed time |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------------: |
| Deterministic fixture generation                                                                                                         |        5.12 s |
| Initial browser selection/hash to initiation request                                                                                     |       79.31 s |
| Reselection/hash to persisted-session lookup                                                                                             |       77.42 s |
| Sum of active transfer windows                                                                                                           |      111.11 s |
| Multipart completion API round trip                                                                                                      |        0.09 s |
| Successful worker stream/hash                                                                                                            |        8.43 s |
| Completion reply to READY observed by API polling                                                                                        |       10.82 s |
| Browser download click to local download completion                                                                                      |       34.70 s |
| Independent streamed downloaded-file SHA-256                                                                                             |        6.09 s |
| Complete runner including fixture/source hash, setup, both browser hashes, pause/reload, processing, download, verification, and cleanup |      350.73 s |

The transfer-window rate was **45.00 decimal MB/s** under the definition above. It is not the end-to-end rate, a p95 measurement, or an AWS/network-capacity claim.

The shared local host was an **Apple M3, 8 logical CPUs, 8 GiB RAM**, with Node 26.3.0 and Chrome 156.0.8078.48. Docker Engine 29.5.3 reported 8 CPUs and about 3.83 GiB usable VM memory. Storage was local disk-backed MinIO, with PostgreSQL/Redis/Celery in the same isolated project. Three other containers were running when the run began. The host and Docker disk-capacity preflight passed.

| Sampled memory metric                                  |      Peak |
| ------------------------------------------------------ | --------: |
| Sum of Chrome process RSS                              | 849.2 MiB |
| Node runner RSS                                        | 219.1 MiB |
| API Docker memory usage                                | 100.0 MiB |
| Worker container memory usage, including its processes | 190.1 MiB |
| MinIO Docker memory usage                              | 386.8 MiB |

There were **150 successful resource samples and zero sampling errors**. The nominal timer was 1 second; collection takes time and overlapping samples are skipped, so actual timestamps are authoritative. Sampling covers browser phases rather than fixture generation. Chrome RSS can count shared memory more than once; Docker usage follows the CLI's memory accounting. These sampled peaks are observations, not hard resource limits or proof of instantaneous maxima.

Evidence: [run JSON](evidence/sprint-2/5gb-run-1.json), [runner transcript](evidence/sprint-2/5gb-run-1.txt), [verified browser screenshot](evidence/sprint-2/5gb-run-1-verified.png), [78 Python tests](evidence/sprint-2/python-final.txt), [JUnit](evidence/sprint-2/python-final.xml), [20 packaged browser workflows](evidence/sprint-2/browser-complete.txt), and [run history](evidence/sprint-2/run-history.json).

## Development failures and scope

The run history preserves the earlier smaller rehearsals: the first exposed the sparse MinIO receipt issue, the second failed in the runner's UI/download synchronization, and the third passed with 67,108,881 bytes. Those are development rehearsals, not additional target-size successes. The initial broader browser run also caught the workspace readiness race; a deterministic test now covers it. Final verification passed 28 unit cases, 50 real-service integration cases, and 20 packaged browser workflows without retries locally.

Sprint 3's named **20-case recovery matrix remains pending**. The 20 browser workflow count is unrelated to that denominator. Sprint 4's **ten concurrent 500 MB transfers remain unmeasured**. This single-file MinIO result does not verify live AWS, repeated-run reliability, WAN behavior, production availability, or total memory bounds under concurrent users.
