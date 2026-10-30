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
  await run(process.env.CLOUDDRIVE_TEST_PYTHON || ".venv/bin/python", [
    "-c",
    `
from uuid import uuid4
from sqlalchemy.engine import make_url
from clouddrive.db import engine
from clouddrive.config import settings
from clouddrive.service import query
assert make_url(settings().database_url).database.endswith('_test')
assert settings().s3_bucket.endswith('-test')
with engine().begin() as conn:
 for n in range(205):
  id=uuid4()
  query(conn, "INSERT INTO files(id,workspace_id,request_id,name,size,media_type,expected_sha256,part_size,object_key,status,created_at) VALUES (:id,:ws,:request,:name,1,'text/plain',:sha,8388608,:key,'EXPIRED',now()-(:n * interval '1 second'))", id=id,ws='${workspace}',request=uuid4(),name=f'past-{n:03}.txt',sha='0'*64,key=f'${workspace}/{id}',n=n)
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
  await page.locator("#search").fill("past-204");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await expect(page.locator("tbody tr")).toContainText("past-204.txt");
  await page.locator("#search").fill("");
  await expect(page.locator("tbody tr")).toHaveCount(50);
  await expect(page.locator("#load-more")).toBeVisible();
});
