import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig(base, {
  grep: /@stress/,
  grepInvert: [],
  retries: 1,
});
