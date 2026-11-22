import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

if (process.env.PLAYWRIGHT_RASTER_OFFLINE !== "1") {
  throw new Error("Mask product E2E requires the offline raster fixture launcher");
}
if (!Array.isArray(base.webServer) || base.webServer.length !== 1) {
  throw new Error("Mask product E2E requires the supervised API and isolated Web server");
}

export default defineConfig({
  ...base,
  webServer: [],
});
