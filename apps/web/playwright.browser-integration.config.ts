import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Source-module browser integration; never counted as built-product E2E.
export default defineConfig(base, {
  testDir: "./e2e/browser-integration",
  reporter: process.env.CI
    ? [["line"], ["github"], ["json", { outputFile: "e2e-browser-integration-results.json" }]]
    : "html",
  testMatch: ["**/*.spec.ts"],
});
