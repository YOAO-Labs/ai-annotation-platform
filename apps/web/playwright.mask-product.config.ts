import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

if (!Array.isArray(base.webServer) || base.webServer.length !== 2) {
  throw new Error("Mask product E2E requires the isolated API and Web servers");
}

const [api, web] = base.webServer;
const port = process.env.PLAYWRIGHT_ISOLATED_WEB_PORT ?? "3001";

export default defineConfig({
  ...base,
  webServer: [
    api,
    { ...web, command: `pnpm preview --host 127.0.0.1 --port ${port} --strictPort` },
  ],
});
