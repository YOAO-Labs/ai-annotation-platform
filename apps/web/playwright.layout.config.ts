import { defineConfig } from "@playwright/test";

// Frontend fixtures exercise real styles and primitives without an API or database.
export default defineConfig({
  testDir: "./e2e/layout",
  outputDir: "./test-results/scrollbar-layout",
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3218",
    launchOptions: {
      ignoreDefaultArgs: ["--hide-scrollbars"],
      args: ["--disable-features=OverlayScrollbar"],
    },
  },
  projects: [
    { name: "desktop-light", use: { viewport: { width: 1440, height: 900 } } },
    { name: "desktop-dark", use: { viewport: { width: 1440, height: 900 }, colorScheme: "dark" } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: "pnpm dev --host 127.0.0.1 --port 3218 --strictPort",
    url: "http://127.0.0.1:3218/e2e/fixtures/scrollbar-layout.html",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
