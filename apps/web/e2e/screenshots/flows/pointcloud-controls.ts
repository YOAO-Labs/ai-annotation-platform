/**
 * 流程录制：点云工作台控件演示(pointcloud-controls) —— 相机上色 / 点大小 / 深度提示。
 *
 * 输出：outputs/flows/pointcloud-controls.gif → docs-site/.../workbench/pointcloud-controls-bar.gif
 *
 * 数据来自 screenshots seed 的 pointcloud_demo（nuScenes mini 自动驾驶场景）。这些控件不在画面浮条上，
 * 而在「工作台设置」窗口的「画布与视角」分类里(toggle/slider)。
 * 本 flow 纯切设置不落标注；账号级偏好 PATCH 由 flows.spec 的录制沙箱在内存中响应，不写回服务端。
 *
 * 点云 PCD 由前端按需加载并经真实硬件 WebGL 渲染，故进入后多等一段让点云就绪。
 *
 * 返回 { drawStartMs, drawEndMs }：供 finalize 裁掉开头(导航/解析/点云加载等待)。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import {
  installRecordingWorkbenchLayout,
  recordingLayoutCommand,
  waitForRecordingPanels,
  waitForRecordingWorkbenchLayout,
} from "./_workbench-layout";

export async function runPointcloudControls(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.pointcloud_demo;
  await installRecordingWorkbenchLayout(page, "both", {
    workspace: { context: "annotate:3d", preset: "standard" },
  });
  await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.frame_000.id}`);
  // 点云工作台持续渲染 + 可能轮询, networkidle 不会稳定 settle; 用 domcontentloaded + 显式等待。
  await page.waitForLoadState("domcontentloaded");

  // 等 3D 视口挂载 + 点云经 WebGL 渲染(PCD 异步加载, SwiftShader 较慢, 多等一段)。
  await page.getByTestId("pc-viewport").waitFor({ timeout: 20_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await page.waitForTimeout(4000);

  await recordingLayoutCommand(page, "传感器融合");
  await recordingLayoutCommand(page, "全部相机停靠");
  await waitForRecordingPanels(page, ["canvas", "camera-view"], ["tri-view"]);
  const frontCamera = page
    .locator('[data-workbench-panel="camera-view"] section')
    .filter({ has: page.locator('img[alt="CAM_FRONT"]') });
  await expect(frontCamera).toBeVisible();
  await expect
    .poll(() =>
      frontCamera
        .locator('img[alt="CAM_FRONT"]')
        .evaluate((node) => (node instanceof HTMLImageElement ? node.naturalWidth : 0)),
    )
    .toBeGreaterThan(0);

  const drawStartMs = Date.now();

  // ── 打开工作台设置窗口(Topbar 齿轮; 居中设置窗口)──
  await page.getByRole("button", { name: "工作台设置" }).first().click();
  await page.getByTestId("workbench-settings-dialog").waitFor({ timeout: 5000 });
  await page.getByRole("tab", { name: "画布与视角", exact: true }).click();
  await page.waitForTimeout(800);

  // ── 相机上色 ON：点云从高度色 → 采样相机 RGB，关闭窗口后检查效果──
  // 使用可访问开关直接切换。
  const colorize = page.getByTestId("setting-field-pointcloud.colorizeWithCamera");
  await colorize.scrollIntoViewIfNeeded();
  await colorize.getByRole("switch").click();
  await page.waitForTimeout(1600);

  // ── 点大小调大：拖滑块(键盘步进)放大点径, 点云更饱满 ──
  const sizeSlider = page.getByTestId("setting-field-pointcloud.pointSize").getByRole("slider");
  await sizeSlider.scrollIntoViewIfNeeded();
  await sizeSlider.focus();
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(900);

  // ── 深度提示 ON：相机视图叠加深度热力 ──
  const depth = page.getByTestId("setting-field-pointcloud.showDepthHint");
  await depth.scrollIntoViewIfNeeded();
  await depth.getByRole("switch").click();
  await page.waitForTimeout(1400);

  // ── 关闭设置窗口, 露出完整重着色 + 放大后的点云, 停留展示 ──
  await page.keyboard.press("Escape");
  await page.waitForTimeout(4200);
  await expect
    .poll(() =>
      frontCamera.locator("canvas").evaluate((node) => {
        if (!(node instanceof HTMLCanvasElement)) return 0;
        const context = node.getContext("2d");
        if (!context) return 0;
        const pixels = context.getImageData(0, 0, node.width, node.height).data;
        let coloredPixels = 0;
        for (let index = 0; index < pixels.length; index += 4) {
          const red = pixels[index] ?? 0;
          const green = pixels[index + 1] ?? 0;
          const blue = pixels[index + 2] ?? 0;
          if (
            (pixels[index + 3] ?? 0) > 0 &&
            Math.max(red, green, blue) - Math.min(red, green, blue) > 20
          ) {
            coloredPixels += 1;
          }
        }
        return coloredPixels;
      }),
    )
    .toBeGreaterThan(100);

  const drawEndMs = Date.now();
  return { drawStartMs, drawEndMs };
}
