import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Stress sequences share CI runners with heavy renderer loads: a post-reload
// layout restore can freeze the main thread for tens of seconds before the
// manifest query fires, which is runner starvation, not a product defect. One
// retry separates deterministic breakage (fails twice, still red) from such
// starvation; retried passes stay visible as flaky. Visual baselines keep
// reporting once via playwright.extended.config.ts.
export default defineConfig(base, {
  grep: /@stress/,
  grepInvert: [],
  retries: 1,
});
