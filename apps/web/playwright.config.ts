import { defineConfig, devices } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const worktreeMode = process.env.AAP_WORKTREE_MODE;
if (worktreeMode === "dev") {
  throw new Error(
    "开发模式不能运行 E2E；请使用 pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e",
  );
}
if (worktreeMode && (!process.env.PLAYWRIGHT_E2E_DATABASE_URL || !process.env.DATABASE_URL)) {
  throw new Error("独立工作树缺少测试数据库配置；请通过 dev:worktree exec 启动");
}
const rasterMaskMatrix = process.env.PLAYWRIGHT_RASTER_MASK_MATRIX;
const rasterMaskCreateEnabled = rasterMaskMatrix === "native";
const chromiumChannel = process.env.PLAYWRIGHT_CHROMIUM_CHANNEL;
const pointcloudWebGpuQualification = process.env.PLAYWRIGHT_POINTCLOUD_WEBGPU === "1";
const configDir = dirname(fileURLToPath(import.meta.url));
const isCI = Boolean(process.env.CI);
const useIsolatedServers = Boolean(worktreeMode) || !isCI || Boolean(rasterMaskMatrix);
const isolatedApiPort = process.env.PLAYWRIGHT_ISOLATED_API_PORT ?? "8010";
const isolatedWebPort = process.env.PLAYWRIGHT_ISOLATED_WEB_PORT ?? "3001";
// SSH ForwardX11 会注入 localhost DISPLAY；无头 SwANGLE 会因此误走 XCB，导致 WebGL2 初始化失败。
// 只隔离转发型 DISPLAY，保留本机 :0 / :1，避免破坏显式 --headed 调试。
const forwardedX11Display = /^localhost:\d+(?:\.\d+)?$/.test(process.env.DISPLAY ?? "");
const pointcloudBrowserEnv = forwardedX11Display
  ? Object.fromEntries(
      Object.entries(process.env).filter(
        (entry): entry is [string, string] => entry[0] !== "DISPLAY" && entry[1] !== undefined,
      ),
    )
  : undefined;

if (useIsolatedServers) {
  // 固定隔离端口，避免继承 shell/CI job 中指向开发服务的旧变量。
  process.env.PLAYWRIGHT_BASE_URL = `http://127.0.0.1:${isolatedWebPort}`;
  process.env.PLAYWRIGHT_API_BASE = `http://127.0.0.1:${isolatedApiPort}`;
}

const defaultBaseURL = useIsolatedServers
  ? `http://127.0.0.1:${isolatedWebPort}`
  : "http://127.0.0.1:3000";
process.env.PLAYWRIGHT_BASE_URL ??= defaultBaseURL;
const e2eDatabaseURL = worktreeMode
  ? process.env.PLAYWRIGHT_E2E_DATABASE_URL!
  : isCI
    ? (process.env.DATABASE_URL ?? "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation_test")
    : (process.env.PLAYWRIGHT_E2E_DATABASE_URL ??
      "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation_e2e");
// Python ledger fixtures run as child processes and need the same disposable
// database URL as the API, including in CI where the URL is derived from DATABASE_URL.
process.env.PLAYWRIGHT_E2E_DATABASE_URL ??= e2eDatabaseURL;
const e2eRuntimeDatabaseURL = worktreeMode ? process.env.DATABASE_URL! : e2eDatabaseURL;

const isolatedApiCommand = [
  ...(isCI
    ? []
    : ['DATABASE_URL="$MIGRATION_DATABASE_URL" uv run python scripts/prepare_e2e_db.py']),
  "uv run alembic upgrade head",
  `MIGRATION_DATABASE_URL= TEST_DATABASE_URL= PLAYWRIGHT_E2E_DATABASE_URL= uv run uvicorn app.main:app --host 127.0.0.1 --port ${isolatedApiPort}`,
].join(" && ");

/**
 * Playwright E2E 配置。
 *
 * 本地只需先启动 postgres / redis / minio；Playwright 会创建并迁移
 * annotation_e2e，再自启 8010 API 和 3001 Web，避免复用开发服务。
 *
 * CI 中通过 webServer 启动 vite preview，使用真实后端 API。
 */
export default defineConfig({
  testDir: "./e2e",
  // Documentation capture remains separate. Visual baselines and stress scenarios
  // in tests/ run through playwright.extended.config.ts with their own CI reports.
  testMatch: ["**/tests/**/*.spec.ts"],
  grepInvert: /@visual|@stress/,
  // seed/reset and teardown delete the shared E2E fixture namespace. Keep one
  // worker per database; CI shards use independent runners and services.
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: isCI ? 1 : 0,
  // Bound each CI shard, including serial failures and retries.
  maxFailures: isCI ? 1 : 0,
  globalTimeout: isCI ? 15 * 60_000 : 0,
  workers: 1,
  reporter: isCI
    ? [["line"], ["github"], ["html"], ["json", { outputFile: "e2e-results.json" }]]
    : "html",

  globalTeardown: "./e2e/global-teardown.ts",

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? defaultBaseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(chromiumChannel ? { channel: chromiumChannel } : {}),
      },
      // 3D 点云 spec 交给下方 pointcloud project(需 WebGL 软渲染参数),此处排除。
      testIgnore: ["**/workbench-pointcloud*.spec.ts"],
    },
    // 3D 点云 spec 单列:headless Chromium 默认无 GPU,Three.js/WebGL 跑不起来;
    // 经 ANGLE 走 SwiftShader 软渲染(新版 Chromium 的 WebGL SwiftShader 需 unsafe 旗标显式放行)。
    {
      name: "pointcloud",
      testMatch: ["**/workbench-pointcloud*.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        ...(chromiumChannel ? { channel: chromiumChannel } : {}),
        launchOptions: {
          ...(pointcloudBrowserEnv ? { env: pointcloudBrowserEnv } : {}),
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
    // 起步只跑 chromium；稳定后再加 firefox/webkit
  ],

  webServer: useIsolatedServers
    ? [
        {
          command: isolatedApiCommand,
          cwd: resolve(configDir, "../api"),
          env: {
            DATABASE_URL: e2eRuntimeDatabaseURL,
            MIGRATION_DATABASE_URL: e2eDatabaseURL,
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
        },
        {
          command: "pnpm dev --host 127.0.0.1",
          cwd: configDir,
          env: {
            API_PROXY_TARGET: `http://127.0.0.1:${isolatedApiPort}`,
            VITE_WS_HOST: `127.0.0.1:${isolatedApiPort}`,
            PORT: isolatedWebPort,
          },
          url: `http://127.0.0.1:${isolatedWebPort}`,
          reuseExistingServer: false,
          timeout: 120_000,
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
