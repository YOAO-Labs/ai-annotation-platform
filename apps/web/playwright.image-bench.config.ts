import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  projects: base.projects?.filter(({ name }) => name === "chromium"),
  testMatch: "**/benchmarks/image-bench.spec.ts",
  outputDir: "./test-results/image-bench",
  retries: 0,
});
