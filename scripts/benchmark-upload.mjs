/** Reproduce a disk-backed browser pause/reload/resume and verified download.
 * Never prints workspace keys or signed URLs. Full-size runs are opt-in.
 */
import { chromium } from "@playwright/test";
import { createCipheriv, createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import {
  mkdir,
  mkdtemp,
  open,
  readFile,
  rm,
  stat,
  statfs,
} from "node:fs/promises";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { execFile } from "node:child_process";
import { promisify, parseArgs } from "node:util";
import { resolve, join, basename } from "node:path";
import os from "node:os";
const run = promisify(execFile);
const { values } = parseArgs({
  options: {
    bytes: { type: "string", default: "5000000000" },
    output: { type: "string", default: "test-results/large-file/results.json" },
    "base-url": { type: "string", default: "http://localhost:18091" },
    project: { type: "string", default: "clouddrive-sprint2-large" },
    channel: { type: "string" },
    "keep-fixture": { type: "boolean", default: false },
  },
});
const size = Number(values.bytes),
  baseURL = values["base-url"],
  project = values.project;
if (!Number.isSafeInteger(size) || size <= 4 * 8388608 || size > 5_000_000_000)
  throw new Error("--bytes must be an integer in 33554433..5000000000");
if (!/^[a-z0-9][a-z0-9_-]*$/.test(project))
  throw new Error("Invalid Compose project name");
const endpoint = new URL(baseURL);
if (!["localhost", "127.0.0.1"].includes(endpoint.hostname))
  throw new Error("This benchmark requires an isolated loopback test stack");
const output = resolve(values.output),
  started = performance.now();
const clock = () => Number(((performance.now() - started) / 1000).toFixed(6));
const cleanError = (error) =>
  String(error?.message || error)
    .split("Call log:")[0]
    .replace(/https?:\/\/[^\s]+/g, "[URL redacted]")
    .slice(0, 1500);
const report = {
  schema_version: 1,
  status: "running",
  started_at: new Date().toISOString(),
  scope:
    "One file, browser pause/reload/reselection, local MinIO, one attempt; not a concurrency or 20-case recovery benchmark",
  source: {
    bytes: size,
    unit: "B",
    generator:
      "AES-256-CTR over zeros; SHA-256(seed) key; zero IV; 4 MiB writes",
    seed: "CloudDrive Sprint 2 fixture v1",
  },
  environment: {
    platform: os.platform(),
    release: os.release(),
    architecture: os.arch(),
    host_cpu: os.cpus()[0]?.model,
    host_logical_cpus: os.cpus().length,
    host_memory_bytes: os.totalmem(),
    node: process.version,
    compose_project: project,
  },
  timings: {},
  checks: {},
  events: [],
  resources: {
    interval_ms: 1000,
    samples: [],
    sample_errors: 0,
    definition:
      "Chrome sum of process RSS (shared pages can be counted multiple times); Node RSS; Docker memory usage and CPU from docker stats. Samples are not instantaneous allocation maxima.",
  },
};
await mkdir(resolve(output, ".."), { recursive: true });
const reportFile = await open(output, "wx");
let browser,
  context,
  page,
  fixtureDir,
  sourcePath,
  fileId,
  sampleTimer,
  sampling,
  phase = "setup",
  releaseGate;
let services = [];
const sampleTasks = new Set();
function log(stage) {
  console.log(JSON.stringify({ stage, elapsed_s: clock(), bytes: size }));
}
async function docker(...args) {
  return (await run("docker", args, { maxBuffer: 4 * 1024 * 1024 })).stdout;
}
async function hashPath(path) {
  const hash = createHash("sha256");
  let bytes = 0;
  for await (const chunk of createReadStream(path, {
    highWaterMark: 1024 * 1024,
  })) {
    bytes += chunk.length;
    hash.update(chunk);
  }
  return { bytes, sha256: hash.digest("hex") };
}
async function poll(fn, description, timeout = 20 * 60_000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    if (report.browser_errors?.length) throw new Error("Browser runtime error");
    if (
      page &&
      ["first", "resume"].includes(phase) &&
      (await page.locator(".transfer-status").allTextContents()).some((s) =>
        s.includes("Interrupted"),
      )
    )
      throw new Error(
        "Transfer stopped: " + (await page.locator("#notice").innerText()),
      );
    const value = await fn();
    if (value) return value;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("Timed out: " + description);
}
async function inventory() {
  return page.evaluate(async () => {
    const response = await fetch("/api/files", {
      headers: {
        Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
      },
    });
    if (!response.ok) throw new Error("Inventory failed");
    return (await response.json()).files;
  });
}
function memoryBytes(value) {
  const m = value.match(/([\d.]+)\s*([KMGT]?i?B)/);
  if (!m) return null;
  const units = {
    B: 1,
    kB: 1000,
    KB: 1000,
    MB: 1e6,
    GB: 1e9,
    KiB: 1024,
    MiB: 1024 ** 2,
    GiB: 1024 ** 3,
  };
  return Math.round(Number(m[1]) * (units[m[2]] || 1));
}
async function sample() {
  if (sampling) return;
  sampling = true;
  const task = (async () => {
    try {
      const client = await browser.newBrowserCDPSession();
      let processes;
      try {
        processes = (await client.send("SystemInfo.getProcessInfo"))
          .processInfo;
      } finally {
        await client.detach();
      }
      const ids = processes
        .map((p) => p.id)
        .filter((id) => Number.isInteger(id) && id > 0);
      const raw = (await run("ps", ["-o", "pid=,rss=", "-p", ids.join(",")]))
        .stdout;
      const chromeRSS = raw
        .trim()
        .split("\n")
        .reduce(
          (n, line) => n + Number(line.trim().split(/\s+/)[1] || 0) * 1024,
          0,
        );
      const stats = (
        await docker(
          "stats",
          "--no-stream",
          "--format",
          "{{json .}}",
          ...services.map((s) => s.id),
        )
      )
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => {
          const s = JSON.parse(line);
          return {
            service:
              services.find(
                (v) =>
                  s.ID.startsWith(v.id.slice(0, 12)) || v.id.startsWith(s.ID),
              )?.service || s.Name,
            memory_bytes: memoryBytes(s.MemUsage.split("/")[0]),
            cpu_percent: Number(s.CPUPerc.replace("%", "")),
          };
        });
      report.resources.samples.push({
        t_s: clock(),
        phase,
        chrome_sum_rss_bytes: chromeRSS,
        node_rss_bytes: process.memoryUsage().rss,
        ui_transfer_state: await page
          .locator(".transfer-status")
          .allTextContents(),
        services: stats,
      });
      if (report.resources.samples.length % 10 === 0)
        console.log(
          JSON.stringify({
            stage: "progress",
            phase,
            elapsed_s: clock(),
            state: report.resources.samples.at(-1).ui_transfer_state,
          }),
        );
    } catch {
      report.resources.sample_errors++;
    } finally {
      sampling = false;
    }
  })();
  sampleTasks.add(task);
  await task;
  sampleTasks.delete(task);
}
try {
  // Resolve exact containers; inspect internally without logging credentials.
  const ids = (
    await docker(
      "compose",
      "-p",
      project,
      "-f",
      "compose.large.yaml",
      "ps",
      "-q",
    )
  )
    .trim()
    .split("\n")
    .filter(Boolean);
  if (ids.length !== 6)
    throw new Error(
      "Expected six running services in the dedicated large-file project",
    );
  const containers = JSON.parse(await docker("inspect", ...ids));
  services = containers.map((c) => ({
    id: c.Id,
    service: c.Config.Labels["com.docker.compose.service"],
  }));
  const api = containers.find(
    (c) => c.Config.Labels["com.docker.compose.service"] === "api",
  );
  const env = Object.fromEntries(
    api.Config.Env.map((v) => {
      const i = v.indexOf("=");
      return [v.slice(0, i), v.slice(i + 1)];
    }),
  );
  if (
    !new URL(env.CLOUDDRIVE_DATABASE_URL).pathname.endsWith("_test") ||
    !env.CLOUDDRIVE_S3_BUCKET.endsWith("-test")
  )
    throw new Error("Refusing benchmark against non-test database/bucket");
  if (api.HostConfig.PortBindings["8090/tcp"][0].HostPort !== endpoint.port)
    throw new Error("Base URL does not match isolated project API port");
  report.environment.images = containers.map((c) => ({
    service: c.Config.Labels["com.docker.compose.service"],
    image_id: c.Image,
  }));
  report.environment.database = new URL(
    env.CLOUDDRIVE_DATABASE_URL,
  ).pathname.slice(1);
  report.environment.bucket = env.CLOUDDRIVE_S3_BUCKET;
  report.environment.docker = JSON.parse(
    await docker(
      "info",
      "--format",
      '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}},"version":"{{.ServerVersion}}"}',
    ),
  );
  report.environment.git_sha = (
    await run("git", ["rev-parse", "HEAD"])
  ).stdout.trim();
  report.environment.git_dirty = Boolean(
    (await run("git", ["status", "--porcelain"])).stdout.trim(),
  );
  const tracked = [
    "clouddrive/config.py",
    "clouddrive/storage.py",
    "clouddrive/worker.py",
    "frontend/src/main.ts",
    "frontend/src/hash-worker.ts",
    "scripts/benchmark-upload.mjs",
    "compose.large.yaml",
  ];
  report.environment.source_sha256 = Object.fromEntries(
    await Promise.all(
      tracked.map(async (p) => [
        p,
        createHash("sha256")
          .update(await readFile(p))
          .digest("hex"),
      ]),
    ),
  );
  report.environment.other_running_container_count =
    (await docker("ps", "-q")).trim().split("\n").filter(Boolean).length -
    ids.length;
  await mkdir("test-results", { recursive: true });
  fixtureDir = await mkdtemp(resolve("test-results", "large-fixture-"));
  sourcePath = join(fixtureDir, "large-fixture.bin");
  const disk = await statfs(fixtureDir);
  report.environment.host_free_bytes = Number(disk.bavail) * Number(disk.bsize);
  if (report.environment.host_free_bytes < size * 3 + 2_000_000_000)
    throw new Error(
      "Insufficient host free disk for source, object, and download plus margin",
    );
  const minio = services.find((s) => s.service === "minio");
  const df = await docker("exec", minio.id, "df", "-Pk", "/data");
  report.environment.storage_free_bytes =
    Number(df.trim().split("\n").at(-1).trim().split(/\s+/)[3]) * 1024;
  if (report.environment.storage_free_bytes < size * 2 + 1_000_000_000)
    throw new Error("Insufficient object-storage disk capacity");
  log("generate-source");
  const generationStart = clock();
  const cipher = createCipheriv(
    "aes-256-ctr",
    createHash("sha256").update(report.source.seed).digest(),
    Buffer.alloc(16),
  );
  const zeros = Buffer.alloc(4 * 1024 * 1024);
  function* sourceBlocks() {
    for (let offset = 0; offset < size; offset += zeros.length)
      yield zeros.subarray(0, Math.min(zeros.length, size - offset));
  }
  await pipeline(
    Readable.from(sourceBlocks()),
    cipher,
    createWriteStream(sourcePath, { flags: "wx" }),
  );
  report.timings.fixture_generation_s = clock() - generationStart;
  const source = await hashPath(sourcePath);
  if (source.bytes !== size) throw new Error("Generated file size mismatch");
  report.source.sha256 = source.sha256;
  browser = await chromium.launch({
    headless: true,
    channel: values.channel || process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  report.environment.browser = browser.version();
  context = await browser.newContext({
    baseURL,
    acceptDownloads: true,
    viewport: { width: 1440, height: 1000 },
  });
  page = await context.newPage();
  page.setDefaultTimeout(30_000);
  const hashEnd = {};
  const puts = { first: [], resume: [] },
    acks = { first: [], resume: [] };
  page.on("pageerror", (error) => {
    (report.browser_errors ??= []).push(cleanError(error));
  });
  page.on("request", (request) => {
    const url = new URL(request.url()),
      number = Number(url.searchParams.get("partNumber"));
    if (request.method() === "PUT" && number && puts[phase]) {
      const event = { kind: "part_request", phase, number, t_s: clock() };
      puts[phase].push(event);
      report.events.push(event);
    }
    if (url.pathname.endsWith("/complete") && request.method() === "POST")
      report.timings.completion_started_s = clock();
    if (
      url.pathname === "/api/uploads" &&
      request.method() === "POST" &&
      phase === "first"
    )
      hashEnd.first = clock();
    if (
      fileId &&
      url.pathname === `/api/uploads/${fileId}` &&
      phase === "resume"
    )
      hashEnd.resume ??= clock();
  });
  page.on("response", (response) => {
    const url = new URL(response.url()),
      match = url.pathname.match(/\/api\/uploads\/[^/]+\/parts\/(\d+)$/);
    if (
      response.request().method() === "PUT" &&
      match &&
      response.ok() &&
      acks[phase]
    ) {
      const event = {
        kind: "part_ack",
        phase,
        number: Number(match[1]),
        t_s: clock(),
      };
      acks[phase].push(event);
      report.events.push(event);
    }
    if (
      url.pathname.endsWith("/complete") &&
      response.request().method() === "POST"
    )
      report.timings.completion_response_s = clock();
    const number = Number(url.searchParams.get("partNumber"));
    if (response.request().method() === "PUT" && number)
      report.events.push({
        kind: "storage_response",
        phase,
        number,
        status: response.status(),
        t_s: clock(),
      });
  });
  sampleTimer = setInterval(() => {
    void sample();
  }, 1000);
  await page.goto("/");
  await poll(
    async () =>
      /Workspace [a-f0-9]{8}/.test(
        await page.locator("#workspace-id").innerText(),
      ),
    "workspace creation",
    30_000,
  );
  const workspace = await page.evaluate(
    async () =>
      await (
        await fetch("/api/workspace", {
          headers: {
            Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
          },
        })
      ).json(),
  );
  if (workspace.max_file_bytes !== 5_000_000_000)
    throw new Error(
      "Packaged application does not advertise the decimal 5 GB limit",
    );
  report.environment.part_bytes = workspace.part_bytes;
  report.environment.part_concurrency = 3;
  // Both API and actual browser chooser reject a sparse oversized file before hashing/uploading.
  const apiLimit = await page.evaluate(async () => {
    const r = await fetch("/api/uploads", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        request_id: crypto.randomUUID(),
        name: "oversize.bin",
        size: 5_000_000_001,
        sha256: "0".repeat(64),
      }),
    });
    return r.status;
  });
  if (apiLimit !== 413) throw new Error("API failed to reject 5 GB + 1 byte");
  const oversizePath = join(fixtureDir, "oversize.bin"),
    oversized = await open(oversizePath, "wx");
  await oversized.truncate(5_000_000_001);
  await oversized.close();
  await page.locator("#file-input").setInputFiles(oversizePath);
  await poll(
    async () =>
      (await page.locator("#notice").innerText()).includes(
        "5 GB per-file limit",
      ),
    "browser size rejection",
    10_000,
  );
  if ((await inventory()).length !== 0)
    throw new Error("Oversized rejection allocated upload metadata");
  await rm(oversizePath);
  report.checks.oversized_api_status = apiLimit;
  report.checks.oversized_browser_rejected = true;
  let gateResolve;
  const gate = new Promise((r) => (gateResolve = r));
  releaseGate = gateResolve;
  await page.route(/.*\?.*partNumber=.*/, async (route) => {
    const n = Number(
      new URL(route.request().url()).searchParams.get("partNumber"),
    );
    if (route.request().method() === "PUT" && n > 3 && phase === "first") {
      await gate;
      await route.abort("failed").catch(() => {});
    } else await route.continue().catch(() => {});
  });
  phase = "first";
  log("initial-browser-hash-and-partial-upload");
  const firstSelection = clock();
  await page.locator("#file-input").setInputFiles(sourcePath);
  const partial = await poll(async () => {
    const f = (await inventory()).find((f) => f.name === basename(sourcePath));
    if (f) {
      fileId = f.id;
      if (f.parts.filter((p) => p.etag).length === 3) return f;
    }
    return false;
  }, "three acknowledged parts");
  report.timings.browser_initial_hash_to_initiation_s =
    hashEnd.first - firstSelection;
  await page
    .getByRole("button", { name: `Pause ${basename(sourcePath)}`, exact: true })
    .click();
  await poll(
    async () =>
      (await page.locator("#transfer-panel").innerText()).includes(
        "Paused · Select file to resume",
      ),
    "paused transfer",
    10_000,
  );
  releaseGate();
  releaseGate = undefined;
  await page.unrouteAll({ behavior: "wait" });
  report.checks.confirmed_before_reload = partial.parts
    .filter((p) => p.etag)
    .map((p) => p.number);
  phase = "reload";
  await page.reload();
  await poll(
    async () => (await inventory()).some((f) => f.id === fileId),
    "persisted session after reload",
    30_000,
  );
  const row = page.locator("tbody tr", { hasText: basename(sourcePath) });
  await row
    .getByRole("button", {
      name: `Resume ${basename(sourcePath)}`,
      exact: true,
    })
    .click();
  phase = "resume";
  log("resume-browser-hash-and-transfer");
  const resumeSelection = clock();
  await page.locator("#file-input").setInputFiles(sourcePath);
  const ready = await poll(async () => {
    const f = (await inventory()).find((f) => f.id === fileId);
    if (f?.status === "FAILED" || f?.status === "EXPIRED")
      throw new Error("Upload reached " + f.status);
    return f?.status === "READY" ? f : false;
  }, "READY after resume");
  report.timings.browser_resume_hash_to_session_lookup_s =
    hashEnd.resume - resumeSelection;
  report.timings.resume_selection_to_ready_s = clock() - resumeSelection;
  report.timings.multipart_completion_s =
    report.timings.completion_response_s - report.timings.completion_started_s;
  report.timings.completion_response_to_ready_observed_s =
    clock() - report.timings.completion_response_s;
  report.checks.acknowledged_parts = ready.parts.filter((p) => p.etag).length;
  report.checks.expected_parts = Math.ceil(size / workspace.part_bytes);
  report.checks.resent_confirmed_parts =
    report.checks.confirmed_before_reload.filter((n) =>
      puts.resume.some((p) => p.number === n),
    );
  if (
    report.checks.resent_confirmed_parts.length ||
    ready.sha256 !== source.sha256 ||
    ready.expected_sha256 !== source.sha256 ||
    ready.result.bytes_verified !== size ||
    report.checks.acknowledged_parts !== report.checks.expected_parts
  )
    throw new Error("Resume/worker integrity check failed");
  report.checks.worker_verified_bytes = ready.result.bytes_verified;
  report.checks.worker_sha256 = ready.sha256;
  report.timings.worker_stream_verification_s =
    ready.result.stream_verification_seconds;
  const firstWindow = acks.first.at(-1).t_s - puts.first[0].t_s,
    resumeWindow = acks.resume.at(-1).t_s - puts.resume[0].t_s;
  report.timings.first_transfer_window_s = firstWindow;
  report.timings.resume_transfer_window_s = resumeWindow;
  report.timings.active_transfer_windows_s = firstWindow + resumeWindow;
  report.transfer_window_MB_per_s =
    size / 1e6 / report.timings.active_transfer_windows_s;
  report.transfer_definition =
    "Source bytes / sum of first-PUT-to-last-ack windows before pause and after resume; includes signing, acknowledgements and retries, excludes both prehashes, intentional pause/reload, processing and download. Browser request events include intentionally intercepted/unaccepted requests 4..6 before pause.";
  phase = "download";
  log("browser-download-and-independent-hash");
  // API polling can see READY before the UI's three-second refresh does.
  await row
    .getByText("Verified", { exact: true })
    .waitFor({ state: "visible" });
  await row.getByText(basename(sourcePath), { exact: true }).click();
  const downloadStart = clock();
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 120_000 }),
    page.getByRole("button", { name: "Download file", exact: true }).click(),
  ]);
  const downloadedPath = await download.path();
  if (!downloadedPath) throw new Error("Browser download failed");
  report.timings.browser_download_click_to_local_completion_s =
    clock() - downloadStart;
  const hashStart = clock();
  const downloaded = await hashPath(downloadedPath);
  report.timings.independent_download_hash_s = clock() - hashStart;
  report.checks.download = downloaded;
  if (downloaded.bytes !== size || downloaded.sha256 !== source.sha256)
    throw new Error("Downloaded file differs from the source");
  if (report.browser_errors?.length)
    throw new Error("Browser emitted runtime errors");
  await page.screenshot({
    path: output.replace(/\.json$/, "") + "-verified.png",
    fullPage: true,
  });
  const deletion = await page.evaluate(
    async (id) =>
      (
        await fetch(`/api/files/${id}`, {
          method: "DELETE",
          headers: {
            Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
          },
        })
      ).status,
    fileId,
  );
  if (deletion !== 204) throw new Error("Benchmark object cleanup failed");
  report.checks.object_deleted = true;
  await sample();
  if (report.resources.samples.length < (size === 5_000_000_000 ? 5 : 1))
    throw new Error("Insufficient resource samples for measured evidence");
  report.status = "passed";
  log("passed");
} catch (error) {
  report.status = "failed";
  report.error = cleanError(error);
  console.error(report.error);
  process.exitCode = 1;
} finally {
  releaseGate?.();
  if (report.status === "failed" && fileId && page) {
    report.checks.failure_cleanup_status = await page
      .evaluate(
        async (id) =>
          (
            await fetch(`/api/files/${id}`, {
              method: "DELETE",
              headers: {
                Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
              },
            })
          ).status,
        fileId,
      )
      .catch(() => null);
  }
  clearInterval(sampleTimer);
  await Promise.allSettled([...sampleTasks]);
  report.resources.chrome_sum_rss_peak_bytes = Math.max(
    0,
    ...report.resources.samples.map((s) => s.chrome_sum_rss_bytes),
  );
  report.resources.node_rss_peak_bytes = Math.max(
    0,
    ...report.resources.samples.map((s) => s.node_rss_bytes),
  );
  report.resources.service_memory_peak_bytes = Object.fromEntries(
    services.map((s) => [
      s.service,
      Math.max(
        0,
        ...report.resources.samples.flatMap((v) =>
          v.services
            .filter((v) => v.service === s.service)
            .map((v) => v.memory_bytes || 0),
        ),
      ),
    ]),
  );
  await context?.close().catch(() => {});
  await browser?.close().catch(() => {});
  if (fixtureDir && !values["keep-fixture"])
    await rm(fixtureDir, { recursive: true, force: true });
  report.finished_at = new Date().toISOString();
  report.timings.runner_total_s = clock();
  await mkdir(resolve(output, ".."), { recursive: true });
  await reportFile.writeFile(JSON.stringify(report, null, 2) + "\n");
  await reportFile.close();
  console.log(JSON.stringify({ status: report.status, report: output }));
}
