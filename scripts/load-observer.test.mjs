import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { observeApi, observerError } from "./load-observer.mjs";

async function withServer(handler, check) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    await check(url);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

test("JSON observer requests use separate sockets and close every connection", async () => {
  const sockets = [],
    bodies = [];
  await withServer(
    async (request, response) => {
      sockets.push(request.socket);
      assert.equal(request.headers.connection, "close");
      assert.equal(
        request.headers.authorization,
        "Bearer private-workspace-key",
      );
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end('{"id":"created"}');
    },
    async (url) => {
      for (let i = 0; i < 2; i++)
        assert.deepEqual(
          await observeApi(url, {
            method: "POST",
            headers: {
              Authorization: "Bearer private-workspace-key",
              Connection: "keep-alive",
            },
            body: { request: i },
          }),
          { status: 201, data: { id: "created" } },
        );
    },
  );
  assert.equal(new Set(sockets).size, 2);
  assert.deepEqual(bodies, [{ request: 0 }, { request: 1 }]);
});

test("204 and non-success statuses remain explicit without retries", async () => {
  let calls = 0;
  await withServer(
    (request, response) => {
      calls++;
      response.writeHead(request.url === "/empty" ? 204 : 429);
      response.end(request.url === "/empty" ? undefined : '{"detail":"busy"}');
    },
    async (url) => {
      assert.deepEqual(await observeApi(url + "/empty"), {
        status: 204,
        data: null,
      });
      assert.deepEqual(await observeApi(url + "/busy"), {
        status: 429,
        data: { detail: "busy" },
      });
    },
  );
  assert.equal(calls, 2);
});

test("absolute deadline includes a stalled response body and does not retry", async () => {
  let calls = 0;
  await withServer(
    (_request, response) => {
      calls++;
      response.writeHead(200, { "Content-Type": "application/json" });
      response.write('{"unfinished":');
    },
    async (url) => {
      await assert.rejects(observeApi(url, { timeoutMs: 100 }), (error) => {
        assert.equal(error.code, "ETIMEDOUT");
        assert.equal(error.name, "LoadObserverError");
        assert.ok(!error.message.includes(url));
        return true;
      });
    },
  );
  assert.equal(calls, 1);
});

test("socket resets fail once with a safe native error code", async () => {
  let calls = 0;
  await withServer(
    (request) => {
      calls++;
      request.socket.destroy();
    },
    async (url) => {
      await assert.rejects(
        observeApi(url + "/?signed=private-url-secret", {
          headers: { Authorization: "Bearer private-workspace-key" },
        }),
        (error) => {
          assert.equal(error.code, "ECONNRESET");
          const text = String(error) + JSON.stringify(error);
          assert.ok(!text.includes(url));
          assert.ok(!text.includes("private-url-secret"));
          assert.ok(!text.includes("private-workspace-key"));
          return true;
        },
      );
    },
  );
  assert.equal(calls, 1);
});

test("invalid JSON and nested causes preserve codes, never raw diagnostic text", async () => {
  await withServer(
    (_request, response) => response.end("private-response-secret"),
    async (url) => {
      await assert.rejects(observeApi(url), (error) => {
        assert.equal(error.code, "ERR_OBSERVER_JSON");
        assert.ok(!String(error).includes("private-response-secret"));
        return true;
      });
    },
  );
  const error = observerError({
    message:
      "Bearer private-workspace-key https://example.invalid/?signed=secret",
    code: "EPIPE",
    cause: { code: "ECONNRESET", message: "private-cause-secret" },
  });
  assert.equal(error.code, "EPIPE");
  assert.equal(error.cause_code, "ECONNRESET");
  assert.ok(!JSON.stringify(error).includes("secret"));
  assert.ok(!String(error).includes("private"));
});
