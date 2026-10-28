/** R01-R09: real packaged browser recovery, with explicit transport faults.
 * Called by recovery-matrix.mjs; never prints bearer keys or signed URLs.
 */
import assert from "node:assert/strict";
import { chromium, expect } from "@playwright/test";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import http from "node:http";
import { promisify } from "node:util";

const run = promisify(execFile);
const names = {
  R01: "Upload initiation commits, response is lost",
  R02: "Part-signing transaction commits, response is lost",
  R03: "Storage PUT interrupted before accepting the complete part",
  R04: "Storage accepts PUT, its response is lost",
  R05: "Acknowledgement request is lost before reaching API",
  R06: "Acknowledgement commits, its response is lost",
  R07: "Browser reload after several acknowledged parts",
  R08: "User pause while parts are in flight",
  R09: "Part URL expires before PUT",
};
const storagePattern = /.*\?.*partNumber=.*/;
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const cleanError = (error) =>
  String(error?.message || error)
    .split("Call log:")[0]
    .replace(/https?:\/\/[^\s]+/g, "[URL redacted]")
    .slice(0, 1500);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => (resolve = done));
  return { promise, resolve };
};

async function inspect(id) {
  const { stdout } = await run(
    process.env.CLOUDDRIVE_TEST_PYTHON || "python",
    ["scripts/recovery-inspect.py", "--file", id],
    { env: { ...process.env, PYTHONPATH: "." }, timeout: 15_000 },
  );
  return JSON.parse(stdout);
}

// Send only a prefix over a real TCP connection while declaring the entire
// signed Content-Length. MinIO must discard this incomplete part. The browser
// sees a transport failure and retries through the ordinary application code.
async function truncatedPut(request) {
  const url = new URL(request.url());
  assert.equal(url.protocol, "http:");
  const body = request.postDataBuffer();
  assert.ok(body && body.length > 65_536);
  const headers = await request.allHeaders();
  headers["content-length"] = String(body.length);
  delete headers["transfer-encoding"];
  const prefix = body.subarray(0, 65_536);
  await new Promise((resolve, reject) => {
    let sent = false;
    const connection = http.request(url, { method: "PUT", headers });
    const timer = setTimeout(() => {
      connection.destroy();
      if (sent) resolve();
      else reject(new Error("Truncated PUT did not transmit its prefix"));
    }, 250);
    connection.on("error", (error) => {
      clearTimeout(timer);
      if (sent) resolve();
      else reject(error);
    });
    connection.on("response", (response) => {
      clearTimeout(timer);
      response.resume();
      connection.destroy();
      reject(
        new Error(
          `Storage replied before interruption: ${response.statusCode}`,
        ),
      );
    });
    connection.write(prefix, () => (sent = true));
    // Intentionally never end the request: 65,536 bytes cannot satisfy the
    // original full-length declaration, even on very fast local storage.
  });
  return {
    transmitted_prefix_bytes: prefix.length,
    declared_bytes: body.length,
  };
}

export async function runUploadCases({
  baseURL,
  reportCase = () => {},
  runId,
  browser: sharedBrowser,
  caseIds = Object.keys(names),
}) {
  const browser =
    sharedBrowser ||
    (await chromium.launch({
      headless: true,
      ...(process.env.PLAYWRIGHT_CHANNEL
        ? { channel: process.env.PLAYWRIGHT_CHANNEL }
        : {}),
    }));
  const results = [];
  try {
    for (const id of caseIds) {
      assert.ok(names[id], `Unknown browser recovery case ${id}`);
      const started = performance.now();
      const content = Buffer.alloc(
        ["R07", "R08"].includes(id)
          ? 24 * 1024 * 1024
          : id === "R03"
            ? 17 * 1024 * 1024 + 17
            : 256 * 1024 + 17,
        Number(id.slice(1)) + 61,
      );
      Buffer.from(`CloudDrive recovery fixture ${id}\n`).copy(content);
      const source = { bytes: content.length, sha256: sha(content) };
      const fixture = {
        name: `${id}-${String(runId).replace(/[^a-zA-Z0-9_-]/g, "_")}.bin`,
        mimeType: "application/octet-stream",
        buffer: content,
      };
      const result = {
        id,
        name: names[id],
        status: "failed",
        fault_observed: false,
        source,
        deadline_s: 120,
        recovery_clock:
          id === "R07" || id === "R08"
            ? "Original-file reselection requested after reload/pause to READY observed in browser; includes resume hashing and excludes deliberate interruption hold."
            : id === "R09"
              ? "First actual expired-signature HTTP 403 observed to READY observed in browser; includes URL renewal and transfer."
              : "Injected fault observed to READY observed in browser; includes retries or explicit original-file reselection.",
        fault: { kind: "browser transport interception" },
      };
      const context = await browser.newContext({
        baseURL,
        acceptDownloads: true,
      });
      const page = await context.newPage();
      page.setDefaultTimeout(30_000);
      const deadline = setTimeout(
        () => context.close().catch(() => {}),
        120_000,
      );
      let token, fileId, recoveredAt;
      const release = deferred();
      const events = { storage_puts: [], signs: [], acknowledgements: [] };
      const errors = [];
      const routeErrors = [];
      const intercept = (pattern, handler) =>
        page.route(pattern, async (route) => {
          try {
            await handler(route);
          } catch (error) {
            routeErrors.push(cleanError(error));
            await route.abort("failed").catch(() => {});
          }
        });
      page.on("pageerror", (error) => errors.push(cleanError(error)));
      page.on("request", (request) => {
        const url = new URL(request.url());
        if (request.method() === "PUT" && url.searchParams.has("partNumber"))
          events.storage_puts.push(Number(url.searchParams.get("partNumber")));
        const sign = url.pathname.match(/\/parts\/(\d+)\/sign$/);
        if (sign) events.signs.push(Number(sign[1]));
        const ack = url.pathname.match(/\/parts\/(\d+)$/);
        if (ack && request.method() === "PUT")
          events.acknowledgements.push(Number(ack[1]));
      });
      const request = async (path, method = "GET", body, key = token) => {
        const response = await context.request.fetch(`${baseURL}/api${path}`, {
          method,
          headers: { Authorization: `Bearer ${key}` },
          ...(body ? { data: body } : {}),
          timeout: 15_000,
        });
        return response;
      };
      const inventory = async () => {
        const response = await request("/files");
        assert.equal(response.status(), 200);
        return (await response.json()).files;
      };
      const select = () => page.locator("#file-input").setInputFiles(fixture);
      let faultAt, resumeRequestedAt;
      const markFault = (details = {}) => {
        result.fault_observed = true;
        result.fault = { ...result.fault, ...details };
        faultAt ??= performance.now();
      };
      try {
        if (id === "R08")
          await page.addInitScript(() => {
            window.__recoveryXHR = [];
            const open = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function (method, url, ...args) {
              const part = Number(
                new URL(url, location.href).searchParams.get("partNumber"),
              );
              if (method === "PUT" && part) {
                this.upload.addEventListener("progress", (event) =>
                  window.__recoveryXHR.push({
                    type: "progress",
                    part,
                    loaded: event.loaded,
                    total: event.total,
                  }),
                );
                this.addEventListener("abort", () =>
                  window.__recoveryXHR.push({ type: "abort", part }),
                );
              }
              return open.call(this, method, url, ...args);
            };
          });
        await page.goto(baseURL);
        await expect(page.locator("#workspace-id")).toHaveText(
          /Workspace [a-f0-9]{8}/,
        );
        await expect(page.locator("#upload-limit")).toContainText("5 GB");
        token = await page.evaluate(() =>
          localStorage.getItem("clouddrive.access"),
        );
        assert.ok(token);
        // Assert the application's download gate while the session is still
        // UPLOADING. This hook lets the real completion request continue.
        await intercept("**/api/uploads/*/complete", async (route) => {
          fileId = new URL(route.request().url()).pathname.split("/")[3];
          const premature = await request(`/files/${fileId}/download`);
          result.pre_ready_download_status = premature.status();
          assert.equal(premature.status(), 409);
          await route.continue();
        });

        if (id === "R01") {
          let lost = false;
          let committedId, initiationBody;
          await intercept("**/api/uploads", async (route) => {
            if (route.request().method() !== "POST" || lost)
              return route.continue();
            initiationBody = route.request().postDataJSON();
            const response = await route.fetch();
            assert.equal(response.status(), 201);
            committedId = (await response.json()).id;
            fileId = committedId;
            lost = true;
            markFault({
              committed_response_status: response.status(),
              delivered_to_browser: false,
            });
            await route.abort("failed");
          });
          await select();
          await expect(page.locator("#notice")).toContainText(
            "Failed to fetch",
          );
          assert.equal(lost, true);
          const replay = await request("/uploads", "POST", initiationBody);
          assert.equal(replay.status(), 201);
          assert.equal((await replay.json()).id, committedId);
          result.fault.same_request_id_same_upload = true;
          assert.equal((await inventory()).length, 1);
          await select();
        } else if (id === "R02") {
          let lost = false;
          await intercept("**/parts/1/sign", async (route) => {
            if (lost) return route.continue();
            const response = await route.fetch();
            assert.equal(response.status(), 200);
            lost = true;
            markFault({
              committed_response_status: 200,
              delivered_to_browser: false,
            });
            await route.abort("failed");
          });
          await select();
        } else if (id === "R03") {
          let interrupted = false;
          await intercept(storagePattern, async (route) => {
            if (route.request().method() !== "PUT") return route.continue();
            const number = Number(
              new URL(route.request().url()).searchParams.get("partNumber"),
            );
            if (number === 3) {
              await release.promise;
              await route.continue().catch(() => {});
              return;
            }
            if (number !== 2 || interrupted) return route.continue();
            interrupted = true;
            await expect
              .poll(
                async () => {
                  const files = await inventory();
                  fileId = files[0]?.id;
                  return (
                    files[0]?.parts
                      .filter((part) => part.etag)
                      .map((part) => part.number) || []
                  );
                },
                { timeout: 30_000 },
              )
              .toEqual([1]);
            const details = await truncatedPut(route.request());
            const state = await inspect(fileId);
            assert.deepEqual(state.remote_parts, [1]);
            assert.deepEqual(state.confirmed_parts, [1]);
            markFault({
              kind: "real truncated storage PUT plus browser transport failure",
              ...details,
              remote_parts_after_interruption: state.remote_parts,
              confirmed_parts_preserved: state.confirmed_parts,
              interrupted_part: 2,
            });
            release.resolve();
            await route.abort("connectionreset");
          });
          await select();
        } else if (id === "R04" || id === "R06") {
          let lost = false;
          const pattern = id === "R04" ? storagePattern : "**/parts/1";
          await intercept(pattern, async (route) => {
            if (route.request().method() !== "PUT" || lost)
              return route.continue();
            const response = await route.fetch();
            assert.equal(response.status(), 200);
            lost = true;
            markFault({
              upstream_response_status: 200,
              delivered_to_browser: false,
            });
            await route.abort("failed");
          });
          await select();
        } else if (id === "R05") {
          let lost = false;
          await intercept("**/parts/1", async (route) => {
            if (route.request().method() !== "PUT" || lost)
              return route.continue();
            lost = true;
            fileId = new URL(route.request().url()).pathname.split("/")[3];
            const state = await inspect(fileId);
            assert.deepEqual(state.remote_parts, [1]);
            assert.deepEqual(state.confirmed_parts, []);
            markFault({
              reached_api: false,
              remote_parts: state.remote_parts,
              confirmed_parts: state.confirmed_parts,
            });
            await route.abort("failed");
          });
          await select();
        } else if (id === "R07" || id === "R08") {
          const acceptedBefore = id === "R07" ? [1, 2] : [1];
          let block = true;
          const held = [];
          let cdp;
          if (id === "R08") {
            // Enable throttling before XHR creation, so outstanding route
            // interceptions cannot inherit an earlier unthrottled request.
            cdp = await context.newCDPSession(page);
            await cdp.send("Network.enable");
            await cdp.send("Network.emulateNetworkConditions", {
              offline: false,
              latency: 0,
              downloadThroughput: -1,
              uploadThroughput: 512 * 1024,
            });
          }
          await intercept(storagePattern, async (route) => {
            const number = Number(
              new URL(route.request().url()).searchParams.get("partNumber"),
            );
            if (
              route.request().method() === "PUT" &&
              block &&
              !acceptedBefore.includes(number)
            ) {
              held.push(number);
              await release.promise;
              if (id === "R08") await route.continue().catch(() => {});
              else await route.abort("failed").catch(() => {});
              return;
            }
            await route.continue().catch(() => {});
          });
          await select();
          await expect
            .poll(
              async () => {
                const files = await inventory();
                fileId = files[0]?.id;
                return (
                  files[0]?.parts
                    .filter((part) => part.etag)
                    .map((part) => part.number) || []
                );
              },
              { timeout: 45_000 },
            )
            .toEqual(acceptedBefore);
          await expect.poll(() => held.length).toBeGreaterThan(0);
          if (id === "R08") {
            block = false;
            release.resolve();
            await expect
              .poll(
                () =>
                  page.evaluate(
                    () =>
                      window.__recoveryXHR.filter(
                        (event) =>
                          event.type === "progress" &&
                          event.part !== 1 &&
                          event.loaded > 0 &&
                          event.loaded < event.total,
                      ).length,
                  ),
                { timeout: 20_000 },
              )
              .toBeGreaterThan(0);
          }
          if (id === "R07") {
            // Reload directly with outstanding requests. Do not pause first:
            // R08 separately exercises the user's orderly pause operation.
            await page.reload();
            await expect(
              page.locator("tbody tr", { hasText: fixture.name }),
            ).toBeVisible();
          } else {
            await page
              .getByRole("button", {
                name: `Pause ${fixture.name}`,
                exact: true,
              })
              .click();
            await expect(
              page.getByText("Paused · Select file to resume", { exact: true }),
            ).toBeVisible();
          }
          block = false;
          release.resolve();
          let liveEvents;
          if (id === "R08") {
            liveEvents = await page.evaluate(() => window.__recoveryXHR);
            assert.ok(
              liveEvents.some(
                (event) =>
                  event.type === "progress" &&
                  event.part !== 1 &&
                  event.loaded > 0 &&
                  event.loaded < event.total,
              ),
            );
            assert.ok(
              liveEvents.some(
                (event) => event.type === "abort" && event.part !== 1,
              ),
            );
            await cdp.send("Network.emulateNetworkConditions", {
              offline: false,
              latency: 0,
              downloadThroughput: -1,
              uploadThroughput: -1,
            });
            await cdp.detach();
          }
          const snapshot = await inspect(fileId);
          assert.deepEqual(snapshot.confirmed_parts, acceptedBefore);
          markFault({
            kind:
              id === "R07"
                ? "real browser reload after durable acknowledgements"
                : "user pause during real storage PUTs throttled by Chrome DevTools",
            acknowledged_before_fault: acceptedBefore,
            held_inflight_parts: held,
            confirmed_after_interruption: snapshot.confirmed_parts,
            ...(liveEvents
              ? {
                  upload_throughput_bytes_s: 512 * 1024,
                  xhr_events: liveEvents,
                }
              : {}),
          });
          if (id === "R07") result.fault.browser_reloaded = true;
          result.puts_before_resume = [...events.storage_puts];
          events.storage_puts.length = 0;
          resumeRequestedAt = performance.now();
          await select();
          result.must_not_resend = acceptedBefore;
        } else if (id === "R09") {
          let expired = false;
          const statuses = [];
          page.on("response", (response) => {
            if (new URL(response.url()).searchParams.has("partNumber")) {
              statuses.push(response.status());
              if (response.status() === 403)
                markFault({
                  kind: "genuinely expired signed URL rejected by MinIO",
                  rejected_status: 403,
                });
            }
          });
          await intercept("**/parts/1/sign", async (route) => {
            if (expired) return route.continue();
            const response = await route.fetch();
            const signed = await response.json();
            fileId = new URL(route.request().url()).pathname.split("/")[3];
            const { stdout } = await run(
              process.env.CLOUDDRIVE_TEST_PYTHON || "python",
              [
                "scripts/expire-test-upload.py",
                fileId,
                "--expired-part",
                "1",
                "--checksum",
                route.request().postDataJSON().checksum,
              ],
              { env: { ...process.env, PYTHONPATH: "." }, timeout: 15_000 },
            );
            signed.url = JSON.parse(stdout).url;
            expired = true;
            await route.fulfill({ response, json: signed });
          });
          await select();
          await expect
            .poll(() => statuses, { timeout: 30_000 })
            .toEqual([403, 200]);
          markFault({
            kind: "genuinely expired signed URL rejected by MinIO",
            storage_statuses: statuses,
            expired_signature_seconds: 60,
            signature_age_hours: 2,
          });
        }

        const row = page.locator("tbody tr", { hasText: fixture.name });
        await expect(row.getByText("Verified", { exact: true })).toBeVisible({
          timeout: 60_000,
        });
        recoveredAt = performance.now();
        assert.equal(
          result.fault_observed,
          true,
          "Expected fault was never observed",
        );
        const files = await inventory();
        assert.equal(files.length, 1);
        fileId = files[0].id;
        assert.equal(files[0].status, "READY");
        assert.equal(files[0].sha256, source.sha256);
        assert.equal(files[0].size, source.bytes);
        const state = await inspect(fileId);
        assert.equal(state.metadata_count, 1);
        assert.equal(state.object_count, 1);
        assert.equal(state.multipart_count, 0);
        assert.equal(state.result_count, 1);
        assert.equal(state.status, "READY");
        assert.equal(state.bytes_verified, source.bytes);
        assert.equal(state.sha256, source.sha256);
        assert.equal(state.claim_active, false);
        const outsider = await context.request.post(
          `${baseURL}/api/workspaces`,
        );
        assert.equal(outsider.status(), 201);
        const other = await outsider.json();
        const forbidden = await request(
          `/files/${fileId}/download`,
          "GET",
          undefined,
          other.access_key,
        );
        assert.equal(forbidden.status(), 404);
        result.cross_workspace_download_status = forbidden.status();
        assert.equal(result.pre_ready_download_status, 409);
        if (result.must_not_resend)
          assert.equal(
            events.storage_puts.some((number) =>
              result.must_not_resend.includes(number),
            ),
            false,
          );
        if (["R04", "R05", "R06"].includes(id))
          assert.deepEqual(events.storage_puts, [1]);
        if (["R02", "R04", "R05", "R06", "R09"].includes(id))
          assert.deepEqual(events.signs, [1, 1]);
        if (id === "R03") {
          for (const number of [1, 2, 3]) {
            const expected = number === 2 ? 2 : 1;
            assert.equal(
              events.signs.filter((part) => part === number).length,
              expected,
            );
            assert.equal(
              events.storage_puts.filter((part) => part === number).length,
              expected,
            );
          }
        }
        await row.getByText(fixture.name, { exact: true }).click();
        await expect(page.locator(".hash")).toHaveText(source.sha256);
        const [download] = await Promise.all([
          page.waitForEvent("download", { timeout: 30_000 }),
          page
            .getByRole("button", { name: "Download file", exact: true })
            .click(),
        ]);
        const downloaded = await readFile(await download.path());
        result.download = { bytes: downloaded.length, sha256: sha(downloaded) };
        assert.deepEqual(result.download, source);
        assert.deepEqual(errors, []);
        assert.deepEqual(routeErrors, []);
        result.file_id = fileId;
        result.final = state;
        result.events = events;
        result.recovery_s = Number(
          ((recoveredAt - (resumeRequestedAt ?? faultAt)) / 1000).toFixed(6),
        );
        result.status = "passed";
      } catch (error) {
        result.error = cleanError(error);
        if (routeErrors.length) result.route_errors = routeErrors;
        result.file_id = fileId;
        result.events = events;
      } finally {
        release.resolve();
        clearTimeout(deadline);
        // Context closure releases any held routes before scoped API cleanup.
        await context.close().catch(() => {});
        if (token) {
          try {
            const listed = await fetch(`${baseURL}/api/files`, {
              headers: { Authorization: `Bearer ${token}` },
              signal: AbortSignal.timeout(15_000),
            });
            const ownFiles = (await listed.json()).files;
            result.cleanup = [];
            for (const file of ownFiles) {
              const response = await fetch(`${baseURL}/api/files/${file.id}`, {
                method: "DELETE",
                headers: { Authorization: `Bearer ${token}` },
                signal: AbortSignal.timeout(15_000),
              });
              result.cleanup.push({
                file_id: file.id,
                status: response.status,
              });
              assert.equal(response.status, 204);
            }
          } catch (error) {
            result.status = "failed";
            result.cleanup_error = cleanError(error);
          }
        }
        result.elapsed_s = Number(
          ((performance.now() - started) / 1000).toFixed(6),
        );
      }
      results.push(result);
      await reportCase(result);
    }
  } finally {
    if (!sharedBrowser) await browser.close();
  }
  return results;
}
