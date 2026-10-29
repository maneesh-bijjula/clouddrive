/** Benchmark control-plane requests: fresh socket, fixed deadline, no retries. */
import http from "node:http";
import https from "node:https";

const safeCode = (value) =>
  typeof value === "string" && /^[A-Z][A-Z0-9_]{0,79}$/.test(value)
    ? value
    : undefined;

export function observerError(error, fallback = "ERR_OBSERVER_REQUEST") {
  const code = safeCode(error?.code) ?? fallback;
  const causeCode = safeCode(error?.cause?.code);
  const result = new Error(
    `Load observer request failed (${code}${causeCode ? `; cause ${causeCode}` : ""})`,
  );
  result.name = "LoadObserverError";
  result.code = code;
  if (causeCode) result.cause_code = causeCode;
  return result;
}

export function observeApi(
  url,
  { method = "GET", headers = {}, body, timeoutMs = 15_000 } = {},
) {
  return new Promise((resolve, reject) => {
    let request,
      deadline,
      settled = false;
    const fail = (error, fallback) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      reject(observerError(error, fallback));
      request?.destroy();
    };
    try {
      const parsed = new URL(url);
      const transport = { "http:": http, "https:": https }[parsed.protocol];
      if (!transport) return fail(null, "ERR_OBSERVER_PROTOCOL");
      const data = body === undefined ? undefined : JSON.stringify(body);
      const normalized = Object.fromEntries(
        Object.entries(headers).map(([key, value]) => [
          key.toLowerCase(),
          value,
        ]),
      );
      request = transport.request(
        parsed,
        {
          method,
          agent: false,
          headers: {
            ...normalized,
            ...(data === undefined
              ? {}
              : {
                  "content-type": "application/json",
                  "content-length": Buffer.byteLength(data),
                }),
            connection: "close",
          },
        },
        (response) => {
          const chunks = [];
          let length = 0;
          response.on("error", (error) => fail(error));
          response.on("aborted", () =>
            fail(null, "ERR_OBSERVER_RESPONSE_ABORTED"),
          );
          response.on("data", (chunk) => {
            length += chunk.length;
            if (length > 8 * 1024 * 1024)
              return fail(null, "ERR_OBSERVER_RESPONSE_TOO_LARGE");
            chunks.push(chunk);
          });
          response.on("end", () => {
            if (settled) return;
            try {
              const value =
                response.statusCode === 204
                  ? null
                  : JSON.parse(Buffer.concat(chunks).toString("utf8"));
              settled = true;
              clearTimeout(deadline);
              resolve({ status: response.statusCode, data: value });
            } catch {
              fail(null, "ERR_OBSERVER_JSON");
            }
          });
        },
      );
      request.on("error", (error) => fail(error));
      deadline = setTimeout(() => fail(null, "ETIMEDOUT"), timeoutMs);
      request.end(data);
    } catch (error) {
      fail(error);
    }
  });
}
