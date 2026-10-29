import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import { join } from "node:path";
import test from "node:test";
import { hashFile } from "./load-file-hash.mjs";

async function withDirectory(check) {
  const directory = await mkdtemp(join(os.tmpdir(), "clouddrive-hash-test-"));
  try {
    await check(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

test("hashes multiple read buffers and a short tail with exact byte count", async () => {
  await withDirectory(async (directory) => {
    const data = Buffer.alloc(2 * 1024 * 1024 + 17);
    for (let i = 0; i < data.length; i++) data[i] = (i * 31 + 7) & 255;
    const file = join(directory, "two-buffers-and-tail.bin");
    await writeFile(file, data);
    assert.deepEqual(await hashFile(file), {
      bytes: data.length,
      sha256: createHash("sha256").update(data).digest("hex"),
    });
  });
});

test("empty file returns the SHA-256 empty digest and zero bytes", async () => {
  await withDirectory(async (directory) => {
    const file = join(directory, "empty.bin");
    await writeFile(file, "");
    assert.deepEqual(await hashFile(file), {
      bytes: 0,
      sha256:
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    });
  });
});

test("missing file remains an explicit read failure", async () => {
  await withDirectory(async (directory) => {
    await assert.rejects(hashFile(join(directory, "missing.bin")), {
      code: "ENOENT",
    });
  });
});

test("a directory is rejected rather than hashed as an empty file", async () => {
  await withDirectory(async (directory) => {
    await assert.rejects(hashFile(directory), { code: "EISDIR" });
  });
});
