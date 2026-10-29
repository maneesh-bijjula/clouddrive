import { test, expect, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { UploadScheduler } from "../frontend/src/scheduler";

const storage = /.*\?.*partNumber=.*/;
const fixture = (number: number) => ({
  name: `parallel-${number}.txt`,
  mimeType: "text/plain",
  buffer: Buffer.from(
    `Concurrent file ${number}: preserve these exact bytes.\n`,
  ),
});
const gate = () => {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => (release = resolve));
  return { promise, release };
};
async function open(page: Page) {
  await page.goto("/");
  await expect(page.locator("#workspace-id")).toHaveText(
    /Workspace [a-f0-9]{8}/,
  );
  await expect(page.locator("#upload-limit")).toContainText("5 GB");
}
async function inventory(page: Page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/files", {
      headers: {
        Authorization: `Bearer ${localStorage.getItem("clouddrive.access")}`,
      },
    });
    return (await response.json()).files;
  });
}
async function verified(page: Page, numbers: number[]) {
  for (const number of numbers)
    await expect(
      page
        .locator("tbody tr", { hasText: fixture(number).name })
        .getByText("Verified", { exact: true }),
    ).toBeVisible();
  const files = await inventory(page);
  expect(files).toHaveLength(numbers.length);
  for (const number of numbers) {
    const source = fixture(number);
    const file = files.find((item: any) => item.name === source.name);
    expect(file.status).toBe("READY");
    expect(file.size).toBe(source.buffer.length);
    expect(file.sha256).toBe(
      createHash("sha256").update(source.buffer).digest("hex"),
    );
  }
}

test("scheduler reserves a first part for every admitted file and cancels queued work", async () => {
  const scheduler = new UploadScheduler();
  const controllers = Array.from({ length: 11 }, () => new AbortController());
  const files = await Promise.all(
    controllers
      .slice(0, 10)
      .map((controller, index) =>
        scheduler.acquireFile(String(index), controller.signal),
      ),
  );
  const queuedFile = scheduler.acquireFile("10", controllers[10].signal);
  const cancelledFile = expect(queuedFile).rejects.toMatchObject({
    name: "AbortError",
  });
  controllers[10].abort();
  await cancelledFile;
  const first = await scheduler.acquirePart("0", controllers[0].signal);
  const extras = [
    scheduler.acquirePart("0", controllers[0].signal),
    scheduler.acquirePart("0", controllers[0].signal),
  ];
  const cancelledExtras = extras.map((promise) =>
    expect(promise).rejects.toMatchObject({ name: "AbortError" }),
  );
  const others = await Promise.all(
    controllers
      .slice(1, 10)
      .map((controller, index) =>
        scheduler.acquirePart(String(index + 1), controller.signal),
      ),
  );
  expect(scheduler.snapshot()).toMatchObject({
    active_files: 10,
    queued_files: 0,
    global_parts: 10,
    queued_parts: 2,
  });
  controllers[0].abort();
  await Promise.all(cancelledExtras);
  first();
  first(); // A duplicate finally/release cannot increase capacity.
  files[0]();
  others.forEach((release) => release());
  files.slice(1).forEach((release) => release());
  expect(scheduler.snapshot()).toEqual({
    active_files: 0,
    queued_files: 0,
    hash_workers: 0,
    queued_hashes: 0,
    global_parts: 0,
    queued_parts: 0,
  });
});

test("scheduler bounds hashing and each file's part ownership", async () => {
  const scheduler = new UploadScheduler();
  const controller = new AbortController();
  const file = await scheduler.acquireFile("one", controller.signal);
  const hashes = await Promise.all([
    scheduler.acquireHash(controller.signal),
    scheduler.acquireHash(controller.signal),
  ]);
  const waitingHash = scheduler.acquireHash(controller.signal);
  const parts = await Promise.all(
    Array.from({ length: 3 }, () =>
      scheduler.acquirePart("one", controller.signal),
    ),
  );
  const waitingPart = scheduler.acquirePart("one", controller.signal);
  expect(scheduler.snapshot()).toMatchObject({
    hash_workers: 2,
    queued_hashes: 1,
    global_parts: 3,
    queued_parts: 1,
  });
  const hashRejected = expect(waitingHash).rejects.toMatchObject({
    name: "AbortError",
  });
  const partRejected = expect(waitingPart).rejects.toMatchObject({
    name: "AbortError",
  });
  controller.abort();
  await Promise.all([hashRejected, partRejected]);
  hashes.forEach((release) => release());
  parts.forEach((release) => release());
  file();
  expect(scheduler.snapshot()).toMatchObject({
    active_files: 0,
    hash_workers: 0,
    queued_hashes: 0,
    global_parts: 0,
    queued_parts: 0,
  });
});

test("one selection starts ten files without exceeding the global part budget", async ({
  page,
}) => {
  await open(page);
  const hold = gate();
  const uploads = new Set<string>();
  await page.route(storage, async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    uploads.add(new URL(route.request().url()).pathname);
    await hold.promise;
    await route.continue().catch(() => {});
  });
  try {
    await page
      .locator("#file-input")
      .setInputFiles(
        Array.from({ length: 10 }, (_, index) => fixture(index + 1)),
      );
    await expect.poll(() => uploads.size).toBe(10);
    await expect(page.locator("#transfer-panel")).toHaveAttribute(
      "data-global_parts",
      "10",
    );
    await expect(page.locator("#transfer-panel")).toHaveAttribute(
      "data-active_files",
      "10",
    );
  } finally {
    hold.release();
  }
  await verified(
    page,
    Array.from({ length: 10 }, (_, index) => index + 1),
  );
  await expect(page.locator("#transfer-panel")).toHaveAttribute(
    "data-global_parts",
    "0",
  );
});

test("pausing queued and active files frees capacity without starting the paused selection", async ({
  page,
  request,
}) => {
  await open(page);
  const hold = gate();
  const uploads = new Set<string>();
  const initiated: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname === "/api/uploads")
      initiated.push(r.postDataJSON().name);
  });
  await page.route(storage, async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    uploads.add(new URL(route.request().url()).pathname);
    await hold.promise;
    await route.continue().catch(() => {});
  });
  try {
    await page
      .locator("#file-input")
      .setInputFiles(
        Array.from({ length: 12 }, (_, index) => fixture(index + 1)),
      );
    await expect.poll(() => uploads.size).toBe(10);
    const queued = page.locator(".transfer", { hasText: fixture(12).name });
    await expect(queued).toContainText("Queued");
    await page
      .getByRole("button", { name: `Pause ${fixture(12).name}`, exact: true })
      .click();
    await expect(queued).toContainText("Paused · Select file to resume");
    await page
      .getByRole("button", { name: `Pause ${fixture(1).name}`, exact: true })
      .click();
    const first = (await inventory(page)).find(
      (file: any) => file.name === fixture(1).name,
    );
    const key = await page.evaluate(() =>
      localStorage.getItem("clouddrive.access"),
    );
    // Pausing keeps durable upload metadata and therefore consumes server quota.
    // Explicitly delete this test's paused session before admitting file eleven.
    const removed = await request.delete(`/api/files/${first.id}`, {
      headers: { Authorization: `Bearer ${key}` },
    });
    expect(removed.status()).toBe(204);
    await expect.poll(() => uploads.size).toBe(11);
    expect(initiated).not.toContain(fixture(12).name);
  } finally {
    hold.release();
  }
  await verified(
    page,
    Array.from({ length: 10 }, (_, index) => index + 2),
  );
  await expect(page.locator("#transfer-panel")).toHaveAttribute(
    "data-queued_parts",
    "0",
  );
  await expect(page.locator("#transfer-panel")).toHaveAttribute(
    "data-global_parts",
    "0",
  );
});

test("hash wait cancellation permits a new file while at most two workers run", async ({
  page,
}) => {
  await open(page);
  const hold = gate();
  let workers = 0;
  await page.route("**/hash-worker-*.js", async (route) => {
    workers++;
    await hold.promise;
    await route.continue().catch(() => {});
  });
  try {
    await page
      .locator("#file-input")
      .setInputFiles([fixture(1), fixture(2), fixture(3)]);
    await expect.poll(() => workers).toBe(2);
    await expect(
      page.locator(".transfer", { hasText: fixture(3).name }),
    ).toContainText("Waiting to check file");
    await page
      .getByRole("button", { name: `Pause ${fixture(3).name}`, exact: true })
      .click();
    await page
      .getByRole("button", { name: `Pause ${fixture(1).name}`, exact: true })
      .click();
    await page.locator("#file-input").setInputFiles(fixture(4));
    await expect.poll(() => workers).toBe(3);
    await expect(page.locator("#transfer-panel")).toHaveAttribute(
      "data-hash_workers",
      "2",
    );
  } finally {
    hold.release();
  }
  await verified(page, [2, 4]);
  await expect(page.locator("#transfer-panel")).toHaveAttribute(
    "data-hash_workers",
    "0",
  );
});

for (const boundary of ["initiation", "completion"])
  test(`bounded ${boundary} capacity retries preserve the upload for manual resume`, async ({
    page,
  }) => {
    await open(page);
    let blocked = true,
      attempts = 0;
    const requests: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && new URL(r.url()).pathname === "/api/uploads")
        requests.push(r.postDataJSON().request_id);
    });
    await page.route(
      boundary === "initiation"
        ? "**/api/uploads"
        : "**/api/uploads/*/complete",
      async (route) => {
        if (route.request().method() !== "POST" || !blocked)
          return route.continue();
        attempts++;
        await route.fulfill({
          status: 429,
          headers: { "Retry-After": "1" },
          json: {
            detail: {
              code: "upload_capacity",
              message:
                "Capacity is temporarily full. Select the same file to resume.",
            },
          },
        });
      },
    );
    await page.locator("#file-input").setInputFiles(fixture(30));
    await expect(
      page.locator(".transfer", { hasText: fixture(30).name }),
    ).toContainText("Waiting for capacity · Select file to resume");
    expect(attempts).toBe(8);
    expect(new Set(requests).size).toBe(1);
    blocked = false;
    await page.locator("#file-input").setInputFiles(fixture(30));
    await verified(page, [30]);
    expect(new Set(requests).size).toBe(1);
    expect(requests).toHaveLength(boundary === "initiation" ? 9 : 1);
    await expect(page.locator("#transfer-panel")).toBeHidden();
  });

test("workspace switching retires paused queued and hashing work", async ({
  page,
  request,
}) => {
  await open(page);
  const hold = gate();
  let block = true,
    workers = 0;
  const startedKeys: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname === "/api/uploads")
      startedKeys.push(r.headers()["authorization"]);
  });
  await page.route("**/hash-worker-*.js", async (route) => {
    workers++;
    if (block) await hold.promise;
    await route.continue().catch(() => {});
  });
  const second = await (await request.post("/api/workspaces")).json();
  try {
    await page
      .locator("#file-input")
      .setInputFiles(
        Array.from({ length: 12 }, (_, index) => fixture(index + 1)),
      );
    await expect.poll(() => workers).toBe(2);
    for (let index = 1; index <= 12; index++)
      await page
        .getByRole("button", {
          name: `Pause ${fixture(index).name}`,
          exact: true,
        })
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
    block = false;
    hold.release();
    await expect(page.locator("#transfer-panel")).toBeHidden();
    await page.locator("#file-input").setInputFiles(fixture(20));
    await verified(page, [20]);
    expect(startedKeys).toEqual([`Bearer ${second.access_key}`]);
  } finally {
    hold.release();
  }
});
