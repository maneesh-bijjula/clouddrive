import { test, expect, type Page } from "@playwright/test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
const run = promisify(execFile);
const bytes = Buffer.from("Retry and verify these exact original bytes.\n");
const file = { name: "retry.txt", mimeType: "text/plain", buffer: bytes };
async function open(page: Page) {
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
  await expect(page.locator("#upload-limit")).toContainText("5 GB");
}
async function verify(page: Page) {
  const row = page.locator("tbody tr", { hasText: file.name });
  await expect(row.getByText("Verified", { exact: true })).toBeVisible();
  await row.getByText(file.name, { exact: true }).click();
  await expect(page.locator(".hash")).toHaveText(
    createHash("sha256").update(bytes).digest("hex"),
  );
  const event = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download file", exact: true })
    .click();
  expect(await readFile((await (await event).path())!)).toEqual(bytes);
}

test("a transient signing failure retries automatically", async ({ page }) => {
  await open(page);
  let signs = 0;
  await page.route("**/parts/1/sign", async (route) => {
    if (++signs === 1)
      return route.fulfill({
        status: 503,
        json: { detail: "Temporary storage outage" },
      });
    await route.continue();
  });
  await page.locator("#file-input").setInputFiles(file);
  await verify(page);
  expect(signs).toBe(2);
});

for (const boundary of ["storage-reply", "acknowledgement-reply"])
  test(`a lost ${boundary} is reconciled without a second PUT`, async ({
    page,
  }) => {
    await open(page);
    let puts = 0,
      signs = 0,
      lost = false;
    page.on("request", (r) => {
      if (
        r.method() === "PUT" &&
        new URL(r.url()).searchParams.has("partNumber")
      )
        puts++;
      if (r.url().endsWith("/parts/1/sign")) signs++;
    });
    const pattern =
      boundary === "storage-reply" ? /.*\?.*partNumber=.*/ : "**/parts/1";
    await page.route(pattern, async (route) => {
      if (route.request().method() !== "PUT" || lost) return route.continue();
      lost = true;
      const result = await route.fetch();
      expect(result.ok()).toBe(true);
      await route.abort("failed");
    });
    await page.locator("#file-input").setInputFiles(file);
    await verify(page);
    expect(puts).toBe(1);
    expect(signs).toBe(2);
  });

test("a genuinely expired S3 URL is re-signed without expiring the upload", async ({
  page,
}) => {
  await open(page);
  let signs = 0;
  const storageStatuses: number[] = [];
  page.on("response", (r) => {
    if (new URL(r.url()).searchParams.has("partNumber"))
      storageStatuses.push(r.status());
  });
  await page.route("**/parts/1/sign", async (route) => {
    if (++signs !== 1) return route.continue();
    const response = await route.fetch();
    const signed = await response.json();
    const id = new URL(route.request().url()).pathname.split("/")[3];
    const { stdout } = await run(
      process.env.CLOUDDRIVE_TEST_PYTHON || "python",
      [
        "scripts/expire-test-upload.py",
        id,
        "--expired-part",
        "1",
        "--checksum",
        route.request().postDataJSON().checksum,
      ],
      { env: { ...process.env, PYTHONPATH: "." } },
    );
    signed.url = JSON.parse(stdout).url;
    await route.fulfill({ response, json: signed });
  });
  await page.locator("#file-input").setInputFiles(file);
  await verify(page);
  expect(signs).toBe(2);
  expect(storageStatuses).toEqual([403, 200]);
});

test("a permanent storage rejection is not retried", async ({ page }) => {
  await open(page);
  let puts = 0;
  await page.route(/.*\?.*partNumber=.*/, (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    puts++;
    return route.fulfill({
      status: 403,
      contentType: "application/xml",
      body: "<Error><Code>AccessDenied</Code><Message>Not authorized</Message></Error>",
    });
  });
  await page.locator("#file-input").setInputFiles(file);
  await expect(page.locator("#notice")).toContainText(
    "Storage upload failed (403)",
  );
  expect(puts).toBe(1);
});

test("transient failures stop after four attempts and can then resume", async ({
  page,
}) => {
  await open(page);
  let puts = 0;
  await page.route(/.*\?.*partNumber=.*/, (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    puts++;
    return route.abort("failed");
  });
  await page.locator("#file-input").setInputFiles(file);
  await expect(page.locator("#notice")).toContainText(
    "Upload connection interrupted",
  );
  expect(puts).toBe(4);
  await page.unrouteAll();
  await page.locator("#file-input").setInputFiles(file);
  await verify(page);
});

test("pause cancels a hashing worker before creating an upload", async ({
  page,
}) => {
  await open(page);
  let release!: () => void;
  const hold = new Promise<void>((r) => (release = r));
  let held = false,
    initiations = 0;
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/uploads" && r.method() === "POST")
      initiations++;
  });
  await page.route("**/hash-worker-*.js", async (route) => {
    held = true;
    await hold;
    await route.continue().catch(() => {});
  });
  try {
    await page.locator("#file-input").setInputFiles(file);
    await expect.poll(() => held).toBe(true);
    await page
      .getByRole("button", { name: `Pause ${file.name}`, exact: true })
      .click();
    await expect(
      page.getByText("Paused · Select file to resume", { exact: true }),
    ).toBeVisible();
    expect(initiations).toBe(0);
  } finally {
    release();
  }
});

test("upload can start after workspace configuration while the first listing is pending", async ({
  page,
}) => {
  let release!: () => void;
  const hold = new Promise<void>((r) => (release = r));
  let first = true,
    initiations = 0;
  await page.route(/\/api\/files(?:\?.*)?$/, async (route) => {
    if (first) {
      first = false;
      await hold;
    }
    await route.continue().catch(() => {});
  });
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/uploads" && r.method() === "POST")
      initiations++;
  });
  try {
    await open(page);
    await page.locator("#file-input").setInputFiles(file);
    await expect.poll(() => initiations).toBe(1);
  } finally {
    release();
  }
  await verify(page);
});

test("the last multipart part can finish before the first two parts", async ({
  page,
}) => {
  await open(page);
  let release!: () => void;
  const hold = new Promise<void>((r) => (release = r));
  const content = Buffer.alloc(16 * 1024 * 1024 + 17, 113);
  await page.route(/.*\?.*partNumber=.*/, async (route) => {
    if (
      route.request().method() === "PUT" &&
      Number(new URL(route.request().url()).searchParams.get("partNumber")) !==
        3
    )
      await hold;
    await route.continue().catch(() => {});
  });
  try {
    const third = page.waitForResponse(
      (r) =>
        r.url().endsWith("/parts/3") &&
        r.request().method() === "PUT" &&
        r.status() === 200,
    );
    await page.locator("#file-input").setInputFiles({
      name: "sparse.bin",
      mimeType: "application/octet-stream",
      buffer: content,
    });
    await third;
  } finally {
    release();
  }
  const row = page.locator("tbody tr", { hasText: "sparse.bin" });
  await expect(row.getByText("Verified", { exact: true })).toBeVisible();
  await row.getByText("sparse.bin", { exact: true }).click();
  await expect(page.locator(".hash")).toHaveText(
    createHash("sha256").update(content).digest("hex"),
  );
});
