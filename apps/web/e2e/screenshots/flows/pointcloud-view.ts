/**
 * 流程录制：点云视图导航(pointcloud-view) —— 拖动主 3D 视图旋转点云(orbit)。
 *
 * 输出：outputs/flows/pointcloud-view.gif → docs-site/.../workbench/pointcloud-view-orbit.gif
 *
 * 数据复用 pointcloud-controls 的 P-NU-nuscenes-mini(nuScenes-mini, lidar)。本 flow 纯导航(不开
 * 任何工具、不落标注)：默认模式下 OrbitControls 左键 = ROTATE, 在 pc-viewport 内左键拖拽即沿轨道
 * 环绕点云, 再滚轮拉近一档。设置走 localStorage(每次全新 context 默认态), 故无需 afterAll 清理。
 *
 * 录制保留当前 Dockview 两侧面板，让点云视图与紧凑工具栏处在完整工作台语境中。
 *
 * 返回 { drawStartMs, drawEndMs }：供 finalize 裁掉开头(导航/解析/点云加载等待 + 收边栏)。
 */
import type { Locator, Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";
import {
  installRecordingWorkbenchLayout,
  waitForRecordingWorkbenchLayout,
} from "./_workbench-layout";

/** 在 pc-viewport 内做一次左键拖拽(orbit)。 */
async function dragOrbit(
  page: Page,
  box: { x: number; y: number; width: number; height: number },
  from: { dx: number; dy: number },
  to: { dx: number; dy: number },
): Promise<void> {
  const sx = box.x + box.width * from.dx;
  const sy = box.y + box.height * from.dy;
  const ex = box.x + box.width * to.dx;
  const ey = box.y + box.height * to.dy;
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, { x: sx, y: sy }, { x: ex, y: ey }, 900);
  await page.mouse.up();
  await page.waitForTimeout(350);
}

/** 读取点云场景的真实相机方向，保证录制结尾没有回到初始视角。 */
async function viewDirection(viewport: Locator): Promise<[number, number, number]> {
  return viewport.evaluate((element) => {
    const scene = (
      element as HTMLElement & {
        __pointCloudScene?: {
          getViewState: () => {
            position: [number, number, number];
            target: [number, number, number];
          };
        };
      }
    ).__pointCloudScene;
    if (!scene) throw new Error("[pointcloud-view] 点云视图状态不可用");
    const view = scene.getViewState();
    const direction = view.position.map((value, index) => value - view.target[index]);
    const length = Math.hypot(...direction);
    if (!Number.isFinite(length) || length === 0) {
      throw new Error("[pointcloud-view] 点云视图方向无效");
    }
    return direction.map((value) => value / length) as [number, number, number];
  });
}

export async function runPointcloudView(
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
  const viewport = page.getByTestId("pc-viewport");
  await viewport.waitFor({ timeout: 20_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await page.waitForTimeout(4000);

  const box = await viewport.boundingBox();
  if (!box) throw new Error("[pointcloud-view] pc-viewport 没有可见边界");
  const startingDirection = await viewDirection(viewport);

  const drawStartMs = Date.now();
  await page.waitForTimeout(1_500);

  // ── 左键拖拽 orbit：默认模式下 OrbitControls LEFT=ROTATE, 拖拽即沿轨道环绕点云 ──
  // 第一段：水平向右拖(绕竖直轴转), 视角横扫。
  await dragOrbit(page, box, { dx: 0.32, dy: 0.5 }, { dx: 0.7, dy: 0.42 });
  // 第二段：反向 + 略带俯仰, 让点云换个角度。
  await dragOrbit(page, box, { dx: 0.68, dy: 0.45 }, { dx: 0.35, dy: 0.62 });
  // 第三、四段分别强调仰视和水平回看，形成完整的多角度检查链路。
  await dragOrbit(page, box, { dx: 0.46, dy: 0.62 }, { dx: 0.55, dy: 0.28 });
  await dragOrbit(page, box, { dx: 0.56, dy: 0.34 }, { dx: 0.28, dy: 0.48 });

  // ── 滚轮拉近一档(dolly), 展示缩放查看 ──
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  for (let i = 0; i < 3; i++) {
    await page.mouse.wheel(0, -120);
    await page.waitForTimeout(150);
  }
  await page.waitForTimeout(1_000);

  // 拉远一档核对整体结构，再停在新视角展示空间关系。
  for (let i = 0; i < 2; i++) {
    await page.mouse.wheel(0, 120);
    await page.waitForTimeout(180);
  }
  // Keep the final orbit as the end state instead of resetting back to the opening top view.
  await dragOrbit(page, box, { dx: 0.54, dy: 0.46 }, { dx: 0.34, dy: 0.58 });
  await page.waitForTimeout(2_400);
  const endingDirection = await viewDirection(viewport);
  const directionDot = startingDirection.reduce(
    (sum, value, index) => sum + value * endingDirection[index],
    0,
  );
  if (directionDot >= 0.94) {
    throw new Error("[pointcloud-view] 结束视角未明显偏离初始视角");
  }
  const drawEndMs = Date.now();
  return { drawStartMs, drawEndMs };
}
