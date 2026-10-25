import { test, expect, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFile } from "node:fs/promises";

const run = promisify(execFile);
const content = Buffer.from("Sprint 1 original content\n");
const file = {
  name: "restart-content.txt",
  mimeType: "text/plain",
  buffer: content,
};

async function open(page: Page) {
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
}
async function inventory(page: Page) {
  return page.evaluate(
    async () =>
      (
        await (
          await fetch("/api/files", {
            headers: {
              Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
            },
          })
        ).json()
      ).files,
  );
}
async function interrupt(page: Page) {
  await page.route(/.*\?.*partNumber=.*/, (route) =>
    route.request().method() === "PUT"
      ? route.abort("failed")
      : route.continue(),
  );
  await page.locator("#file-input").setInputFiles(file);
  await expect(page.locator("#notice")).toContainText(
    "Upload connection interrupted",
  );
  await expect(page.locator("tbody tr", { hasText: file.name })).toBeVisible();
  return (await inventory(page))[0];
}
async function verifiedDownload(page: Page) {
  const row = page.locator("tbody tr", { hasText: file.name });
  await expect(row.getByText("Verified", { exact: true })).toBeVisible();
  await row.getByText(file.name, { exact: true }).click();
  await expect(page.locator(".hash")).toHaveText(
    createHash("sha256").update(content).digest("hex"),
  );
  const event = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download file", exact: true })
    .click();
  expect(await readFile((await (await event).path())!)).toEqual(content);
}

test("interrupted upload can be deleted and uploaded again with a fresh request ID", async ({
  page,
}) => {
  await open(page);
  const original = await interrupt(page);
  const originalRequests: string[] = await page.evaluate(() =>
    Object.keys(localStorage).filter((k) =>
      k.startsWith("clouddrive.request."),
    ),
  );
  expect(originalRequests).toHaveLength(1);
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: `Delete ${file.name}`, exact: true })
    .click();
  await expect(page.locator("tbody tr")).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      Object.keys(localStorage).filter((k) =>
        k.startsWith("clouddrive.request."),
      ),
    ),
  ).toEqual([]);
  await page.unrouteAll();
  await page.locator("#file-input").setInputFiles(file);
  await verifiedDownload(page);
  const files = await inventory(page);
  expect(files).toHaveLength(1);
  expect(files[0].id).not.toBe(original.id);
});

for (const reload of [false, true])
  test(`a deleted upload remembered by another tab is replaced ${reload ? "after reload" : "without reload"}`, async ({
    page,
  }) => {
    await open(page);
    const original = await interrupt(page);
    // Simulate deletion in another browser: this tab retains its fingerprint.
    await page.evaluate(async (id) => {
      const response = await fetch(`/api/files/${id}`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
        },
      });
      if (response.status !== 204) throw new Error("test deletion failed");
    }, original.id);
    if (reload) {
      await page.reload();
      await expect(page.locator("tbody tr")).toHaveCount(0);
    }
    await page.unrouteAll();
    await page.locator("#file-input").setInputFiles(file);
    await verifiedDownload(page);
    expect((await inventory(page))[0].id).not.toBe(original.id);
  });

test("expired storage becomes terminal and Restart verifies the reselected original file", async ({
  page,
}) => {
  await open(page);
  const original = await interrupt(page);
  await run(
    process.env.CLOUDDRIVE_TEST_PYTHON || "python",
    ["scripts/expire-test-upload.py", original.id],
    { env: { ...process.env, PYTHONPATH: "." } },
  );
  await page.unrouteAll();
  await page.locator("#file-input").setInputFiles(file);
  const row = page.locator("tbody tr", { hasText: file.name });
  await expect(row.getByText("Upload expired", { exact: true })).toBeVisible();
  // Wrong content never replaces the original session or starts storage work.
  await row
    .getByRole("button", { name: `Restart ${file.name}`, exact: true })
    .click();
  await page
    .locator("#file-input")
    .setInputFiles({ ...file, buffer: Buffer.from("different bytes") });
  await expect(page.locator("#notice")).toContainText(
    "This is a different file",
  );
  expect((await inventory(page))[0].id).toBe(original.id);
  await row
    .getByRole("button", { name: `Restart ${file.name}`, exact: true })
    .click();
  await page.locator("#file-input").setInputFiles(file);
  await verifiedDownload(page);
  const files = await inventory(page);
  expect(files).toHaveLength(1);
  expect(files[0].id).not.toBe(original.id);
});

test("duplicate file selection keeps one active upload and one storage transfer", async ({
  page,
}) => {
  await open(page);
  let release!: () => void;
  const hold = new Promise<void>((resolve) => (release = resolve));
  let puts = 0;
  await page.route(/.*\?.*partNumber=.*/, async (route) => {
    if (route.request().method() === "PUT") {
      puts++;
      await hold;
    }
    await route.continue().catch(() => {});
  });
  try {
    await page.locator("#file-input").setInputFiles(file);
    await expect.poll(() => puts).toBe(1);
    await page.locator("#file-input").setInputFiles(file);
    await expect(page.locator("#notice")).toContainText(
      "already has an active upload",
    );
    expect(await inventory(page)).toHaveLength(1);
  } finally {
    release();
  }
  await verifiedDownload(page);
  expect(puts).toBe(1);
});

test("workspace switch isolates saved requests and an old in-flight response", async ({
  page,
  request,
}) => {
  await open(page);
  const originalWorkspace = await page.evaluate(
    async () =>
      (
        await (
          await fetch("/api/workspace", {
            headers: {
              Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
            },
          })
        ).json()
      ).id,
  );
  const second = await (await request.post("/api/workspaces")).json();
  const initiations: { key: string; request: string }[] = [];
  let release!: () => void;
  const hold = new Promise<void>((resolve) => (release = resolve));
  let held = false;
  await page.route("**/api/uploads", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    initiations.push({
      key: route.request().headers()["authorization"],
      request: route.request().postDataJSON().request_id,
    });
    if (initiations.length === 1) {
      const response = await route.fetch();
      held = true;
      await hold;
      await route.fulfill({ response }).catch(() => {});
    } else await route.continue();
  });
  await page.locator("#file-input").setInputFiles(file);
  try {
    await expect.poll(() => held).toBe(true);
    await page
      .getByRole("button", { name: `Pause ${file.name}`, exact: true })
      .click();
    await page.getByRole("button", { name: "Manage access" }).click();
    await page.locator("#key-input").setInputFiles({
      name: "access.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          application: "clouddrive",
          access_key: second.access_key,
        }),
      ),
    });
    await expect(page.locator("#workspace-id")).toHaveText(
      `Workspace ${second.id.slice(0, 8)}`,
    );
    await expect(page.locator("tbody tr")).toHaveCount(0);
  } finally {
    release();
  }
  // Wait for the old response to settle before beginning the new transfer.
  await expect(page.locator("#transfer-panel")).toBeHidden();
  await page.locator("#file-input").setInputFiles(file);
  await verifiedDownload(page);
  expect(initiations).toHaveLength(2);
  expect(initiations[0].key).not.toBe(initiations[1].key);
  expect(initiations[0].request).not.toBe(initiations[1].request);
  const oldKeys = await page.evaluate(
    (id) =>
      Object.keys(localStorage).filter((k) =>
        k.startsWith(`clouddrive.request.${id}.`),
      ),
    originalWorkspace,
  );
  expect(oldKeys).toHaveLength(1);
  const oldState = await request.get("/api/files", {
    headers: { Authorization: initiations[0].key },
  });
  expect((await oldState.json()).files[0].status).toBe("UPLOADING");
});
