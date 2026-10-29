/** Independently hash a file using one reusable 1 MiB read buffer. */
import { createHash } from "node:crypto";
import { open } from "node:fs/promises";

export async function hashFile(path) {
  const file = await open(path, "r");
  try {
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    const digest = createHash("sha256");
    let bytes = 0;
    for (;;) {
      const { bytesRead } = await file.read(buffer, 0, buffer.length, null);
      if (!bytesRead) break;
      // update() consumes these bytes synchronously before the next read reuses
      // the same allocation. No fresh large stream chunk is retained per read.
      digest.update(buffer.subarray(0, bytesRead));
      bytes += bytesRead;
    }
    return { bytes, sha256: digest.digest("hex") };
  } finally {
    await file.close();
  }
}
