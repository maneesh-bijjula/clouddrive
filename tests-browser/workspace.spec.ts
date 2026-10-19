import { test, expect, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

async function openWorkspace(page: Page) {
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
}

async function uploadText(
  page: Page,
  name = "project-notes.txt",
  content = "A good place for important files.\n",
) {
  await page.locator("#file-input").setInputFiles({
    name,
    mimeType: "text/plain",
    buffer: Buffer.from(content),
  });
  const row = page.locator("tbody tr", { hasText: name });
  await expect(row.getByText("Verified", { exact: true })).toBeVisible();
  return row;
}

test("uploads, verifies through Celery, previews safely, and downloads identical bytes", async ({
  page,
}) => {
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await openWorkspace(page);
  const content = "CloudDrive — real bytes.\n<img src=x onerror=alert(1)>\n";
  const row = await uploadText(page, "research-notes.txt", content);
  await row.getByText("research-notes.txt", { exact: true }).click();
  await expect(page.locator(".preview")).toHaveText(content);
  await expect(page.locator(".preview img")).toHaveCount(0);
  await expect(page.locator(".hash")).toHaveText(
    createHash("sha256").update(content).digest("hex"),
  );
  const downloadEvent = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download file", exact: true })
    .click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("research-notes.txt");
  expect(await readFile((await download.path())!, "utf8")).toBe(content);
  expect(browserErrors).toEqual([]);
});

test("resumes a multipart upload after reload without resending its confirmed part", async ({
  page,
}) => {
  await openWorkspace(page);
  const content = Buffer.alloc(12 * 1024 * 1024 + 17, 97);
  let blockSecond = true;
  let release: () => void = () => {};
  const hold = new Promise<void>((resolve) => (release = resolve));
  const putNumbers: number[] = [];
  await page.route(/.*\?.*partNumber=.*/, async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const number = Number(
      new URL(route.request().url()).searchParams.get("partNumber"),
    );
    putNumbers.push(number);
    if (number === 2 && blockSecond) {
      await hold;
      await route.abort("failed").catch(() => {});
      return;
    }
    await route.continue().catch(() => {});
  });
  await page.locator("#file-input").setInputFiles({
    name: "multipart.txt",
    mimeType: "text/plain",
    buffer: content,
  });
  await expect
    .poll(async () =>
      page.evaluate(async () => {
        const response = await fetch("/api/files", {
          headers: {
            Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
          },
        });
        const files = (await response.json()).files;
        return (
          files
            .find((file: any) => file.name === "multipart.txt")
            ?.parts.filter((part: any) => part.etag).length || 0
        );
      }),
    )
    .toBe(1);
  await page
    .getByRole("button", { name: "Pause multipart.txt", exact: true })
    .click();
  await expect(
    page.getByText("Paused · Select file to resume", { exact: true }),
  ).toBeVisible();
  blockSecond = false;
  release();
  await page.reload();
  const row = page.locator("tbody tr", { hasText: "multipart.txt" });
  await expect(row.getByText("Uploading", { exact: true })).toBeVisible();
  putNumbers.length = 0;
  await row.getByRole("button", { name: "Resume multipart.txt" }).click();
  await page.locator("#file-input").setInputFiles({
    name: "multipart.txt",
    mimeType: "text/plain",
    buffer: content,
  });
  await expect(row.getByText("Verified", { exact: true })).toBeVisible();
  expect(putNumbers).toEqual([2]);
  await row.getByText("multipart.txt", { exact: true }).click();
  await expect(page.locator(".hash")).toHaveText(
    createHash("sha256").update(content).digest("hex"),
  );
});

test("exports workspace access, reopens it in a fresh browser, searches, and deletes", async ({
  page,
  browser,
}) => {
  await openWorkspace(page);
  await uploadText(page, "access-test.txt");
  await page.getByRole("button", { name: "Manage access" }).click();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export access key" }).click();
  const file = await downloadEvent;
  const access = await readFile((await file.path())!);
  const context = await browser.newContext();
  const second = await context.newPage();
  await openWorkspace(second);
  await expect(second.locator("tbody tr")).toHaveCount(0);
  await second.getByRole("button", { name: "Manage access" }).click();
  await second.locator("#key-input").setInputFiles({
    name: "access.json",
    mimeType: "application/json",
    buffer: access,
  });
  const row = second.locator("tbody tr", { hasText: "access-test.txt" });
  await expect(row).toBeVisible();
  await second.getByRole("searchbox").fill("no match");
  await expect(second.locator("tbody tr")).toHaveCount(0);
  await second.getByRole("searchbox").fill("access-test");
  await expect(row).toBeVisible();
  second.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Delete access-test.txt" }).click();
  await expect(second.locator("tbody tr")).toHaveCount(0);
  await context.close();
});

test("mobile workspace fits the screen and supports upload and file details", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openWorkspace(page);
  const row = await uploadText(page, "mobile-notes.txt");
  await expect(page.locator("#stat-files")).toHaveText("1 file");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("button", { name: "Workspace access", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Export access key", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Close access settings", exact: true })
    .click();
  await page.screenshot({ path: "test-results/mobile.png", fullPage: true });
  await row.getByText("mobile-notes.txt", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Download file", exact: true }),
  ).toBeVisible();
});

test("validates empty uploads and renders a real populated workspace", async ({
  page,
}) => {
  await openWorkspace(page);
  await page.locator("#file-input").setInputFiles({
    name: "empty.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(0),
  });
  await expect(page.getByRole("status")).toContainText(
    "Empty files are not supported",
  );
  await uploadText(
    page,
    "Architecture notes.md",
    "# CloudDrive\n\nDirect multipart uploads. Durable processing. Verified bytes.\n",
  );
  await uploadText(
    page,
    "Product brief.txt",
    "Keep your files close. Keep your workflow moving.\n",
  );
  await uploadText(
    page,
    "sample-data.json",
    '{"project":"CloudDrive","storage":"private","integrity":"SHA-256"}\n',
  );
  await page
    .locator("#notice")
    .evaluate((element) => ((element as HTMLElement).hidden = true));
  await page.screenshot({ path: "test-results/workspace.png", fullPage: true });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
