/**
 * v2 · 截图自动化主 driver。
 *
 * 执行：`pnpm screenshots`（手动触发，不进 CI）
 *
 * 前置条件：
 *   1. docker compose up -d
 *   2. uv run alembic upgrade head (apps/api)
 *   3. E2E_SEED_ENABLED=true uv run uvicorn app.main:app --port 8010
 *   4. PORT=3001 pnpm dev  (apps/web)
 *   5. cd apps/api && PYTHONPATH=. uv run python scripts/seed.py
 *
 * 矩阵规则：
 *   - scene 不声明 matrix → 只跑 desktop-light project
 *   - scene 声明 matrix.themes:['light','dark'] → desktop-light + desktop-dark 都跑
 *   - scene 声明 matrix.primaryTheme:'dark' → 暗色写入无后缀 target
 *   - scene 声明 matrix.viewports:['desktop','mobile'] → desktop-light + mobile 都跑
 *
 * 输出路径规则：
 *   - desktop + primaryTheme → 原 target 路径不加后缀
 *   - 非主主题               → 追加 .light / .dark 后缀
 *   - mobile                → 追加 .mobile 后缀
 */
import { test } from "../fixtures/seed";
import type { ScreenshotSeedCatalog } from "../fixtures/seed";
import { selectScreenshotScenes } from "./scenes/index";
import type { Role } from "./scenes/index";
import { injectAnnotations } from "./_helpers/annotate";
import { setupMockState } from "./_helpers/mock-state";
import { validateScreenshotFixture } from "./catalog";
import { loadScreenshotCatalog } from "./catalog-runtime";
import { PROJECT_AXES, resolveOutputPath, shouldRunInProject } from "./matrix";
import {
  applyScreenshotTheme,
  installScreenshotEnvironment,
  waitForScreenshotReady,
} from "./environment";
import path from "path";
import fs from "fs";

const HERE = decodeURIComponent(new URL(".", import.meta.url).pathname);
const REPO_ROOT = HERE.replace(/\/apps\/web\/e2e\/screenshots\/?$/, "");
const MANIFEST_PATH = path.join(REPO_ROOT, "apps/web/e2e/screenshots/outputs/manifest.json");
const VALIDATE_ONLY = process.env.SCREENSHOT_VALIDATE_ONLY === "1";

// 全局默认 mask 选择器（时间戳 / 头像 / 显式标记元素）
const DEFAULT_MASK_SELECTORS = [
  "[data-screenshot-mask]",
  "[data-testid='user-avatar']",
  "time[datetime]",
];

function readExistingManifest(): Record<string, { auto?: boolean }> {
  if (!fs.existsSync(MANIFEST_PATH)) return {};
  const raw = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8")) as {
    schema_version?: number;
    entries?: Record<string, { auto?: boolean }>;
  } & Record<string, { auto?: boolean }>;
  return raw.schema_version === 2 && raw.entries ? raw.entries : raw;
}

const existingManifest = readExistingManifest();

const screenshotSelection = selectScreenshotScenes(process.env.SCREENSHOT_STATIC_PROFILE);
if (process.env.SCREENSHOT_STATIC_PROFILE === "non-ml") {
  console.info(
    `[screenshots] static profile=non-ml; selected ${screenshotSelection.scenes.length} scene(s): ` +
      `${screenshotSelection.scenes.map((scene) => scene.name).join(", ")}; excluded ML scene(s): ` +
      screenshotSelection.excluded.join(", "),
  );
}

test.describe("screenshots automation", () => {
  let cached: ScreenshotSeedCatalog | null = null;

  test.beforeAll(() => {
    cached = loadScreenshotCatalog();
  });

  for (const scene of screenshotSelection.scenes) {
    test(scene.name, async ({ page, seed }, info) => {
      if (!cached) throw new Error("screenshot seed catalog 未完成");
      const catalog = cached;

      // 获取当前 Playwright project 对应的矩阵轴
      const axis =
        PROJECT_AXES[info.project.name as keyof typeof PROJECT_AXES] ??
        PROJECT_AXES["desktop-light"];

      // 不在此 project 跑的 scene 直接 skip
      if (!shouldRunInProject(scene, axis)) {
        test.skip();
        return;
      }

      validateScreenshotFixture(scene.name, scene.fixture, catalog);
      const route = scene.route(catalog);

      const emailMap: Record<Role, string> = {
        admin: catalog.users.admin.email,
        annotator: catalog.users.annotator.email,
        reviewer: catalog.users.reviewer.email,
      };
      const roleEmail = Array.isArray(scene.role) ? emailMap[scene.role[0]] : emailMap[scene.role];

      let cleanupMock: () => Promise<void> = async () => {};
      let cleanupAnnotations: () => Promise<void> = async () => {};

      let cleanupScene: () => Promise<void> = async () => {};
      try {
        // 激活网络 mock（如有）
        cleanupMock = await setupMockState(page, scene.mockState);

        const clock = await installScreenshotEnvironment(page, { clock: scene.clock });
        await seed.injectToken(page, roleEmail);
        await applyScreenshotTheme(page, axis.theme);
        await page.goto(route);
        if (scene.prepare) {
          await scene.prepare(page, catalog, (cleanup) => {
            cleanupScene = cleanup;
          });
        }
        await applyScreenshotTheme(page, axis.theme);
        await waitForScreenshotReady(page);

        // 注入 SVG 注释 overlay（如有）
        cleanupAnnotations = await injectAnnotations(page, scene.annotate);

        // 合并默认 mask + scene 级 mask
        const maskSelectors = [...DEFAULT_MASK_SELECTORS, ...(scene.mask ?? [])];
        const maskLocators = maskSelectors.map((sel) => page.locator(sel));

        // 确保输出目录存在
        const outRelative = resolveOutputPath(scene, axis);
        const out = `${REPO_ROOT}/${outRelative}`;
        if (!VALIDATE_ONLY) {
          if (existingManifest[outRelative]?.auto === false) {
            throw new Error(`${scene.name}: ${outRelative} 已标记为 auto:false，拒绝覆盖`);
          }
          fs.mkdirSync(path.dirname(out), { recursive: true });
        }

        // 按 capture 模式截图
        const capture = scene.capture;

        if (!capture) {
          if (!VALIDATE_ONLY) {
            await page.screenshot({
              path: out,
              fullPage: false,
              animations: "disabled",
              mask: maskLocators,
            });
          }
        } else if (capture.kind === "fullPage") {
          if (!VALIDATE_ONLY) {
            await page.screenshot({
              path: out,
              fullPage: true,
              animations: "disabled",
              mask: maskLocators,
            });
          }
        } else if (capture.kind === "locator") {
          const locator = page.locator(capture.selector);
          await locator.waitFor({ state: "visible", timeout: 10_000 });
          const box = await locator.boundingBox();
          if (!box) {
            throw new Error(`${scene.name}: 截图目标 ${capture.selector} 没有可见边界`);
          }
          if (VALIDATE_ONLY) {
            // locator 的可见性与边界已经在上方验证，无需写文件。
          } else if (capture.padding) {
            await page.screenshot({
              path: out,
              animations: "disabled",
              mask: maskLocators,
              clip: {
                x: Math.max(0, box.x - capture.padding),
                y: Math.max(0, box.y - capture.padding),
                width: box.width + capture.padding * 2,
                height: box.height + capture.padding * 2,
              },
            });
          } else {
            await page.screenshot({
              path: out,
              animations: "disabled",
              mask: maskLocators,
              clip: { x: box.x, y: box.y, width: box.width, height: box.height },
            });
          }
        } else if (capture.kind === "clip") {
          if (!VALIDATE_ONLY) {
            await page.screenshot({
              path: out,
              animations: "disabled",
              mask: maskLocators,
              clip: capture.rect,
            });
          }
        }

        info.annotations.push({ type: "screenshot", description: outRelative });
        await info.attach("screenshot-manifest", {
          body: Buffer.from(
            JSON.stringify({
              target: outRelative,
              scene: scene.name,
              source: scene.source,
              capture: capture ?? { kind: "viewport" },
              fixture: scene.fixture ?? null,
              seed_revision: catalog.seed_revision,
              project: info.project.name,
              viewport: page.viewportSize(),
              theme: axis.theme,
              clock,
              locale: axis.locale,
              browser: {
                name: page.context().browser()?.browserType().name() ?? "chromium",
                version: page.context().browser()?.version() ?? "unknown",
              },
            }),
          ),
          contentType: "application/json",
        });
      } finally {
        try {
          await cleanupAnnotations();
        } finally {
          try {
            await cleanupMock();
          } finally {
            try {
              await cleanupScene();
            } finally {
              // scene.prepare 也可能注册局部 route；测试失败时一并清除。
              await page.unrouteAll({ behavior: "ignoreErrors" });
            }
          }
        }
      }
    });
  }
});
