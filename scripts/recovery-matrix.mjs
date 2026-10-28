/** Execute the versioned recovery denominator; preserve every failed full pass. */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import {
  chmod,
  mkdir,
  open,
  readFile,
  readdir,
  realpath,
} from "node:fs/promises";
import { resolve, dirname, join } from "node:path";
import { parseArgs, promisify } from "node:util";
import os from "node:os";
import { runUploadCases } from "./recovery-upload.mjs";

const run = promisify(execFile);
const { values } = parseArgs({
  options: {
    output: { type: "string", default: "test-results/recovery/results.json" },
    passes: { type: "string", default: "3" },
    cases: { type: "string" },
    project: { type: "string", default: "clouddrive-sprint3-recovery" },
    "base-url": { type: "string", default: "http://localhost:18092" },
    "control-dir": { type: "string", default: "test-results/recovery-control" },
  },
});
const output = resolve(values.output),
  project = values.project;
const passes = Number(values.passes),
  baseURL = values["base-url"];
const controlDir = resolve(values["control-dir"]);
const controlRoot = resolve("test-results") + "/";
assert.ok(
  controlDir.startsWith(controlRoot),
  "Controls must be inside this repository's test-results directory",
);
assert.ok(Number.isInteger(passes) && passes >= 1 && passes <= 3);
assert.match(project, /^clouddrive-[a-z0-9-]*recovery[a-z0-9-]*$/);
assert.ok(["localhost", "127.0.0.1"].includes(new URL(baseURL).hostname));
assert.equal(new URL(baseURL).port, "18092");
const manifestPath = "docs/recovery-matrix.json";
const manifestBytes = await readFile(manifestPath);
const manifest = JSON.parse(manifestBytes);
const ids = manifest.cases.map((c) => c.id);
assert.equal(ids.length, 20);
assert.deepEqual(
  ids,
  Array.from({ length: 20 }, (_, i) => `R${String(i + 1).padStart(2, "0")}`),
);
const selected = values.cases ? values.cases.split(",") : ids;
assert.equal(new Set(selected).size, selected.length);
assert.ok(selected.length && selected.every((id) => ids.includes(id)));
const fullMatrix = selected.length === 20;
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const safeError = (error) =>
  String(error?.message || error)
    .split("Call log:")[0]
    .replace(/https?:\/\/[^\s]+/g, "[URL redacted]")
    .slice(0, 2000);
await mkdir(dirname(output), { recursive: true });
const fd = await open(output, "wx");
const report = {
  schema_version: 1,
  suite: manifest.suite,
  started_at: new Date().toISOString(),
  run_id: randomUUID(),
  status: "running",
  scope:
    "Packaged local MinIO/PostgreSQL/Redis/Celery recovery; small deterministic fixtures; no live AWS, concurrency or large-file measurement",
  manifest_sha256: sha(manifestBytes),
  selected_cases: selected,
  full_matrix: fullMatrix,
  requested_full_passes: passes,
  expected_attempts: selected.length * passes,
  environment: {
    platform: os.platform(),
    architecture: os.arch(),
    node: process.version,
    host_cpu: os.cpus()[0]?.model,
    host_logical_cpus: os.cpus().length,
    host_memory_bytes: os.totalmem(),
    compose_project: project,
  },
  passes: [],
};
const save = async () => {
  const bytes = Buffer.from(JSON.stringify(report, null, 2) + "\n");
  await fd.write(bytes, 0, bytes.length, 0);
  await fd.truncate(bytes.length);
  await fd.sync();
};
const env = {
  ...process.env,
  PYTHONPATH: ".",
  CLOUDDRIVE_DATABASE_URL:
    "postgresql+psycopg://clouddrive:clouddrive@localhost:15438/clouddrive_recovery_test",
  CLOUDDRIVE_REDIS_URL: "redis://localhost:16385/0",
  CLOUDDRIVE_S3_ENDPOINT: "http://localhost:19012",
  CLOUDDRIVE_S3_PUBLIC_ENDPOINT: "http://localhost:19012",
  CLOUDDRIVE_S3_BUCKET: "clouddrive-recovery-test",
  CLOUDDRIVE_S3_ACCESS_KEY: "clouddrive",
  CLOUDDRIVE_S3_SECRET_KEY: "clouddrive-local",
  CLOUDDRIVE_S3_REGION: "us-east-1",
  CLOUDDRIVE_RECOVERY_CONTROL_DIR: controlDir,
};
Object.assign(process.env, env);
const python = process.env.CLOUDDRIVE_TEST_PYTHON || "python";
const compose = (...args) =>
  run(
    "docker",
    ["compose", "-p", project, "-f", "compose.recovery.yaml", ...args],
    { env, timeout: 120_000 },
  );

function validate(result) {
  assert.equal(
    result.status,
    "passed",
    safeError(result.error || "Case failed"),
  );
  assert.equal(result.fault_observed, true, "Fault not observed");
  assert.ok(result.source?.bytes > 0);
  assert.match(result.source?.sha256 || "", /^[a-f0-9]{64}$/);
  assert.deepEqual(result.download, result.source);
  if (Number(result.id.slice(1)) <= 9) {
    assert.equal(result.final?.status, "READY");
    assert.equal(result.final?.metadata_count, 1);
    assert.equal(result.final?.object_count, 1);
    assert.equal(result.final?.multipart_count, 0);
    assert.equal(result.final?.result_count, 1);
    assert.equal(result.final?.sha256, result.source.sha256);
    assert.equal(result.final?.bytes_verified, result.source.bytes);
    assert.equal(result.pre_ready_download_status, 409);
    assert.equal(result.cross_workspace_download_status, 404);
  } else {
    assert.equal(result.final_status, "READY");
    assert.deepEqual(result.worker, result.source);
    assert.equal(result.metadata_count, 1);
    assert.equal(result.object_count, 1);
    assert.equal(result.multipart_count, 0);
    assert.equal(result.committed_result_count, 1);
    assert.equal(result.preverification_download_blocked, true);
    assert.equal(result.wrong_workspace_download_blocked, true);
  }
  assert.ok(Number.isFinite(result.recovery_s) && result.recovery_s >= 0);
  const limit = manifest.cases.find(
    (c) => c.id === result.id,
  ).recovery_deadline_seconds;
  assert.ok(result.recovery_s <= limit, `Recovery exceeded ${limit}s deadline`);
}

try {
  await mkdir(controlDir, { recursive: true });
  assert.ok(
    (await realpath(controlDir)).startsWith(controlRoot),
    "Recovery controls cannot follow a symlink outside test-results",
  );
  await chmod(controlDir, 0o777);
  const { stdout: services } = await compose("ps", "--all", "--format", "json");
  const rows = services
    .trim()
    .split("\n")
    .filter(Boolean)
    .flatMap((line) => JSON.parse(line));
  assert.deepEqual(rows.map((r) => r.Service).sort(), [
    "api",
    "beat",
    "minio",
    "postgres",
    "redis",
    "worker",
  ]);
  assert.ok(
    rows.every((r) => r.State === "running"),
    "All six isolated services must be running before measurement",
  );
  const { stdout: details } = await run(
    "docker",
    ["inspect", ...rows.map((r) => r.ID)],
    { env },
  );
  const containers = JSON.parse(details);
  for (const c of containers) {
    const service = c.Config.Labels["com.docker.compose.service"];
    const port = {
      api: ["8090/tcp", "18092"],
      postgres: ["5432/tcp", "15438"],
      redis: ["6379/tcp", "16385"],
      minio: ["9000/tcp", "19012"],
    }[service];
    if (port) {
      assert.deepEqual(
        c.NetworkSettings.Ports[port[0]],
        [{ HostIp: "127.0.0.1", HostPort: port[1] }],
        `Unexpected ${service} port ownership`,
      );
    }
    assert.equal(c.Config.Labels["com.docker.compose.project"], project);
    assert.equal(
      resolve(c.Config.Labels["com.docker.compose.project.working_dir"]),
      process.cwd(),
    );
    if (
      ["api", "worker", "beat"].includes(
        c.Config.Labels["com.docker.compose.service"],
      )
    ) {
      assert.ok(
        c.Config.Env.includes("CLOUDDRIVE_S3_BUCKET=clouddrive-recovery-test"),
      );
      assert.ok(
        c.Config.Env.includes(
          "CLOUDDRIVE_DATABASE_URL=postgresql+psycopg://clouddrive:clouddrive@postgres:5432/clouddrive_recovery_test",
        ),
      );
      assert.ok(
        c.Mounts.some(
          (m) =>
            m.Destination === "/recovery-control" &&
            resolve(m.Source) === controlDir,
        ),
      );
    }
  }
  const health = await fetch(`${baseURL}/api/health`);
  assert.equal(health.status, 200);
  const git = async (...args) => (await run("git", args)).stdout.trim();
  report.environment.git_sha = await git("rev-parse", "HEAD");
  report.environment.git_dirty = Boolean(await git("status", "--porcelain"));
  if (fullMatrix && passes === 3) {
    assert.equal(
      report.environment.git_dirty,
      false,
      "Commit the implementation before the three-pass release run",
    );
  }
  const { stdout: dockerInfo } = await run("docker", [
    "info",
    "--format",
    "{{json .}}",
  ]);
  const docker = JSON.parse(dockerInfo);
  report.environment.docker = {
    version: docker.ServerVersion,
    cpus: docker.NCPU,
    memory_bytes: docker.MemTotal,
  };
  report.environment.images = containers.map((c) => ({
    service: c.Config.Labels["com.docker.compose.service"],
    image_id: c.Image,
  }));
  report.environment.other_running_container_count =
    (await run("docker", ["ps", "-q"])).stdout
      .trim()
      .split("\n")
      .filter(Boolean).length - 6;
  report.environment.source_sha256 = {};
  for (const dir of ["clouddrive", "scripts"]) {
    for (const name of await readdir(dir)) {
      if (
        (dir === "clouddrive" && name.endsWith(".py")) ||
        (dir === "scripts" && /^recovery[-_].*\.(py|mjs)$/.test(name))
      ) {
        const path = `${dir}/${name}`;
        report.environment.source_sha256[path] = sha(await readFile(path));
      }
    }
  }
  report.environment.source_sha256["compose.recovery.yaml"] = sha(
    await readFile("compose.recovery.yaml"),
  );
  for (const path of ["frontend/src/main.ts", "frontend/src/hash-worker.ts"]) {
    report.environment.source_sha256[path] = sha(await readFile(path));
  }
  const { stdout: packagedHashes } = await compose(
    "exec",
    "-T",
    "api",
    "python",
    "-c",
    "import hashlib,json,pathlib; print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('clouddrive').glob('*.py')}))",
  );
  const imageSources = JSON.parse(packagedHashes);
  for (const [path, digest] of Object.entries(imageSources)) {
    assert.equal(
      report.environment.source_sha256[path],
      digest,
      `Rebuild required: packaged ${path} differs from host source`,
    );
  }
  assert.equal(
    new Set(
      containers
        .filter((c) =>
          ["api", "worker", "beat"].includes(
            c.Config.Labels["com.docker.compose.service"],
          ),
        )
        .map((c) => c.Image),
    ).size,
    1,
  );
  report.environment.packaged_python_source_matches = true;
  await save();
  for (let pass = 1; pass <= passes; pass++) {
    const record = {
      pass,
      started_at: new Date().toISOString(),
      cases: [],
      status: "running",
    };
    report.passes.push(record);
    const reportCase = async (result) => {
      try {
        validate(result);
      } catch (error) {
        result.status = "failed";
        result.validation_error = safeError(error);
      }
      record.cases.push(result);
      console.log(
        JSON.stringify({
          pass,
          case: result.id,
          status: result.status,
          recovery_s: result.recovery_s,
          error: result.error || result.validation_error,
        }),
      );
      await save();
    };
    const uploadIds = selected.filter((id) => Number(id.slice(1)) <= 9);
    if (uploadIds.length)
      await runUploadCases({
        baseURL,
        reportCase,
        runId: `${report.run_id}-p${pass}`,
        caseIds: uploadIds,
      });
    for (const id of selected.filter((id) => Number(id.slice(1)) >= 10)) {
      const backendOutput = join(
        dirname(output),
        `${report.run_id}-p${pass}-${id}.json`,
      );
      let failure;
      try {
        await run(
          python,
          [
            "scripts/recovery_backend.py",
            "--case",
            id,
            "--output",
            backendOutput,
            "--project",
            project,
            "--base-url",
            baseURL,
            "--control-dir",
            controlDir,
          ],
          { env, timeout: 300_000, maxBuffer: 2 * 1024 * 1024 },
        );
      } catch (error) {
        failure = safeError(error);
      }
      let result;
      try {
        result = JSON.parse(await readFile(backendOutput, "utf8"));
      } catch {
        result = {
          id,
          status: "failed",
          fault_observed: false,
          error: failure || "Backend case produced no result",
        };
      }
      if (failure && result.status === "passed") {
        result.status = "failed";
        result.error = failure;
      }
      await reportCase(result);
    }
    assert.deepEqual(
      record.cases.map((c) => c.id).sort(),
      [...selected].sort(),
    );
    record.status = record.cases.every((c) => c.status === "passed")
      ? "passed"
      : "failed";
    record.finished_at = new Date().toISOString();
    await save();
  }
  const attempts = report.passes.flatMap((p) => p.cases);
  const changedSources = [];
  for (const [path, digest] of Object.entries(
    report.environment.source_sha256,
  )) {
    if (sha(await readFile(path)) !== digest) changedSources.push(path);
  }
  report.sources_unchanged_during_run = changedSources.length === 0;
  report.changed_sources = changedSources;
  assert.equal(
    sha(await readFile(manifestPath)),
    report.manifest_sha256,
    "Manifest changed during the measured run",
  );
  assert.deepEqual(
    changedSources,
    [],
    "Source changed during the measured run",
  );
  assert.equal(
    await git("rev-parse", "HEAD"),
    report.environment.git_sha,
    "Commit changed during the measured run",
  );
  report.passed_attempts = attempts.filter((c) => c.status === "passed").length;
  report.attempted_cases = attempts.length;
  report.distinct_cases = new Set(attempts.map((c) => c.id)).size;
  report.status =
    attempts.length === report.expected_attempts &&
    report.passes.every((p) => p.status === "passed")
      ? "passed"
      : "failed";
  report.release_gate_passed =
    fullMatrix &&
    passes === 3 &&
    report.status === "passed" &&
    attempts.length === 60;
} catch (error) {
  report.status = "failed";
  report.error = safeError(error);
} finally {
  report.finished_at = new Date().toISOString();
  await save();
  await fd.close();
}
console.log(
  JSON.stringify({
    status: report.status,
    passed_attempts: report.passed_attempts,
    expected_attempts: report.expected_attempts,
    release_gate_passed: report.release_gate_passed,
    output,
  }),
);
if (report.status !== "passed") process.exitCode = 1;
