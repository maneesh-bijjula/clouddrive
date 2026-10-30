import { test, expect } from "@playwright/test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

test("browses more than 200 files and searches the whole workspace", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
  const workspace = await page.evaluate(
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
  await run(process.env.CLOUDDRIVE_TEST_PYTHON || "python", [
    "-c",
    `
import os
from uuid import uuid4
os.environ.setdefault('CLOUDDRIVE_DATABASE_URL', 'postgresql+psycopg://clouddrive:clouddrive@localhost:5436/clouddrive_test')
os.environ.setdefault('CLOUDDRIVE_S3_BUCKET', 'clouddrive-test')
from sqlalchemy.engine import make_url
from clouddrive.db import engine
from clouddrive.config import settings
from clouddrive.service import query
assert make_url(settings().database_url).database.endswith('_test')
assert settings().s3_bucket.endswith('-test')
with engine().begin() as conn:
 for n in range(205):
  id=uuid4()
  query(conn, "INSERT INTO files(id,workspace_id,request_id,name,size,media_type,expected_sha256,part_size,object_key,status,created_at) VALUES (:id,:ws,:request,:name,:size,'text/plain',:sha,8388608,:key,'EXPIRED',now()-(:n * interval '1 second'))", id=id,ws='${workspace}',request=uuid4(),name=f'past-{n:03}.txt',sha='0'*64,key=f'${workspace}/{id}',n=n,size=n+1)
`,
  ]);
  await page
    .getByRole("button", { name: "Refresh files", exact: true })
    .click();
  await expect(page.locator("tbody tr")).toHaveCount(50);
  for (const count of [100, 150, 200, 205]) {
    await page
      .getByRole("button", { name: "Load more files", exact: true })
      .click();
    await expect(page.locator("tbody tr")).toHaveCount(count);
  }
  await expect(page.locator("#load-more")).toBeHidden();
  await page.locator("#sort").selectOption("size");
  await expect(page.locator("tbody tr")).toHaveCount(50);
  await expect(page.locator("tbody tr").first()).toContainText("past-204.txt");
  await page.locator("#sort").selectOption("name");
  await expect(page.locator("tbody tr").first()).toContainText("past-000.txt");
  await page.locator("#search").fill("past-204");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await expect(page.locator("tbody tr")).toContainText("past-204.txt");
  await page.locator("#search").fill("");
  await expect(page.locator("tbody tr")).toHaveCount(50);
  await expect(page.locator("#load-more")).toBeVisible();
});

test("a late old listing cannot overwrite a newer refresh of the same workspace", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.evaluate(() => {
    const original = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await original(...args);
      const read = response.json.bind(response);
      response.json = async () => {
        const value = await read();
        if (value.stats?.total === 987) (window as any).__oldListingRead = true;
        return value;
      };
      return response;
    };
  });
  let captured = false;
  await page.route(/\/api\/files(?:\?.*)?$/, async (route) => {
    if (!captured) {
      captured = true;
      const response = await route.fetch();
      const result = await response.json();
      result.stats.total = 987;
      await held;
      await route.fulfill({ response, json: result });
    } else await route.continue();
  });
  try {
    await page
      .getByRole("button", { name: "Refresh files", exact: true })
      .click();
    await expect.poll(() => captured).toBe(true);
    await page
      .getByRole("button", { name: "Refresh files", exact: true })
      .click();
    await expect(page.locator("#load-more")).toBeEnabled();
    const completed = page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/files",
    );
    release();
    await completed;
    await page.waitForFunction(() => (window as any).__oldListingRead === true);
    await expect(page.locator("#nav-count")).toHaveText("0");
  } finally {
    release();
  }
});
