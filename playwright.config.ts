import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests-browser",
  outputDir: "test-results/browser",
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    ignoreHTTPSErrors: process.env.PLAYWRIGHT_IGNORE_HTTPS_ERRORS === "1",
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:8090",
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: process.env.PLAYWRIGHT_EXTERNAL_SERVER
    ? undefined
    : {
        command: "bash scripts/browser-server.sh",
        url: "http://localhost:8090/api/health",
        timeout: 60_000,
        reuseExistingServer: false,
        env: {
          CLOUDDRIVE_DATABASE_URL:
            process.env.CLOUDDRIVE_DATABASE_URL ||
            "postgresql+psycopg://clouddrive:clouddrive@localhost:5436/clouddrive_test",
          CLOUDDRIVE_REDIS_URL:
            process.env.CLOUDDRIVE_REDIS_URL || "redis://localhost:6383/0",
          CLOUDDRIVE_S3_ENDPOINT:
            process.env.CLOUDDRIVE_S3_ENDPOINT || "http://localhost:9010",
          CLOUDDRIVE_S3_PUBLIC_ENDPOINT:
            process.env.CLOUDDRIVE_S3_PUBLIC_ENDPOINT ||
            "http://localhost:9010",
          CLOUDDRIVE_S3_BUCKET:
            process.env.CLOUDDRIVE_S3_BUCKET || "clouddrive-test",
        },
      },
});
