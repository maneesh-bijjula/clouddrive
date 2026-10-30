import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const args = JSON.parse(input);
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ acceptDownloads: true });
  await page.goto(args.base);
  await page.waitForFunction(() =>
    /^Workspace [a-f0-9]{8}$/.test(
      document.querySelector("#workspace-id").textContent,
    ),
  );
  await page
    .getByRole("button", { name: "Manage access", exact: true })
    .click();
  await page.locator("#key-input").setInputFiles({
    name: "access.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify({ application: "clouddrive", access_key: args.key }),
    ),
  });
  await page.waitForFunction(
    (id) =>
      document.querySelector("#workspace-id").textContent ===
      `Workspace ${id.slice(0, 8)}`,
    args.workspace,
  );
  await page
    .locator("tbody tr", { hasText: "ready-0.bin" })
    .getByText("ready-0.bin", { exact: true })
    .click();
  const downloaded = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download file", exact: true })
    .click();
  const bytes = await readFile(await (await downloaded).path());
  if (createHash("sha256").update(bytes).digest("hex") !== args.ready_sha256)
    throw new Error("Restored browser download mismatch");
  await page.locator("#detail-dialog [data-close]").click();
  await page
    .getByRole("button", { name: "Restart unfinished.bin", exact: true })
    .click();
  await page.locator("#file-input").setInputFiles({
    name: "unfinished.bin",
    mimeType: "application/octet-stream",
    buffer: Buffer.from(args.unfinished_base64, "base64"),
  });
  await page
    .locator("tbody tr", { hasText: "unfinished.bin" })
    .getByText("Verified", { exact: true })
    .waitFor({ timeout: 120000 });
  process.stdout.write(
    JSON.stringify({
      original_key_import: "passed",
      restored_download: "passed",
      unfinished_restart: "passed",
    }) + "\n",
  );
} catch {
  // Avoid leaking bearer keys or signed URLs through Playwright diagnostics.
  process.stderr.write("Packaged restore browser verification failed\n");
  process.exitCode = 1;
} finally {
  await browser.close();
}
