import { sha256 } from "@noble/hashes/sha2.js";

// One 8 MiB read at a time. The UI can terminate this worker on pause or switch.
self.onmessage = async (event: MessageEvent<{ file: File }>) => {
  try {
    const file = event.data.file;
    const digest = sha256.create();
    for (let offset = 0; offset < file.size; offset += 8 * 1024 * 1024) {
      const end = Math.min(file.size, offset + 8 * 1024 * 1024);
      digest.update(
        new Uint8Array(await file.slice(offset, end).arrayBuffer()),
      );
      self.postMessage({ progress: end / file.size });
    }
    self.postMessage({
      hash: Array.from(digest.digest(), (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join(""),
    });
  } catch {
    self.postMessage({
      error: "Could not read this file. Select it again to retry.",
    });
  }
  self.close();
};
