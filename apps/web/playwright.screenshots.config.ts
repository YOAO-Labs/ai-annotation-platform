/**
 * v2 · 截图自动化多 project 配置。
 *
 * 执行：
 *   pnpm screenshots                  # desktop-light 全量（默认）
 *   pnpm screenshots:dark             # desktop-dark 单跑
 *   pnpm screenshots:matrix           # 三个截图 project 全跑
 *   pnpm screenshots:flows            # 流程预演，不产正式素材
 *   pnpm screenshots:marketing        # 流程录制 → 4K60 MKV 采集源 + MP4 通用母版
 *   pnpm screenshots:regression       # 视觉回归子集（M4）
 *
 * 调试：
 *   PWDEBUG=1 pnpm screenshots --grep="bbox/toolbar"
 *
 * 不进 CI 默认；CI 只跑 regression 子集（见 M4 / .github/workflows）。
 */
import { defineConfig, devices } from "@playwright/test";
import { MARKETING_PROJECT_NAME } from "./e2e/screenshots/_helpers/marketing-recorder";
import { recordingPlan, marketingCaptureDriver } from "./e2e/screenshots/recording-plan.mjs";
import { SCREENSHOT_MATRIX_PROJECTS } from "./e2e/screenshots/matrix";

const MAC_CAPTURE = process.platform === "darwin";
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3001";
// One source archive identity must survive Playwright worker restarts.
process.env.SCREENSHOT_RECORDING_RUN ??= `${new Date().toISOString().replace(/[:.]/g, "-")}-${process.pid}`;
const BROWSER_ENV: Record<string, string> = {};
for (const [key, value] of Object.entries(process.env)) {
  if (value !== undefined && (key !== "DISPLAY" || process.env.PWDEBUG)) {
    BROWSER_ENV[key] = value;
  }
}
const MARKETING_BROWSER_ENV = {
  ...BROWSER_ENV,
  ...(process.env.MARKETING_CAPTURE_DISPLAY
    ? { DISPLAY: process.env.MARKETING_CAPTURE_DISPLAY }
    : {}),
};

const config = defineConfig({
  testDir: "./e2e/screenshots",
  testMatch: ["**/*.spec.ts"],
  globalSetup: "./e2e/screenshots/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["./e2e/screenshots/manifest-reporter.ts"]],
  snapshotPathTemplate: "{testDir}/regression/__screenshots__/{arg}{ext}",

  use: {
    baseURL: BASE_URL,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    video: { mode: "retain-on-failure", size: { width: 1280, height: 720 } },
    screenshot: "only-on-failure",
    // 3D scenes need an explicit software WebGL backend in headless Chromium.
    launchOptions: {
      // A stale SSH DISPLAY makes ANGLE choose XCB and prevents SwiftShader startup.
      // Keep DISPLAY only for explicit PWDEBUG headed sessions.
      env: BROWSER_ENV,
      args: [
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
        "--ignore-gpu-blocklist",
        // SwiftShader's accelerated 2D canvas can retain old pixels after a resize.
        // Keep software WebGL for 3D scenes while drawing screenshot canvases on the CPU.
        "--disable-accelerated-2d-canvas",
      ],
    },
  },

  projects: [
    // ── 截图矩阵 ────────────────────────────────────────────────
    {
      name: "desktop-light",
      testMatch: ["**/screenshots.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 1,
        colorScheme: "light",
      },
    },
    {
      name: "desktop-dark",
      testMatch: ["**/screenshots.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 1,
        colorScheme: "dark",
      },
    },
    {
      name: "mobile",
      testMatch: ["**/screenshots.spec.ts"],
      use: {
        ...devices["iPhone 14"],
        browserName: "chromium",
        deviceScaleFactor: 1,
      },
    },
    // ── 流程预演（正式素材统一从营销母版衍生）────────────────────────────────
    {
      name: "flows",
      testMatch: ["**/flows/flows.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 810 },
        deviceScaleFactor: 1,
        video: "off",
        trace: "on",
      },
    },
    // ── 营销母版（1440×810；Linux 1.8 DPR / Mac 2 DPR → 4K60）──────
    {
      name: MARKETING_PROJECT_NAME,
      testMatch: ["**/flows/flows.spec.ts"],
      timeout: 120_000,
      use: {
        browserName: "chromium",
        headless: false,
        viewport: null,
        video: "off",
        trace: "retain-on-failure",
        launchOptions: {
          env: MARKETING_BROWSER_ENV,
          args: [
            // 起始位置避开屏幕左上角：macOS 26 会把贴边窗口吸进平铺槽并改写尺寸。
            "--window-position=22,55",
            "--window-size=1440,900",
            `--force-device-scale-factor=${MAC_CAPTURE ? 2 : 1.8}`,
            "--hide-scrollbars",
            ...(MAC_CAPTURE
              ? []
              : [
                  "--use-gl=angle",
                  "--use-angle=gl",
                  "--ignore-gpu-blocklist",
                  "--enable-gpu-rasterization",
                  "--disable-frame-rate-limit",
                  "--disable-gpu-vsync",
                ]),
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=CalculateNativeWinOcclusion",
            "--run-all-compositor-stages-before-draw",
          ],
        },
      },
    },
    // ── 视觉回归（同一 screenshot catalog + protocol stub）─────────────
    {
      name: "regression",
      testMatch: ["**/regression/regression.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 1,
        colorScheme: "light",
      },
    },
  ],
});

// An ML-service-free static run has its own catalog scope and only schedules the matrix projects.
const staticProfile = process.env.SCREENSHOT_STATIC_PROFILE;
const backendScope = process.env.SCREENSHOT_BACKEND_REQUIREMENTS;
if (staticProfile !== undefined) {
  if (staticProfile !== "non-ml") {
    throw new Error(`Unsupported SCREENSHOT_STATIC_PROFILE "${staticProfile}"; use "non-ml".`);
  }
  if (
    backendScope !== undefined ||
    process.env.SCREENSHOT_RECORDING_FLOWS !== undefined ||
    process.env.SCREENSHOT_RECORDING_PROFILE !== undefined
  ) {
    throw new Error("Static screenshot selection cannot be combined with a recording scope.");
  }
  config.projects = config.projects?.filter((project) =>
    SCREENSHOT_MATRIX_PROJECTS.includes(
      project.name as (typeof SCREENSHOT_MATRIX_PROJECTS)[number],
    ),
  );
  if (
    config.projects?.length !== SCREENSHOT_MATRIX_PROJECTS.length ||
    SCREENSHOT_MATRIX_PROJECTS.some(
      (name) => !config.projects?.some((project) => project.name === name),
    )
  ) {
    throw new Error("The non-ML static profile requires desktop-light, desktop-dark, and mobile.");
  }
} else if (backendScope !== undefined) {
  const plan = recordingPlan(
    (process.env.SCREENSHOT_RECORDING_FLOWS ?? "").split(","),
    process.env.SCREENSHOT_RECORDING_PROFILE,
  );
  if (plan.backendRequirements !== backendScope) {
    throw new Error("Recording flow selection does not match backend requirements");
  }
  if (plan.profile === "marketing") {
    marketingCaptureDriver(process.platform, process.env.MARKETING_CAPTURE_DRIVER, plan.flows);
  }
  const projectName = plan.profile === "docs" ? "flows" : MARKETING_PROJECT_NAME;
  config.projects = config.projects?.filter((project) => project.name === projectName);
  config.grep = new RegExp(plan.grep);
}

export default config;
