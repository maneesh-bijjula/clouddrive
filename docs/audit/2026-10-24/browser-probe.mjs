// Historical baseline 6f439d9 probe. Intentionally asserts pre-fix behavior; do not run against Sprint 1.
import { chromium, expect } from "@playwright/test";
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const context = await browser.newContext({
    baseURL: "http://localhost:18090",
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
  let failPut = true;
  await page.route(/.*\?.*partNumber=.*/, (route) =>
    failPut && route.request().method() === "PUT"
      ? route.abort("failed")
      : route.continue(),
  );
  const file = {
    name: "audit-interrupted-delete.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Original audit content\n"),
  };
  await page.locator("#file-input").setInputFiles(file);
  await expect(page.locator("#notice")).toContainText(
    "Upload connection interrupted",
  );
  const row = page.locator("tbody tr", { hasText: file.name });
  await expect(row).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await row
    .getByRole("button", { name: `Delete ${file.name}`, exact: true })
    .click();
  await expect(row).toHaveCount(0);
  failPut = false;
  const response = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/uploads" &&
      r.request().method() === "POST",
  );
  await page.locator("#file-input").setInputFiles(file);
  const result = await response;
  expect(result.status()).toBe(409);
  await expect(page.locator("#notice")).toContainText(
    "This upload was deleted; use a new request ID",
  );
  console.log(
    "CONFIRMED: interrupt -> delete in UI -> reselect identical file reuses deleted request ID and returns 409.",
  );
  await page.screenshot({
    path: "docs/audit/2026-10-24/interrupted-delete.png",
    fullPage: true,
  });
  await context.close();
} finally {
  await browser.close();
}
