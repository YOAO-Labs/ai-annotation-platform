import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

if (process.env.PLAYWRIGHT_RASTER_OFFLINE !== "1") {
  throw new Error("Mask product E2E requires the offline raster fixture launcher");
}
if (!Array.isArray(base.webServer) || base.webServer.length !== 1) {
  throw new Error("Mask product E2E requires the supervised API and isolated Web server");
}

const [web] = base.webServer;
const port = process.env.PLAYWRIGHT_ISOLATED_WEB_PORT ?? "3001";

export default defineConfig({
  ...base,
  webServer: [
    {
      ...web,
      command: `env -i PATH="$PATH" HOME="$HOME" LANG="\${LANG:-C.UTF-8}" API_PROXY_TARGET="$API_PROXY_TARGET" MINIO_PROXY_TARGET="$MINIO_PROXY_TARGET" VITE_WS_HOST="$VITE_WS_HOST" PORT="$PORT" pnpm preview --host 127.0.0.1 --port ${port} --strictPort`,
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
        PLAYWRIGHT_RASTER_FIXTURE_SOCKET: "",
      },
    },
  ],
});
