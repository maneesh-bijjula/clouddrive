/** Ten real browser uploads, bounded fixtures and independently hashed downloads. */
import assert from "node:assert/strict";
import { chromium } from "@playwright/test";
import { createCipheriv, createHash, randomUUID } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import {
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  rm,
  statfs,
} from "node:fs/promises";
import { resolve, dirname, join, basename } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { execFile, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { connect } from "node:tls";
import { parseArgs, promisify } from "node:util";
import os from "node:os";
import { observeApi } from "./load-observer.mjs";

const run = promisify(execFile);
const { values } = parseArgs({
  options: {
    bytes: { type: "string", default: "500000000" },
    runs: { type: "string", default: "3" },
    output: { type: "string", default: "test-results/load/results.json" },
    project: { type: "string", default: "clouddrive-sprint4-load" },
    channel: { type: "string" },
  },
});
const size = Number(values.bytes),
  runs = Number(values.runs),
  count = 10;
assert.ok(
  Number.isSafeInteger(size) && size >= 16_777_233 && size <= 500_000_000,
);
assert.ok(Number.isInteger(runs) && runs >= 1 && runs <= 3);
const project = values.project;
assert.match(project, /^clouddrive-[a-z0-9-]*load[a-z0-9-]*$/);
const baseURL = "http://localhost:18093",
  output = resolve(values.output);
const started = performance.now(),
  clock = () => Number(((performance.now() - started) / 1000).toFixed(6));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const safeError = (e) =>
  String(e?.message || e)
    .split("Call log:")[0]
    .replace(/https?:\/\/[^\s]+/g, "[URL redacted]")
    .slice(0, 2000);
const budgetsPath = "docs/load-budgets.json",
  budgetsBytes = await readFile(budgetsPath),
  budgets = JSON.parse(budgetsBytes);
await mkdir(dirname(output), { recursive: true });
const fd = await open(output, "wx");
const report = {
  schema_version: 1,
  method_revision: 2,
  status: "running",
  run_id: randomUUID(),
  started_at: new Date().toISOString(),
  display_timezone: "America/Los_Angeles",
  started_at_local: new Date().toLocaleString("sv-SE", {
    timeZone: "America/Los_Angeles",
  }),
  scope:
    "One browser, ten selected files per cohort, local TLS/HTTP2 gateway to MinIO; declared overlap probe shaping; not live AWS or default HTTP/1 capacity",
  file_bytes: size,
  files_per_run: count,
  requested_runs: runs,
  target_size: size === 500_000_000,
  budget_sha256: sha(budgetsBytes),
  budgets,
  environment: {
    platform: os.platform(),
    release: os.release(),
    architecture: os.arch(),
    host_cpu: os.cpus()[0]?.model,
    host_logical_cpus: os.cpus().length,
    host_memory_bytes: os.totalmem(),
    node: process.version,
    compose_project: project,
    execution: {
      kind: process.env.GITHUB_ACTIONS === "true" ? "github_actions" : "local",
      repository: process.env.GITHUB_REPOSITORY ?? null,
      run_id: process.env.GITHUB_RUN_ID ?? null,
      run_attempt: process.env.GITHUB_RUN_ATTEMPT ?? null,
      source_sha: process.env.GITHUB_SHA ?? null,
      runner_environment: process.env.RUNNER_ENVIRONMENT ?? null,
      runner_os: process.env.RUNNER_OS ?? null,
      runner_arch: process.env.RUNNER_ARCH ?? null,
    },
  },
  fixtures: [],
  runs: [],
  observer_api: {
    transport:
      "Native Node HTTP client, new socket per request, Connection: close, absolute15s deadline, no retries; excluded from native browser API latency population",
    requests: 0,
    status_counts: {},
    transport_failures: [],
  },
  resources: {
    interval_ms: 1000,
    samples: [],
    sample_errors: 0,
    skipped_overlapping_collections: 0,
    definition:
      "Actual timestamps are authoritative. Chrome RSS sums may double-count shared pages. Container memory usage is Docker accounting; process RSS sums may also double-count shared pages. Docker CPU percentages use 100% per CPU core. Samples are observations, not instantaneous maxima.",
  },
};
let saveQueue = Promise.resolve();
const save = () => {
  const write = async () => {
    const b = Buffer.from(JSON.stringify(report, null, 2) + "\n");
    await fd.write(b, 0, b.length, 0);
    await fd.truncate(b.length);
    await fd.sync();
  };
  saveQueue = saveQueue.then(write);
  return saveQueue;
};
const log = (stage, extra = {}) =>
  console.log(JSON.stringify({ stage, t_s: clock(), ...extra }));
const docker = async (...args) =>
  (await run("docker", args, { timeout: 60_000, maxBuffer: 8 * 1024 * 1024 }))
    .stdout;
const compose = (...args) =>
  docker("compose", "-p", project, "-f", "compose.load.yaml", ...args);
const python = process.env.CLOUDDRIVE_TEST_PYTHON || "python";
const env = {
  ...process.env,
  PYTHONPATH: ".",
  CLOUDDRIVE_DATABASE_URL:
    "postgresql+psycopg://clouddrive:clouddrive@localhost:15439/clouddrive_load_test",
  CLOUDDRIVE_REDIS_URL: "redis://localhost:16386/0",
  CLOUDDRIVE_S3_ENDPOINT: "http://localhost:19014",
  CLOUDDRIVE_S3_PUBLIC_ENDPOINT: "https://localhost:19013",
  CLOUDDRIVE_S3_BUCKET: "clouddrive-load-test",
  CLOUDDRIVE_S3_ACCESS_KEY: "clouddrive",
  CLOUDDRIVE_S3_SECRET_KEY: "clouddrive-local",
};
let browser,
  context,
  page,
  fixtureDir,
  observer,
  observerLine,
  pendingObserver,
  metricsKey,
  services = [],
  timer,
  sampling,
  phase = "setup",
  cohort,
  releaseGate;
let observerQueue = Promise.resolve();
const sampleTasks = new Set(),
  eventTasks = new Set();
let lastBrowserCPU,
  lastNodeCPU = process.cpuUsage(),
  lastNodeWall = performance.now();
async function hashPath(path) {
  let bytes = 0;
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(path, {
    highWaterMark: 1024 * 1024,
  })) {
    bytes += chunk.length;
    digest.update(chunk);
  }
  return { bytes, sha256: digest.digest("hex") };
}
function observe(command = {}) {
  const task = observerQueue.then(
    () =>
      new Promise((resolve, reject) => {
        const t = setTimeout(() => {
          pendingObserver = undefined;
          reject(new Error("Load observer timeout"));
        }, 10_000);
        pendingObserver = {
          resolve: (v) => {
            clearTimeout(t);
            resolve(v);
          },
          reject,
        };
        observer.stdin.write(JSON.stringify(command) + "\n");
      }),
  );
  observerQueue = task.catch(() => {});
  return task;
}
async function api(path, { token, method = "GET", body, ops = false } = {}) {
  report.observer_api.requests++;
  try {
    const response = await observeApi(baseURL + "/api" + path, {
      method,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(ops ? { "X-Metrics-Key": metricsKey } : {}),
      },
      body,
    });
    const key = String(response.status);
    report.observer_api.status_counts[key] =
      (report.observer_api.status_counts[key] ?? 0) + 1;
    return response;
  } catch (e) {
    report.observer_api.transport_failures.push({
      run: cohort?.run ?? null,
      phase,
      method,
      route: path.replace(/[0-9a-f-]{36}/g, ":id"),
      code: e.code ?? "ERR_OBSERVER_REQUEST",
      cause_code: e.cause_code ?? null,
      t_s: clock(),
    });
    throw e;
  }
}
async function wait(fn, label, seconds = 600) {
  const end = performance.now() + seconds * 1000;
  while (performance.now() < end) {
    const v = await fn();
    if (v) return v;
    if (cohort?.fatal) throw new Error(cohort.fatal);
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("Timeout: " + label);
}
function memBytes(v) {
  const m = v.match(/([\d.]+)\s*([KMGT]?i?B)/);
  assert.ok(m, v);
  return Math.round(
    Number(m[1]) *
      {
        B: 1,
        kB: 1000,
        KB: 1000,
        MB: 1e6,
        GB: 1e9,
        KiB: 1024,
        MiB: 1024 ** 2,
        GiB: 1024 ** 3,
      }[m[2]],
  );
}
async function cgroupState(service) {
  const raw = await docker(
    "exec",
    service.id,
    "sh",
    "-c",
    'cat /sys/fs/cgroup/memory.events || exit 1; for p in /proc/[0-9]*/status; do cat "$p" 2>/dev/null || true; done',
  );
  const value = { rss_sum_bytes: 0, oom: 0, oom_kill: 0 };
  for (const line of raw.split("\n")) {
    let m;
    if ((m = line.match(/^VmRSS:\s+(\d+)\s+kB/)))
      value.rss_sum_bytes += Number(m[1]) * 1024;
    if ((m = line.match(/^(oom|oom_kill) (\d+)$/))) value[m[1]] = Number(m[2]);
  }
  return value;
}
async function sample() {
  if (sampling) {
    report.resources.skipped_overlapping_collections++;
    return;
  }
  sampling = true;
  const collectionStarted = clock();
  const task = (async () => {
    try {
      const session = await browser.newBrowserCDPSession();
      let processes;
      try {
        processes = (await session.send("SystemInfo.getProcessInfo"))
          .processInfo;
      } finally {
        await session.detach();
      }
      const ids = processes
        .map((p) => p.id)
        .filter((x) => Number.isInteger(x) && x > 0);
      const rss = (
        await run("ps", ["-o", "pid=,rss=", "-p", ids.join(",")])
      ).stdout
        .trim()
        .split("\n")
        .reduce((s, l) => s + Number(l.trim().split(/\s+/)[1] || 0) * 1024, 0);
      const now = performance.now();
      const cpuByPID = new Map(processes.map((p) => [p.id, p.cpuTime]));
      const chromeCPU = lastBrowserCPU
        ? (100 *
            processes.reduce(
              (sum, p) =>
                sum +
                Math.max(
                  0,
                  p.cpuTime - (lastBrowserCPU.processes.get(p.id) ?? p.cpuTime),
                ),
              0,
            )) /
          ((now - lastBrowserCPU.wall) / 1000)
        : null;
      lastBrowserCPU = { processes: cpuByPID, wall: now };
      const nCPU = process.cpuUsage(),
        nodeCPU =
          (100 *
            (nCPU.user + nCPU.system - lastNodeCPU.user - lastNodeCPU.system)) /
          ((now - lastNodeWall) * 1000);
      lastNodeCPU = nCPU;
      lastNodeWall = now;
      const [statsRaw, states, ops, db] = await Promise.all([
        docker(
          "stats",
          "--no-stream",
          "--format",
          "{{json .}}",
          ...services.map((s) => s.id),
        ),
        Promise.all(services.map((s) => cgroupState(s))),
        api("/ops", { ops: true }),
        observe(),
      ]);
      assert.equal(ops.status, 200);
      assert.ok(!db.error_type);
      const stats = statsRaw
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((l) => JSON.parse(l))
        .map((s) => {
          const index = services.findIndex(
            (v) => v.id.startsWith(s.ID) || s.ID.startsWith(v.id.slice(0, 12)),
          );
          assert.ok(index >= 0);
          return {
            service: services[index].service,
            cpu_percent: Number(s.CPUPerc.replace("%", "")),
            memory_usage_bytes: memBytes(s.MemUsage.split("/")[0]),
            ...states[index],
          };
        });
      assert.deepEqual(
        stats.map((s) => s.service).sort(),
        services.map((s) => s.service).sort(),
        "Incomplete resource sample",
      );
      const record = {
        t_s: clock(),
        collection_started_at_s: collectionStarted,
        run: cohort?.run ?? null,
        phase,
        browser_rss_sum_bytes: rss,
        browser_cpu_percent: chromeCPU,
        node_rss_bytes: process.memoryUsage().rss,
        node_cpu_percent: nodeCPU,
        services: stats,
        ops: ops.data,
        database: db,
        scheduler: page
          ? await page
              .locator("#transfer-panel")
              .evaluate((e) => ({ ...e.dataset }))
          : null,
      };
      record.collection_finished_at_s = clock();
      report.resources.samples.push(record);
      for (const [key, limit] of Object.entries({
        active_files: 10,
        hash_workers: 2,
        global_parts: 10,
      })) {
        if (record.scheduler?.[key] !== undefined)
          assert.ok(
            Number(record.scheduler[key]) <= limit,
            `Scheduler budget exceeded: ${key}`,
          );
      }
      assert.ok(
        rss <= budgets.resource_budgets.browser_sampled_rss_sum_ceiling_bytes,
        "Browser RSS budget exceeded",
      );
      assert.ok(
        record.node_rss_bytes <=
          budgets.resource_budgets.runner_sampled_rss_ceiling_bytes,
        "Runner RSS budget exceeded",
      );
      for (const s of stats) {
        assert.equal(
          s.oom,
          services.find((x) => x.service === s.service).oom_start.oom,
          "cgroup OOM allocation",
        );
        assert.equal(
          s.oom_kill,
          services.find((x) => x.service === s.service).oom_start.oom_kill,
          "cgroup OOM kill",
        );
        assert.ok(
          s.memory_usage_bytes <=
            budgets.resource_budgets.containers[s.service]
              .memory_hard_limit_bytes,
        );
      }
      if (report.resources.samples.length % 10 === 0) {
        await save();
        log("sample", {
          run: cohort?.run,
          phase,
          files: cohort?.files?.map((f) => ({
            name: f.name,
            ready: !!f.ready_at_s,
          })),
        });
      }
    } catch (e) {
      report.resources.sample_errors++;
      if (cohort) cohort.fatal = safeError(e);
      report.resources.last_error = safeError(e);
    } finally {
      sampling = false;
    }
  })();
  sampleTasks.add(task);
  await task;
  sampleTasks.delete(task);
}
function latencySummary(events) {
  const ms = events
    .filter((e) => Number.isFinite(e.duration_ms) && e.duration_ms >= 0)
    .map((e) => e.duration_ms)
    .sort((a, b) => a - b);
  return {
    count: ms.length,
    p50_ms: ms.length ? ms[Math.ceil(ms.length * 0.5) - 1] : null,
    p95_ms: ms.length ? ms[Math.ceil(ms.length * 0.95) - 1] : null,
    definition:
      "Nearest-rank browser request.timing.responseEnd milliseconds for native application API requests; excludes observer requests; includes failed statuses in the recorded population",
  };
}
function peakIntervals(intervals) {
  const events = intervals
    .flatMap(([start, end]) => [
      [start, 1],
      [end, -1],
    ])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let active = 0,
    peak = 0;
  for (const [, delta] of events) {
    active += delta;
    peak = Math.max(peak, active);
  }
  return peak;
}
async function workerEvents(since) {
  const service = services.find((s) => s.service === "worker");
  const { stdout, stderr } = await run(
    "docker",
    ["logs", "--since", since, service.id],
    { timeout: 15_000, maxBuffer: 8 * 1024 * 1024 },
  );
  return (stdout + stderr).split("\n").flatMap((line) => {
    const at = line.indexOf("{");
    if (at < 0) return [];
    try {
      const v = JSON.parse(line.slice(at));
      return v.event ? [v] : [];
    } catch {
      return [];
    }
  });
}
function instrument() {
  window.__load = { puts: [], hashes: [] };
  const originalOpen = XMLHttpRequest.prototype.open,
    originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...args) {
    const u = new URL(url, location.href);
    if (method === "PUT" && u.searchParams.has("partNumber")) {
      const v = {
        file_id: u.pathname.split("/").at(-1),
        part: Number(u.searchParams.get("partNumber")),
        partial_samples: 0,
      };
      this.__loadPut = v;
      window.__load.puts.push(v);
      this.upload.addEventListener("progress", (e) => {
        v.loaded = e.loaded;
        v.total = e.total;
        if (e.loaded > 0 && e.loaded < e.total) {
          v.first_partial_ms ??= performance.now();
          v.last_partial_ms = performance.now();
          v.partial_samples++;
        }
      });
      this.upload.addEventListener(
        "loadend",
        () => (v.upload_end_ms = performance.now()),
      );
      this.addEventListener("loadend", () => {
        v.response_end_ms = performance.now();
        v.status = this.status;
      });
      this.addEventListener("error", () => (v.error = true));
      this.addEventListener("abort", () => (v.aborted = true));
    }
    return originalOpen.call(this, method, url, ...args);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    if (this.__loadPut) this.__loadPut.send_ms = performance.now();
    return originalSend.apply(this, args);
  };
  const Original = window.Worker;
  window.Worker = class extends Original {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", (e) => {
        if (this.__loadHash && (e.data.hash || e.data.error)) {
          this.__loadHash.end_ms = performance.now();
          this.__loadHash.error = !!e.data.error;
        }
      });
    }
    postMessage(...args) {
      if (args[0]?.file instanceof File) {
        this.__loadHash = {
          name: args[0].file.name,
          start_ms: performance.now(),
        };
        window.__load.hashes.push(this.__loadHash);
      }
      return super.postMessage(...args);
    }
    terminate() {
      if (this.__loadHash && !this.__loadHash.end_ms) {
        this.__loadHash.end_ms = performance.now();
        this.__loadHash.cancelled = true;
      }
      return super.terminate();
    }
  };
}
try {
  const ids = (await compose("ps", "--quiet"))
    .trim()
    .split("\n")
    .filter(Boolean);
  assert.equal(ids.length, 7);
  const containers = JSON.parse(await docker("inspect", ...ids));
  services = containers.map((c) => ({
    id: c.Id,
    service: c.Config.Labels["com.docker.compose.service"],
  }));
  assert.deepEqual(
    services.map((s) => s.service).sort(),
    Object.keys(budgets.resource_budgets.containers).sort(),
  );
  for (const c of containers) {
    const name = c.Config.Labels["com.docker.compose.service"];
    assert.equal(c.Config.Labels["com.docker.compose.project"], project);
    assert.equal(
      resolve(c.Config.Labels["com.docker.compose.project.working_dir"]),
      process.cwd(),
    );
    assert.equal(
      c.HostConfig.Memory,
      budgets.resource_budgets.containers[name].memory_hard_limit_bytes,
    );
    assert.equal(
      c.HostConfig.NanoCpus / 1e9,
      budgets.resource_budgets.containers[name].cpu_limit_cores,
    );
    assert.equal(c.State.Running, true);
    const port = {
      api: ["8090/tcp", "18093"],
      postgres: ["5432/tcp", "15439"],
      redis: ["6379/tcp", "16386"],
      minio: ["9000/tcp", "19014"],
      gateway: ["443/tcp", "19013"],
    }[name];
    if (port)
      assert.deepEqual(c.NetworkSettings.Ports[port[0]], [
        { HostIp: "127.0.0.1", HostPort: port[1] },
      ]);
  }
  const apiContainer = containers.find(
      (c) => c.Config.Labels["com.docker.compose.service"] === "api",
    ),
    apiEnv = Object.fromEntries(
      apiContainer.Config.Env.map((s) => {
        const n = s.indexOf("=");
        return [s.slice(0, n), s.slice(n + 1)];
      }),
    );
  for (const c of containers.filter((c) =>
    ["api", "worker", "beat"].includes(
      c.Config.Labels["com.docker.compose.service"],
    ),
  ))
    assert.equal(
      c.Image,
      apiContainer.Image,
      "API/worker/beat must use the same immutable image",
    );
  assert.ok(apiEnv.CLOUDDRIVE_DATABASE_URL.endsWith("/clouddrive_load_test"));
  assert.equal(apiEnv.CLOUDDRIVE_S3_BUCKET, "clouddrive-load-test");
  metricsKey = apiEnv.CLOUDDRIVE_METRICS_KEY;
  assert.ok(metricsKey);
  const configKeys = [
    "max_active_uploads_per_workspace",
    "max_active_uploads_global",
    "max_api_inflight",
    "db_pool_size",
    "db_max_overflow",
    "db_pool_timeout_seconds",
    "admission_lock_timeout_seconds",
    "browser_active_files",
    "browser_hash_workers",
    "browser_global_parts",
    "browser_parts_per_file",
  ];
  report.environment.effective_api_settings = JSON.parse(
    await compose(
      "exec",
      "-T",
      "api",
      "python",
      "-c",
      `import json;from clouddrive.config import settings;s=settings();print(json.dumps({k:getattr(s,k) for k in ${JSON.stringify(configKeys)}}))`,
    ),
  );
  const expectedConfig = {
    max_active_uploads_per_workspace: 10,
    max_active_uploads_global: 100,
    max_api_inflight: 64,
    db_pool_size: 8,
    db_max_overflow: 8,
    db_pool_timeout_seconds: 10,
    admission_lock_timeout_seconds: 5,
    browser_active_files: 10,
    browser_hash_workers: 2,
    browser_global_parts: 10,
    browser_parts_per_file: 3,
  };
  assert.deepEqual(report.environment.effective_api_settings, expectedConfig);
  assert.ok(
    containers
      .find((c) => c.Config.Labels["com.docker.compose.service"] === "worker")
      .Config.Cmd.includes("--concurrency=2"),
  );
  report.environment.minio_version = (
    await compose("exec", "-T", "minio", "minio", "--version")
  ).trim();
  report.environment.gateway_version = (
    await compose("exec", "-T", "gateway", "caddy", "version")
  ).trim();
  report.environment.images = containers.map((c) => ({
    service: c.Config.Labels["com.docker.compose.service"],
    image_id: c.Image,
  }));
  report.environment.docker = JSON.parse(
    await docker(
      "info",
      "--format",
      '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}},"version":"{{.ServerVersion}}"}',
    ),
  );
  const git = async (...args) => (await run("git", args)).stdout.trim();
  report.environment.git_sha = await git("rev-parse", "HEAD");
  if (process.env.GITHUB_ACTIONS === "true")
    assert.equal(
      report.environment.git_sha,
      process.env.GITHUB_SHA,
      "Hosted checkout must match workflow source revision",
    );
  report.environment.git_dirty = !!(await git("status", "--porcelain"));
  if (size === 500_000_000 && runs === 3)
    assert.equal(
      report.environment.git_dirty,
      false,
      "Commit implementation before the scored30-file run",
    );
  const sources = [
    "compose.load.yaml",
    "Caddyfile.load",
    "Dockerfile",
    "Dockerfile.minio",
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "tsconfig.json",
    "frontend/index.html",
    "frontend/vite.config.ts",
    "frontend/src/style.css",
    "frontend/public/favicon.svg",
    budgetsPath,
  ];
  for (const d of ["clouddrive", "frontend/src", "scripts"]) {
    for (const n of await readdir(d))
      if (
        (d === "clouddrive" && n.endsWith(".py")) ||
        (d === "frontend/src" && n.endsWith(".ts")) ||
        (d === "scripts" &&
          [
            "benchmark-concurrent.mjs",
            "load-state.py",
            "load-observer.mjs",
            "summarize-load.py",
          ].includes(n))
      )
        sources.push(`${d}/${n}`);
  }
  report.environment.source_sha256 = Object.fromEntries(
    await Promise.all(sources.map(async (p) => [p, sha(await readFile(p))])),
  );
  const packed = JSON.parse(
    await compose(
      "exec",
      "-T",
      "api",
      "python",
      "-c",
      "import hashlib,json,pathlib;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('clouddrive').glob('*.py')}))",
    ),
  );
  for (const [p, h] of Object.entries(packed))
    assert.equal(
      report.environment.source_sha256[p],
      h,
      `Rebuild required: ${p}`,
    );
  // Compare all packaged assets, not only Python: a stale image could otherwise
  // run an older scheduler while source digests describe the current TypeScript.
  const packedAssets = JSON.parse(
    await compose(
      "exec",
      "-T",
      "api",
      "python",
      "-c",
      "import hashlib,json,pathlib;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('dist').rglob('*') if p.is_file()}))",
    ),
  );
  assert.ok(Object.keys(packedAssets).length > 0);
  for (const [p, h] of Object.entries(packedAssets))
    assert.equal(sha(await readFile(p)), h, `Frontend rebuild required: ${p}`);
  report.environment.packaged_asset_sha256 = packedAssets;
  report.environment.other_running_containers =
    (await docker("ps", "-q")).trim().split("\n").filter(Boolean).length - 7;
  const free = await statfs(".");
  report.environment.host_free_bytes = Number(free.bavail) * Number(free.bsize);
  assert.ok(
    report.environment.host_free_bytes >=
      (size === 500_000_000
        ? budgets.disk_budget.minimum_host_free_before_fixture_generation_bytes
        : size * count * 3 + 1e9),
    "Insufficient host disk before fixtures",
  );
  const df = (
    await docker(
      "exec",
      services.find((s) => s.service === "minio").id,
      "df",
      "-Pk",
      "/data",
    )
  )
    .trim()
    .split("\n")
    .at(-1)
    .trim()
    .split(/\s+/);
  report.environment.storage_free_bytes = Number(df[3]) * 1024;
  assert.ok(
    report.environment.storage_free_bytes >=
      (size === 500_000_000
        ? budgets.disk_budget.minimum_storage_free_before_scored_run_bytes
        : size * count * 2 + 1e9),
  );
  report.environment.certificate = await new Promise((resolve, reject) => {
    const s = connect(
      {
        host: "localhost",
        servername: "localhost",
        port: 19013,
        rejectUnauthorized: false,
        ALPNProtocols: ["h2"],
      },
      () => {
        const c = s.getPeerCertificate();
        resolve({
          fingerprint256: c.fingerprint256,
          subject: c.subject,
          issuer: c.issuer,
          negotiated_protocol: s.alpnProtocol,
          scope: "Local test CA; no host trust store changes",
        });
        s.end();
      },
    );
    s.on("error", reject);
  });
  assert.equal(report.environment.certificate.negotiated_protocol, "h2");
  for (const s of services) s.oom_start = await cgroupState(s);
  observer = spawn(python, ["scripts/load-state.py"], {
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });
  observerLine = createInterface({ input: observer.stdout });
  observerLine.on("line", (line) => {
    const pending = pendingObserver;
    pendingObserver = undefined;
    if (pending) pending.resolve(JSON.parse(line));
  });
  observer.on("exit", (code) => {
    if (pendingObserver)
      pendingObserver.reject(new Error(`Observer exited ${code}`));
  });
  fixtureDir = await mkdtemp(resolve("test-results/load-fixture-"));
  log("generate-fixtures");
  const generatedAt = clock();
  for (let i = 0; i < count; i++) {
    const name = `load-${String(i + 1).padStart(2, "0")}.bin`,
      seed = `CloudDrive Sprint4 fixture v1/${i + 1}`,
      path = join(fixtureDir, name),
      cipher = createCipheriv(
        "aes-256-ctr",
        createHash("sha256").update(seed).digest(),
        Buffer.alloc(16),
      ),
      zero = Buffer.alloc(4 * 1024 * 1024);
    function* blocks() {
      for (let o = 0; o < size; o += zero.length)
        yield zero.subarray(0, Math.min(zero.length, size - o));
    }
    await pipeline(
      Readable.from(blocks()),
      cipher,
      createWriteStream(path, { flags: "wx" }),
    );
    report.fixtures.push({ name, seed, ...(await hashPath(path)) });
  }
  report.fixture_generation_and_independent_hash_s = clock() - generatedAt;
  assert.equal(new Set(report.fixtures.map((f) => f.sha256)).size, 10);
  browser = await chromium.launch({
    headless: true,
    channel: values.channel || process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  report.environment.browser = browser.version();
  for (let n = 1; n <= runs; n++) {
    cohort = {
      run: n,
      status: "running",
      started_at: new Date().toISOString(),
      started_at_s: clock(),
      files: report.fixtures.map((f) => ({ ...f })),
      api_events: [],
      storage_events: [],
      browser_errors: [],
      api_transport_failures: [],
      phase_durations: {},
    };
    report.runs.push(cohort);
    await save();
    const runRecord = cohort;
    let token, ws, cdp, outsiderToken;
    const barrierTasks = new Set();
    lastBrowserCPU = undefined;
    const deadlineTimer = setTimeout(() => {
      cohort.fatal = "Cohort900s deadline exceeded";
      void context?.close().catch(() => {});
    }, 900_000);
    try {
      context = await browser.newContext({
        baseURL,
        acceptDownloads: true,
        ignoreHTTPSErrors: true,
        viewport: { width: 1440, height: 1000 },
      });
      await context.addInitScript(instrument);
      page = await context.newPage();
      page.setDefaultTimeout(30_000);
      phase = "prepare";
      page.on("requestfailed", (req) => {
        if (runRecord.closing) return;
        const u = new URL(req.url());
        if (u.origin === baseURL && u.pathname.startsWith("/api/")) {
          (runRecord.api_transport_failures ??= []).push({
            route: u.pathname.replace(/[0-9a-f-]{36}/g, ":id"),
            method: req.method(),
            t_s: clock(),
          });
          runRecord.fatal = "API transport failure";
        }
      });
      page.on("pageerror", (e) => {
        if (runRecord.closing) return;
        runRecord.browser_errors.push(safeError(e));
        runRecord.fatal = "Browser runtime error";
      });
      page.on("requestfinished", (req) => {
        if (runRecord.closing) return;
        const task = (async () => {
          const u = new URL(req.url());
          if (u.origin !== baseURL || !u.pathname.startsWith("/api/")) return;
          const response = await req.response();
          const timing = req.timing();
          const route = u.pathname
            .replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/g, ":id")
            .replace(/\/parts\/\d+/g, "/parts/:number");
          const fileId = u.pathname.match(/\/uploads\/([0-9a-f-]{36})/)?.[1];
          const entry = {
            file_id: fileId,
            route,
            method: req.method(),
            status: response.status(),
            duration_ms: timing.responseEnd,
            request_id: response.headers()["x-request-id"],
            t_s: clock(),
            phase,
          };
          runRecord.api_events.push(entry);
          assert.match(
            entry.request_id ?? "",
            /^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/,
            "Missing request correlation header",
          );
          if (response.status() >= 500)
            runRecord.fatal = `API ${response.status()}`;
          if (
            req.method() === "POST" &&
            u.pathname === "/api/uploads" &&
            response.ok()
          ) {
            const body = req.postDataJSON(),
              file = runRecord.files.find((f) => f.name === body.name);
            assert.ok(file);
            file.id = (await response.json()).id;
            file.initiated_at_s = clock();
          }
          if (u.pathname.endsWith("/complete")) {
            const f = runRecord.files.find((f) => u.pathname.includes(f.id));
            if (f) {
              f.completion_response_s = clock();
              f.completion_api_s = timing.responseEnd / 1000;
            }
          }
        })().catch((e) => (runRecord.fatal = safeError(e)));
        eventTasks.add(task);
        task.finally(() => eventTasks.delete(task));
      });
      cdp = await context.newCDPSession(page);
      await cdp.send("Network.enable");
      const storageRequests = new Set();
      cdp.on("Network.requestWillBeSent", (e) => {
        const u = new URL(e.request.url);
        if (
          e.request.method === "PUT" &&
          u.origin === "https://localhost:19013" &&
          u.searchParams.has("partNumber")
        )
          storageRequests.add(e.requestId);
      });
      cdp.on("Network.responseReceived", (e) => {
        const u = new URL(e.response.url);
        if (
          storageRequests.has(e.requestId) &&
          u.origin === "https://localhost:19013" &&
          u.searchParams.has("partNumber")
        ) {
          runRecord.storage_events.push({
            file_id: u.pathname.split("/").at(-1),
            part: Number(u.searchParams.get("partNumber")),
            protocol: e.response.protocol,
            status: e.response.status,
            t_s: clock(),
          });
          if (e.response.status >= 500)
            runRecord.fatal = `Storage ${e.response.status}`;
        }
      });
      await page.goto("/");
      await wait(
        async () =>
          /Workspace [a-f0-9]{8}/.test(
            await page.locator("#workspace-id").innerText(),
          ),
        "workspace",
        30,
      );
      token = await page.evaluate(() =>
        localStorage.getItem("clouddrive.access"),
      );
      const workspace = await api("/workspace", { token });
      assert.equal(workspace.status, 200);
      ws = workspace.data.id;
      cohort.workspace_id = ws;
      cohort.configuration = workspace.data;
      assert.equal(workspace.data.max_file_bytes, 5_000_000_000);
      assert.deepEqual(workspace.data.upload_limits, {
        active_files: 10,
        hash_workers: 2,
        global_parts: 10,
        parts_per_file: 3,
      });
      cohort.database_before = await observe();
      timer = setInterval(() => void sample(), 1000);
      await sample();
      assert.equal(report.resources.sample_errors, 0);
      const first = new Set();
      let release;
      const gate = new Promise((r) => (release = r));
      releaseGate = release;
      // Only the ten first-part requests need interception. Later parts must
      // not keep paying the browser-driver routing/body-copy overhead.
      const firstPartPattern =
        /^https:\/\/localhost:19013\/.*[?&]partNumber=1(?:&|$)/;
      const firstPartHandler = (route) => {
        const task = (async () => {
          const u = new URL(route.request().url());
          if (route.request().method() === "PUT") {
            first.add(u.pathname.split("/").at(-1));
            await gate;
          }
          if (runRecord.closing) await route.abort("aborted");
          else await route.continue();
        })().catch((e) => {
          if (!runRecord.closing) {
            (runRecord.barrier_errors ??= []).push(safeError(e));
            runRecord.fatal ??= "First-part barrier continuation failed";
          }
        });
        barrierTasks.add(task);
        void task.then(() => barrierTasks.delete(task));
        return task;
      };
      await page.route(firstPartPattern, firstPartHandler);
      await cdp.send("Network.emulateNetworkConditions", {
        offline: false,
        latency: 0,
        downloadThroughput: -1,
        uploadThroughput:
          budgets.network_profile.overlap_probe.upload_cap_bytes_per_second,
      });
      cohort.probe = {
        cap_bytes_per_second:
          budgets.network_profile.overlap_probe.upload_cap_bytes_per_second,
        shaping_started_at_s: clock(),
      };
      phase = "hash-and-transfer";
      cohort.selected_at_s = clock();
      log("select-ten", { run: n, bytes: size });
      await page
        .locator("#file-input")
        .setInputFiles(cohort.files.map((f) => join(fixtureDir, f.name)));
      await wait(() => first.size === 10, "ten first PUTs prepared", 110);
      await wait(
        () => cohort.files.every((f) => f.id),
        "ten initiation identities",
        10,
      );
      const outsiderBefore = await api("/workspaces", { method: "POST" });
      assert.equal(outsiderBefore.status, 201);
      outsiderToken = outsiderBefore.data.access_key;
      for (const f of cohort.files) {
        f.pre_ready_download_status = (
          await api(`/files/${f.id}/download`, { token })
        ).status;
        assert.equal(f.pre_ready_download_status, 409);
        f.cross_workspace_pending_status = (
          await api(`/files/${f.id}/download`, { token: outsiderToken })
        ).status;
        assert.equal(f.cross_workspace_pending_status, 404);
      }
      assert.ok(
        (await page.evaluate(() => window.__load.puts)).every(
          (p) => p.part === 1,
        ),
        "Unexpected bytes before first-part barrier",
      );
      cohort.probe.barrier_released_browser_ms = await page.evaluate(() =>
        performance.now(),
      );
      cohort.probe.barrier_released_at_s = clock();
      await save();
      release();
      releaseGate = undefined;
      // Wait for all already-held first PUTs to continue before removing only
      // this handler. Neither the progress proof nor its shaping is bypassed.
      await Promise.all([...barrierTasks]);
      await page.unroute(firstPartPattern, firstPartHandler);
      cohort.probe.barrier_interceptor_removed_at_s = clock();
      await wait(
        async () => {
          const p = await page.evaluate(() =>
            window.__load.puts.filter((p) => p.part === 1),
          );
          return p.length === 10 && p.every((x) => x.first_partial_ms > 0)
            ? p
            : false;
        },
        "positive partial progress on ten PUTs",
        30,
      );
      const probe = await page.evaluate(() =>
        window.__load.puts.filter((p) => p.part === 1),
      );
      assert.ok(
        probe.every((p) => p.upload_end_ms === undefined),
        "A first PUT finished before all ten showed partial byte progress",
      );
      await cdp.send("Network.emulateNetworkConditions", {
        offline: false,
        latency: 0,
        downloadThroughput: -1,
        uploadThroughput: -1,
      });
      cohort.probe.shaping_removed_at_s = clock();
      phase = "transfer";
      const ready = await wait(
        async () => {
          const inv = await api("/files", { token });
          assert.equal(inv.status, 200);
          for (const f of cohort.files) {
            const s = inv.data.files.find((v) => v.name === f.name);
            if (s?.status === "READY" && !f.ready_at_s) {
              f.ready_at_s = clock();
              f.worker = { bytes: s.result.bytes_verified, sha256: s.sha256 };
              f.stream_verification_s = s.result.stream_verification_seconds;
            }
            if (s && ["FAILED", "EXPIRED"].includes(s.status))
              throw new Error(`File failed ${f.name}`);
          }
          const panel = await page.locator("#transfer-panel").innerText();
          if (
            panel.includes("Interrupted") ||
            panel.includes("Waiting for capacity")
          )
            throw new Error(
              "Transfer stopped: " +
                (await page.locator("#notice").innerText()),
            );
          return inv.data.files.length === 10 &&
            cohort.files.every((f) => f.ready_at_s)
            ? inv.data.files
            : false;
        },
        "ten READY files",
        Math.max(1, 600 - (clock() - cohort.selected_at_s)),
      );
      cohort.all_ready_at_s = clock();
      phase = "verify-and-download";
      await save();
      await Promise.all([...eventTasks]);
      const trace = await page.evaluate(() => window.__load);
      cohort.browser_trace = trace;
      cohort.observed_peak_hash_workers = peakIntervals(
        trace.hashes.map((h) => [h.start_ms, h.end_ms]),
      );
      cohort.observed_peak_xhr_requests = peakIntervals(
        trace.puts.map((p) => [p.send_ms, p.response_end_ms]),
      );
      assert.ok(cohort.observed_peak_hash_workers <= 2);
      assert.ok(cohort.observed_peak_xhr_requests <= 10);
      const initial = trace.puts.filter((p) => p.part === 1);
      assert.equal(initial.length, 10);
      assert.equal(new Set(initial.map((p) => p.file_id)).size, 10);
      const begin = Math.max(...initial.map((p) => p.first_partial_ms)),
        end = Math.min(...initial.map((p) => p.upload_end_ms));
      assert.ok(end > begin, "No ten-way active byte-transfer intersection");
      cohort.overlap = {
        kind: "Ten first PUTs with positive partial upload progress, ending at XHR upload.loadend rather than response completion",
        common_start_browser_ms: begin,
        common_end_browser_ms: end,
        common_seconds: (end - begin) / 1000,
        files: initial.map((p) => ({
          file_id: p.file_id,
          first_partial_ms: p.first_partial_ms,
          upload_end_ms: p.upload_end_ms,
          partial_samples: p.partial_samples,
        })),
      };
      await wait(
        () => cohort.storage_events.length === trace.puts.length,
        "All storage protocol events",
        10,
      );
      assert.equal(cohort.storage_events.length, trace.puts.length);
      assert.deepEqual(
        cohort.storage_events
          .filter((e) => e.part === 1)
          .map((e) => e.file_id)
          .sort(),
        cohort.files.map((f) => f.id).sort(),
        "HTTP/2 proof must cover all ten initial file IDs",
      );
      assert.ok(
        cohort.storage_events.every(
          (e) => e.protocol === "h2" && e.status === 200,
        ),
      );
      assert.ok(
        trace.puts.every((p) => !p.error && !p.aborted && p.status === 200),
      );
      const ends = trace.puts.map((p) => p.upload_end_ms);
      assert.ok(ends.every(Number.isFinite));
      cohort.transfer_window_s =
        (Math.max(...ends) - cohort.probe.barrier_released_browser_ms) / 1000;
      cohort.transfer_MB_per_s =
        (size * count) / 1e6 / cohort.transfer_window_s;
      cohort.selection_to_ready_s =
        cohort.all_ready_at_s - cohort.selected_at_s;
      cohort.selection_to_ready_MB_per_s =
        (size * count) / 1e6 / cohort.selection_to_ready_s;
      const state = await observe({ workspace: ws, inventory: true });
      assert.equal(state.files.length, 10);
      assert.equal(state.object_count, 10);
      assert.equal(state.object_bytes, size * count);
      assert.equal(state.multipart_count, 0);
      for (const f of cohort.files) {
        assert.ok(f.id);
        assert.deepEqual(f.worker, { bytes: f.bytes, sha256: f.sha256 });
        const stored = state.files.find((v) => v.id === f.id);
        assert.equal(stored.result_count, 1);
        assert.equal(stored.claim_active, false);
        f.selection_to_ready_s = f.ready_at_s - cohort.selected_at_s;
        const hash = trace.hashes.find((v) => v.name === f.name);
        assert.ok(hash?.end_ms);
        f.browser_hash_s = (hash.end_ms - hash.start_ms) / 1000;
        f.part_put_count = trace.puts.filter((p) => p.file_id === f.id).length;
        f.expected_parts = Math.ceil(size / workspace.data.part_bytes);
        assert.equal(f.part_put_count, f.expected_parts);
        f.retried_parts = f.part_put_count - f.expected_parts;
        const fp = trace.puts.filter((p) => p.file_id === f.id);
        f.transfer_window_s =
          (Math.max(...fp.map((p) => p.upload_end_ms)) -
            cohort.probe.barrier_released_browser_ms) /
          1000;
        f.completion_to_ready_s = f.ready_at_s - f.completion_response_s;
      }
      cohort.final_inventory = state;
      const workspaceMetrics = await api("/metrics", { token });
      assert.equal(workspaceMetrics.status, 200);
      cohort.workspace_metrics = workspaceMetrics.data;

      for (const f of cohort.files) {
        assert.equal(
          (
            await api(`/files/${f.id}/download`, {
              token: outsiderToken,
            })
          ).status,
          404,
        );
        f.cross_workspace_download_status = 404;
      }
      cohort.api_latency = latencySummary(cohort.api_events);
      cohort.api_latency_by_route = Object.fromEntries(
        [...new Set(cohort.api_events.map((e) => e.route))].map((route) => [
          route,
          latencySummary(cohort.api_events.filter((e) => e.route === route)),
        ]),
      );
      for (const f of cohort.files) {
        const row = page.locator("tbody tr", { hasText: f.name });
        await wait(
          async () =>
            await row.getByText("Verified", { exact: true }).isVisible(),
          "Verified UI",
          15,
        );
        await row.getByText(f.name, { exact: true }).click();
        const t = clock();
        const [download] = await Promise.all([
          page.waitForEvent("download", { timeout: 120_000 }),
          page
            .getByRole("button", { name: "Download file", exact: true })
            .click(),
        ]);
        const path = await download.path();
        f.download_s = clock() - t;
        const h = clock();
        f.download = await hashPath(path);
        f.download_hash_s = clock() - h;
        assert.deepEqual(f.download, { bytes: f.bytes, sha256: f.sha256 });
        f.integrity_status = "passed";
        await download.delete();
        await page
          .locator("[data-close]")
          .filter({ visible: true })
          .first()
          .click();
      }
      assert.equal(cohort.browser_errors.length, 0);
      const work = await workerEvents(cohort.started_at);
      cohort.worker_events = work.filter(
        (e) =>
          cohort.files.some((f) => f.id === e.file_id) ||
          e.event === "outbox_dispatched",
      );
      cohort.outbox_publications = work
        .filter((e) => e.event === "outbox_dispatched")
        .reduce((n, e) => n + e.published, 0);
      for (const f of cohort.files) {
        const commits = work.filter(
          (e) =>
            e.event === "verification_finished" &&
            e.file_id === f.id &&
            e.status === "ready",
        );
        assert.equal(
          commits.length,
          1,
          "Expected one actual committed verifier result",
        );
        f.committed_result_count = commits.length;
        f.task_attempts = work.filter(
          (e) => e.event === "processing_task" && e.file_id === f.id,
        );
        assert.ok(f.task_attempts.length >= 1);
      }
      assert.ok(!cohort.fatal, cohort.fatal);
      assert.equal(report.resources.sample_errors, 0);
      cohort.phase_durations = {
        selection_to_barrier_s:
          cohort.probe.barrier_released_at_s - cohort.selected_at_s,
        probe_shaping_s:
          cohort.probe.shaping_removed_at_s - cohort.probe.shaping_started_at_s,
        transfer_window_s: cohort.transfer_window_s,
        selection_to_ready_s: cohort.selection_to_ready_s,
        downloads_and_independent_hash_s: cohort.files.reduce(
          (s, f) => s + f.download_s + f.download_hash_s,
          0,
        ),
      };
      cohort.status = "passed";
    } catch (e) {
      cohort.status = "failed";
      cohort.error = safeError(e);
      cohort.error_code = e.code ?? null;
      cohort.error_cause_code = e.cause_code ?? null;
    } finally {
      clearTimeout(deadlineTimer);
      clearInterval(timer);
      timer = undefined;
      await Promise.all([...sampleTasks]);
      await Promise.all([...eventTasks]);
      // Snapshot native outcomes before shutdown or metadata/storage cleanup
      // can create intentional aborts or NoSuchUpload responses of their own.
      if (page) {
        try {
          cohort.final_browser_trace = await page.evaluate(() => window.__load);
          cohort.trace_snapshot_at_s = clock();
          cohort.cleanup_interrupted_puts = cohort.final_browser_trace.puts
            .filter((p) => !Number.isFinite(p.response_end_ms))
            .map((p) => ({
              file_id: p.file_id,
              part: p.part,
              send_ms: p.send_ms,
              outcome: "interrupted_measurement_at_shutdown",
              completion_status: "unknown_at_trace_cutoff",
            }));
        } catch (e) {
          cohort.status = "failed";
          cohort.trace_snapshot_error = safeError(e);
          cohort.error ??= "Pre-cleanup browser trace unavailable";
        }
      }
      cohort.closing = true;
      cohort.native_observation_closed_at_s = clock();
      page?.removeAllListeners("requestfailed");
      page?.removeAllListeners("requestfinished");
      await Promise.all([...eventTasks]);
      releaseGate?.();
      releaseGate = undefined;
      await Promise.all([...barrierTasks]);
      let browserClosed = !context;
      try {
        await context?.close();
        browserClosed = true;
        cohort.browser_stopped_before_cleanup_at_s = clock();
      } catch (e) {
        cohort.status = "failed";
        cohort.browser_shutdown_error = safeError(e);
      }
      await cdp?.detach().catch(() => {});
      context = page = undefined;
      if (ws && token && browserClosed) {
        try {
          cohort.cleanup_started_at_s = clock();
          const inv = await api("/files", { token });
          assert.equal(inv.status, 200);
          cohort.cleanup = [];
          for (const f of inv.data.files) {
            const d = await api(`/files/${f.id}`, { token, method: "DELETE" });
            cohort.cleanup.push({ file_id: f.id, status: d.status });
            assert.equal(d.status, 204);
          }
          cohort.after_cleanup = await observe({
            workspace: ws,
            inventory: true,
          });
          assert.equal(cohort.after_cleanup.object_count, 0);
          assert.equal(cohort.after_cleanup.multipart_count, 0);
          assert.equal(cohort.after_cleanup.outbox.count, 0);
          assert.equal(cohort.after_cleanup.work.active_claims, 0);
          assert.equal(cohort.after_cleanup.work.expired_claims, 0);
          assert.equal(cohort.after_cleanup.work.processing, 0);
          assert.ok(
            cohort.after_cleanup.files.every((f) => f.status === "DELETED"),
          );
        } catch (e) {
          cohort.status = "failed";
          cohort.cleanup_error = safeError(e);
        }
      } else if (ws && token) {
        cohort.cleanup_skipped = "Browser shutdown could not be confirmed";
      }
      // Retain observed attempts even when a cohort failed before all files
      // reached READY or completed independent download verification.
      for (const f of cohort.files) {
        if (!f.id || !cohort.final_browser_trace) continue;
        const attempts = cohort.final_browser_trace.puts.filter(
          (p) => p.file_id === f.id,
        );
        f.observed_part_put_attempts = attempts.length;
        const observedStatus = (p) =>
          !Number.isFinite(p.response_end_ms)
            ? "interrupted"
            : String(p.status ?? "unknown");
        f.observed_put_status_counts = Object.fromEntries(
          [...new Set(attempts.map(observedStatus))].map((status) => [
            status,
            attempts.filter((p) => observedStatus(p) === status).length,
          ]),
        );
        f.observed_retried_part_attempts =
          attempts.length - new Set(attempts.map((p) => p.part)).size;
      }
      if (!cohort.worker_events && cohort.files.some((f) => f.id)) {
        try {
          const events = await workerEvents(cohort.started_at);
          cohort.worker_events = events.filter(
            (e) =>
              cohort.files.some((f) => f.id === e.file_id) ||
              e.event === "outbox_dispatched",
          );
          for (const f of cohort.files.filter((f) => f.id)) {
            f.task_attempts = events.filter(
              (e) => e.event === "processing_task" && e.file_id === f.id,
            );
            f.committed_result_count = events.filter(
              (e) =>
                e.event === "verification_finished" &&
                e.file_id === f.id &&
                e.status === "ready",
            ).length;
          }
        } catch (e) {
          cohort.worker_log_error = safeError(e);
        }
      }
      cohort.finished_at = new Date().toISOString();
      cohort.total_elapsed_s = clock() - cohort.started_at_s;
      for (const f of cohort.files) f.integrity_status ??= "not_verified";
      cohort.file_integrity_passed = cohort.files.filter(
        (f) => f.integrity_status === "passed",
      ).length;
      cohort.api_latency = latencySummary(cohort.api_events);
      cohort.api_latency_by_route = Object.fromEntries(
        [...new Set(cohort.api_events.map((e) => e.route))].map((route) => [
          route,
          latencySummary(cohort.api_events.filter((e) => e.route === route)),
        ]),
      );
      if (cohort.total_elapsed_s > 900) {
        cohort.status = "failed";
        cohort.error = "Cohort deadline exceeded";
      }
      if (cohort.fatal) {
        cohort.status = "failed";
        cohort.error = cohort.fatal;
      }
      log("cohort-finished", {
        run: n,
        status: cohort.status,
        error: cohort.error,
        overlap_s: cohort.overlap?.common_seconds,
        transfer_MB_per_s: cohort.transfer_MB_per_s,
      });
      await save();
      if (cohort.browser_shutdown_error)
        throw new Error("Browser shutdown failed; scoped cleanup skipped");
    }
  }
  report.final_global_inventory = await observe({ inventory: true });
  assert.equal(report.final_global_inventory.object_count, 0);
  assert.equal(report.final_global_inventory.multipart_count, 0);
  assert.equal(report.final_global_inventory.outbox.count, 0);
  assert.equal(report.final_global_inventory.work.active_claims, 0);
  assert.equal(report.final_global_inventory.work.expired_claims, 0);
  assert.equal(report.final_global_inventory.work.processing, 0);
  for (const s of services) {
    const end = await cgroupState(s);
    assert.equal(end.oom, s.oom_start.oom);
    assert.equal(end.oom_kill, s.oom_start.oom_kill);
    const c = JSON.parse(await docker("inspect", s.id))[0];
    assert.equal(c.State.OOMKilled, false);
    assert.equal(c.RestartCount, 0);
    assert.equal(c.State.Running, true);
    s.oom_end = end;
  }
  report.environment.cgroup_checks = services.map(
    ({ id, service, oom_start, oom_end }) => ({
      id,
      service,
      oom_start,
      oom_end,
    }),
  );
  assert.equal(report.resources.sample_errors, 0);
  assert.ok(report.resources.samples.length > 0);
  for (const [p, h] of Object.entries(report.environment.source_sha256))
    assert.equal(sha(await readFile(p)), h, `Source changed: ${p}`);
  assert.equal(await git("rev-parse", "HEAD"), report.environment.git_sha);
  assert.equal(
    new Set(report.runs.map((r) => r.workspace_id)).size,
    runs,
    "Fresh workspace per cohort required",
  );
  assert.equal(
    new Set(report.runs.flatMap((r) => r.files.map((f) => f.id))).size,
    runs * count,
    "Fresh file IDs per cohort required",
  );
  report.status =
    report.runs.length === runs &&
    report.runs.every((r) => r.status === "passed")
      ? "passed"
      : "failed";
  report.file_outcomes_passed = report.runs.reduce(
    (n, r) =>
      n +
      (r.status === "passed" ? r.files.filter((f) => f.download).length : 0),
    0,
  );
  report.file_integrity_passed = report.runs.reduce(
    (n, r) => n + r.file_integrity_passed,
    0,
  );
  report.expected_file_outcomes = runs * count;
  report.sources_unchanged_during_run = true;
  report.release_gate_passed =
    report.status === "passed" &&
    size === 500_000_000 &&
    runs === 3 &&
    report.file_outcomes_passed === 30;
} catch (e) {
  report.status = "failed";
  report.error = safeError(e);
  report.error_code = e.code ?? null;
  report.error_cause_code = e.cause_code ?? null;
} finally {
  releaseGate?.();
  clearInterval(timer);
  await Promise.all([...sampleTasks]);
  await context?.close().catch(() => {});
  await browser?.close().catch(() => {});
  observer?.stdin.end();
  observerLine?.close();
  if (fixtureDir) await rm(fixtureDir, { recursive: true, force: true });
  report.finished_at = new Date().toISOString();
  report.total_runner_s = clock();
  await save();
  await fd.close();
}
console.log(
  JSON.stringify({
    status: report.status,
    release_gate_passed: report.release_gate_passed,
    passed_files: report.file_outcomes_passed,
    output,
  }),
);
if (report.status !== "passed") process.exitCode = 1;
