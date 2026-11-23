import { worktreeMode as readWorktreeMode } from "./e2e/helpers/worktree-mode.mjs";
import { defineConfig, devices } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { browserEnvironment } from "./e2e/helpers/browser-environment.mjs";

if (process.env.PLAYWRIGHT_RASTER_OFFLINE === "1" && !process.env.PLAYWRIGHT_FIXTURE_RUN_ID) {
  throw new Error("离线 Mask 测试必须使用启动器准备的 suite");
}
process.env.PLAYWRIGHT_FIXTURE_RUN_ID ??= randomUUID();

const worktreeMode = readWorktreeMode();
if (worktreeMode === "dev") {
  throw new Error(
    "开发模式不能运行 E2E；请使用 pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e",
  );
}
const rasterMaskMatrix = process.env.PLAYWRIGHT_RASTER_MASK_MATRIX;
const rasterOffline = process.env.PLAYWRIGHT_RASTER_OFFLINE === "1";
if (
  rasterOffline &&
  (worktreeMode !== "e2e" ||
    rasterMaskMatrix !== "native" ||
    !process.env.PLAYWRIGHT_RASTER_FAULT_MANIFEST ||
    !process.env.PLAYWRIGHT_FIXTURE_RUN_ID ||
    !process.env.PLAYWRIGHT_RASTER_FIXTURE_SOCKET)
) {
  throw new Error("离线 Mask 测试缺少已准备的工作树、suite、manifest 或夹具通道");
}
if (
  rasterOffline &&
  Object.entries(process.env).some(
    ([key, value]) =>
      Boolean(value) &&
      /^(?:DATABASE_URL|MIGRATION_DATABASE_URL|TEST_DATABASE_URL|PLAYWRIGHT_E2E_DATABASE_URL|FIXTURE_RUNTIME_DATABASE_URL|CONNECTOR_ENCRYPTION_KEY|MINIO_ACCESS_KEY|MINIO_SECRET_KEY|MINIO_ROOT_USER|MINIO_ROOT_PASSWORD|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)$/i.test(
        key,
      ),
  )
) {
  throw new Error("离线 Mask 测试进程不能持有数据库或存储凭据");
}
if (
  worktreeMode &&
  !rasterOffline &&
  (!process.env.PLAYWRIGHT_E2E_DATABASE_URL || !process.env.DATABASE_URL)
) {
  throw new Error("独立工作树缺少测试数据库配置；请通过 dev:worktree exec 启动");
}
const rasterMaskCreateEnabled = rasterMaskMatrix === "native";
const chromiumChannel = process.env.PLAYWRIGHT_CHROMIUM_CHANNEL;
const excludedTags = ["@stress"];
if (rasterMaskMatrix !== "native") excludedTags.push("@mask-native");
if (rasterMaskMatrix !== "readonly") excludedTags.push("@mask-readonly");
const pointcloudWebGpuQualification = process.env.PLAYWRIGHT_POINTCLOUD_WEBGPU === "1";
const configDir = dirname(fileURLToPath(import.meta.url));
const isCI = Boolean(process.env.CI);
if (worktreeMode && !rasterOffline) {
  process.env.AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
}
const useIsolatedServers = Boolean(worktreeMode) || !isCI || Boolean(rasterMaskMatrix);
const isolatedApiPort = process.env.PLAYWRIGHT_ISOLATED_API_PORT ?? "8010";
const isolatedWebPort = process.env.PLAYWRIGHT_ISOLATED_WEB_PORT ?? "3001";
const browserEnv = browserEnvironment();
const pointcloudBrowserEnv = browserEnvironment({ dropForwardedDisplay: true });

if (useIsolatedServers) {
  process.env.PLAYWRIGHT_BASE_URL = `http://127.0.0.1:${isolatedWebPort}`;
  process.env.PLAYWRIGHT_API_BASE = `http://127.0.0.1:${isolatedApiPort}`;
}

const defaultBaseURL = useIsolatedServers
  ? `http://127.0.0.1:${isolatedWebPort}`
  : "http://127.0.0.1:3000";
process.env.PLAYWRIGHT_BASE_URL ??= defaultBaseURL;
const e2eDatabaseURL = rasterOffline
  ? undefined
  : worktreeMode
    ? process.env.PLAYWRIGHT_E2E_DATABASE_URL!
    : isCI
      ? (process.env.DATABASE_URL ??
        "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation_test")
      : (process.env.PLAYWRIGHT_E2E_DATABASE_URL ??
        "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation_e2e");
if (e2eDatabaseURL !== undefined) process.env.PLAYWRIGHT_E2E_DATABASE_URL ??= e2eDatabaseURL;
const e2eRuntimeDatabaseURL = worktreeMode ? process.env.DATABASE_URL : e2eDatabaseURL;

const isolatedApiCommand = [
  ...(isCI
    ? []
    : ['DATABASE_URL="$MIGRATION_DATABASE_URL" uv run python scripts/prepare_e2e_db.py']),
  "uv run alembic upgrade head",
  worktreeMode
    ? `uv run python ../../scripts/fixture_native_playwright.py --port ${isolatedApiPort}`
    : `MIGRATION_DATABASE_URL= TEST_DATABASE_URL= PLAYWRIGHT_E2E_DATABASE_URL= uv run uvicorn app.main:app --host 127.0.0.1 --port ${isolatedApiPort}`,
].join(" && ");

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["**/tests/**/*.spec.ts"],
  grepInvert: new RegExp(excludedTags.join("|")),
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: isCI ? 1 : 0,
  maxFailures: isCI ? 1 : 0,
  globalTimeout: isCI ? 25 * 60_000 : 0,
  workers: 1,
  reporter: isCI
    ? [["line"], ["github"], ["html"], ["json", { outputFile: "e2e-results.json" }]]
    : "html",

  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? defaultBaseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: { env: browserEnv },
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(chromiumChannel ? { channel: chromiumChannel } : {}),
      },
      testIgnore: ["**/workbench-pointcloud*.spec.ts"],
    },
    {
      name: "pointcloud",
      testMatch: ["**/workbench-pointcloud*.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        ...(chromiumChannel ? { channel: chromiumChannel } : {}),
        launchOptions: {
          env: pointcloudBrowserEnv,
          args: pointcloudWebGpuQualification
            ? process.platform === "darwin"
              ? []
              : [
                  "--enable-unsafe-webgpu",
                  "--enable-features=Vulkan",
                  "--use-angle=vulkan",
                  "--disable-vulkan-surface",
                  "--ignore-gpu-blocklist",
                ]
            : [
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--ignore-gpu-blocklist",
              ],
        },
      },
    },
  ],

  webServer: useIsolatedServers
    ? [
        ...(rasterOffline
          ? []
          : [
              {
                command: isolatedApiCommand,
                cwd: resolve(configDir, "../api"),
                env: {
                  DATABASE_URL: e2eRuntimeDatabaseURL!,
                  MIGRATION_DATABASE_URL: e2eDatabaseURL!,
                  ENVIRONMENT: "development",
                  E2E_SEED_ENABLED: "true",
                  ...(rasterMaskMatrix
                    ? {
                        RASTER_MASK_READ_ENABLED: "true",
                        RASTER_MASK_CREATE_ENABLED: rasterMaskCreateEnabled ? "true" : "false",
                      }
                    : {}),
                },
                url: `http://127.0.0.1:${isolatedApiPort}/health/db`,
                reuseExistingServer: false,
                timeout: 120_000,
                gracefulShutdown: { signal: "SIGTERM", timeout: 30_000 },
              },
            ]),
        {
          command: `env -i PATH="$PATH" HOME="$HOME" LANG="\${LANG:-C.UTF-8}" API_PROXY_TARGET="$API_PROXY_TARGET" MINIO_PROXY_TARGET="$MINIO_PROXY_TARGET" VITE_WS_HOST="$VITE_WS_HOST" PORT="$PORT" ${
            isCI && (!rasterMaskMatrix || process.env.PLAYWRIGHT_WEB_PREVIEW === "1")
              ? `pnpm preview --host 127.0.0.1 --port ${isolatedWebPort} --strictPort`
              : "pnpm dev --host 127.0.0.1"
          }`,
          cwd: configDir,
          env: {
            API_PROXY_TARGET: `http://127.0.0.1:${isolatedApiPort}`,
            MINIO_PROXY_TARGET: process.env.MINIO_PROXY_TARGET ?? "http://localhost:9000",
            VITE_WS_HOST: `127.0.0.1:${isolatedApiPort}`,
            PORT: isolatedWebPort,
          },
          url: `http://127.0.0.1:${isolatedWebPort}`,
          reuseExistingServer: false,
          timeout: 120_000,
          gracefulShutdown: { signal: "SIGTERM", timeout: 30_000 },
        },
      ]
    : isCI
      ? {
          command: "pnpm preview --host 127.0.0.1 --port 3000",
          url: "http://127.0.0.1:3000",
          reuseExistingServer: false,
          timeout: 120_000,
        }
      : undefined,
});
