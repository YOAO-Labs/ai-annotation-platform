import { defineConfig, devices } from "@playwright/test";

const port = process.env.PLAYWRIGHT_ISOLATED_WEB_PORT ?? "3219";
const baseURL = `http://127.0.0.1:${port}`;
const isCI = Boolean(process.env.CI);

export default defineConfig({
  testDir: "./e2e/browser-integration",
  testMatch: ["**/*.spec.ts"],
  outputDir: "./test-results/browser-integration",
  fullyParallel: false,
  workers: 1,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  maxFailures: isCI ? 1 : 0,
  globalTimeout: isCI ? 15 * 60_000 : 0,
  reporter: isCI
    ? [["line"], ["github"], ["json", { outputFile: "e2e-browser-integration-results.json" }]]
    : "html",
  use: { baseURL },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [],
});
