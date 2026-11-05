import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

if (!process.env.PLAYWRIGHT_RASTER_FAULT_MANIFEST || !process.env.PLAYWRIGHT_FIXTURE_RUN_ID) {
  throw new Error("Mask product E2E requires the prepared offline raster fault manifest");
}

if (!Array.isArray(base.webServer) || base.webServer.length !== 2) {
  throw new Error("Mask product E2E requires the isolated API and Web servers");
}

const [api, web] = base.webServer;
const port = process.env.PLAYWRIGHT_ISOLATED_WEB_PORT ?? "3001";

export default defineConfig({
  ...base,
  webServer: [
    api,
    {
      ...web,
      command: `pnpm preview --host 127.0.0.1 --port ${port} --strictPort`,
      env: {
        ...web.env,
        DATABASE_URL: "",
        MIGRATION_DATABASE_URL: "",
        TEST_DATABASE_URL: "",
        PLAYWRIGHT_E2E_DATABASE_URL: "",
        CONNECTOR_ENCRYPTION_KEY: "",
        MINIO_ACCESS_KEY: "",
        MINIO_SECRET_KEY: "",
        AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY: "",
        AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY: "",
      },
    },
  ],
});
