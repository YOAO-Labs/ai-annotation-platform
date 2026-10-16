/**
 * 高清母版：展示 3D 工作台的面板编排与跨帧保持。
 *
 * 三视图作为一个 Dockview 面板依次停靠、浮动、隐藏并恢复；相机按整组在
 * 悬浮与停靠图库之间切换。所有检查都基于真实 nuScenes 点云、相机图和选中
 * 框的 DOM/renderer 状态，最后通过 Scene 时间轴切到相邻帧。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog, SeedTaskAnnotation } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import {
  recordingLayoutCommand,
  recordingPanelCommand,
  waitForRecordingPanels,
} from "./_workbench-layout";

export interface PointcloudPanelLayoutResult extends DrawWindow {
  sourceAnnotationId: string;
  frameIds: string[];
  cameraRoles: string[];
}

async function waitForPointcloudFrame(page: Page): Promise<void> {
  await page.getByTestId("pc-viewport").waitFor({ timeout: 20_000 });
  await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("pc-viewport")).toHaveAttribute(
    "data-pointcloud-renderer-count",
    "1",
  );
  await page.waitForTimeout(2_000);
}

async function expectTriViewReady(page: Page): Promise<void> {
  await expect(page.getByTestId("tri-view-renderer-panel")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByLabel(/^俯视精修视图/)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByLabel(/^侧视精修视图/)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByLabel(/^正视精修视图/)).toBeVisible({ timeout: 10_000 });
  await expect
    .poll(async () =>
      Number(await page.getByTestId("pc-viewport").getAttribute("data-pointcloud-tri-pass-count")),
    )
    .toBeGreaterThan(0);
}

type CameraImageScope = "floating" | "docked";
type CameraImageSource = {
  role: string;
  pathname: string;
  filename: string;
};

function cameraImageRoot(page: Page, scope: CameraImageScope) {
  return scope === "floating"
    ? page.getByTestId("camera-panel-layer")
    : page.locator('[data-workbench-panel="camera-view"]');
}

async function expandFloatingCameraPanels(page: Page, roles: readonly string[]): Promise<void> {
  const layer = page.getByTestId("camera-panel-layer");
  for (const role of roles) {
    const tab = layer.locator('button[title="展开相机"]').filter({ hasText: role }).first();
    if (await tab.count()) await tab.click();
    await expect(layer.locator(`img[alt="${role}"]:visible`)).toBeVisible({ timeout: 15_000 });
  }
}

async function expectCameraImages(
  page: Page,
  roles: readonly string[],
  scope: CameraImageScope,
): Promise<void> {
  const root = cameraImageRoot(page, scope);
  for (const role of roles) {
    const image = root.locator(`img[alt="${role}"]:visible`).first();
    await expect(image).toBeVisible({ timeout: 15_000 });
    await expect
      .poll(async () => image.evaluate((node) => (node as HTMLImageElement).naturalWidth))
      .toBeGreaterThan(0);
  }
}

async function selectSourceBox(page: Page, annotationId: string): Promise<void> {
  const item = page.locator(`[data-testid="box-list-item-${annotationId}"]`);
  await expect(item).toBeVisible({ timeout: 15_000 });
  await item.click();
  await expect(item).toHaveClass(/border-brand/);
}

async function cameraImageSources(
  page: Page,
  roles: readonly string[],
  scope: CameraImageScope,
): Promise<CameraImageSource[]> {
  const root = cameraImageRoot(page, scope);
  return Promise.all(
    roles.map(async (role) => {
      const source = await root.locator(`img[alt="${role}"]:visible`).first().getAttribute("src");
      if (!source) throw new Error(`[pointcloud-panel-layout] ${role} 相机图缺少真实 URL`);
      // Signed URLs may change query parameters while the underlying frame stays
      // the same. Compare the public path/filename so the frame transition is
      // asserted per camera role rather than by an opaque URL string.
      const pathname = decodeURIComponent(new URL(source, page.url()).pathname);
      return { role, pathname, filename: pathname.split("/").pop() || pathname };
    }),
  );
}

export async function runPointcloudPanelLayout(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  source: SeedTaskAnnotation,
): Promise<PointcloudPanelLayoutResult> {
  const project = catalog.projects.pointcloud_demo;
  const frame0 = project.tasks.frame_000;
  const frame1 = project.tasks.frame_001;
  const cameraRoles = [
    "CAM_FRONT",
    "CAM_FRONT_RIGHT",
    "CAM_BACK_RIGHT",
    "CAM_BACK",
    "CAM_BACK_LEFT",
    "CAM_FRONT_LEFT",
  ];

  await page.goto(`/projects/${project.id}/annotate?task=${frame0.id}`);
  await page.waitForLoadState("domcontentloaded");
  await waitForPointcloudFrame(page);
  await selectSourceBox(page, source.id);
  const sourceItem = page.locator(`[data-testid="box-list-item-${source.id}"]`);
  const rendererCanvas = page.locator("[data-workbench-render-surface] > canvas");
  await expect(rendererCanvas).toHaveCount(1);
  const rendererCanvasHandle = await rendererCanvas.elementHandle();
  if (!rendererCanvasHandle) throw new Error("[pointcloud-panel-layout] 主渲染画布不可见");
  const assertStableCanvasAndSelection = async () => {
    await expect(rendererCanvas).toHaveCount(1);
    expect(
      await rendererCanvas.evaluate((node, original) => node === original, rendererCanvasHandle),
    ).toBe(true);
    await expect(sourceItem).toHaveClass(/border-brand/);
  };

  // 先把选中框放入三视图，再实际改变 Dockview 拓扑；同一个 renderer、框和底图
  // 必须穿过每次停靠/浮动/隐藏操作。
  await recordingLayoutCommand(page, "框体精修");
  await waitForRecordingPanels(page, ["canvas", "tri-view"], ["camera-view"]);
  await expectTriViewReady(page);
  await assertStableCanvasAndSelection();
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_200);

  await recordingPanelCommand(page, "三视图精修", "停靠到左侧");
  await waitForRecordingPanels(page, ["canvas", "tri-view"], ["camera-view"]);
  await expectTriViewReady(page);
  await assertStableCanvasAndSelection();
  await page.waitForTimeout(1_200);

  await recordingPanelCommand(page, "三视图精修", "浮动面板");
  await expect
    .poll(async () =>
      page
        .locator(".dv-groupview-floating")
        .filter({ has: page.getByRole("button", { name: "隐藏三视图精修", exact: true }) })
        .count(),
    )
    .toBe(1);
  await expectTriViewReady(page);
  await assertStableCanvasAndSelection();
  await page.waitForTimeout(1_500);

  await page.getByRole("button", { name: "隐藏三视图精修", exact: true }).click();
  await waitForRecordingPanels(page, ["canvas"], ["tri-view", "camera-view"]);
  await page.waitForTimeout(900);
  await recordingLayoutCommand(page, "三视图精修");
  await waitForRecordingPanels(page, ["canvas", "tri-view"], ["camera-view"]);
  await expectTriViewReady(page);
  await assertStableCanvasAndSelection();
  const topView = page.getByLabel(/^俯视精修视图/);
  const sideView = page.getByLabel(/^侧视精修视图/);
  const frontView = page.getByLabel(/^正视精修视图/);
  await expect(topView).toHaveAttribute("aria-label", "俯视精修视图，缩放 100%");
  await expect(sideView).toHaveAttribute("aria-label", "侧视精修视图，缩放 100%");
  await expect(frontView).toHaveAttribute("aria-label", "正视精修视图，缩放 100%");
  const topViewBounds = await topView.boundingBox();
  if (!topViewBounds) throw new Error("[pointcloud-panel-layout] 俯视精修视图不可见");
  await page.mouse.move(
    topViewBounds.x + topViewBounds.width / 2,
    topViewBounds.y + topViewBounds.height / 2,
  );
  await page.mouse.wheel(0, -120);
  await expect(topView).toHaveAttribute("aria-label", "俯视精修视图，缩放 112%");
  await expect(sideView).toHaveAttribute("aria-label", "侧视精修视图，缩放 100%");
  await expect(frontView).toHaveAttribute("aria-label", "正视精修视图，缩放 100%");
  await assertStableCanvasAndSelection();
  await page.waitForTimeout(1_400);
  await topView.focus();
  await page.keyboard.press("Shift+Digit0");
  await expect(topView).toHaveAttribute("aria-label", "俯视精修视图，缩放 100%");
  await assertStableCanvasAndSelection();
  await page.waitForTimeout(1_100);
  await page.waitForTimeout(1_200);

  // 传感器融合保留已选框，按物理 camera role 显示六路实景图；先验证悬浮，再
  // 用顶部布局菜单将整组收入图库，并从图库标题栏隐藏/恢复。
  await recordingLayoutCommand(page, "传感器融合");
  await waitForRecordingPanels(page, ["canvas"], ["tri-view", "camera-view"]);
  await expect(page.getByTestId("camera-panel-layer")).toBeVisible({ timeout: 15_000 });
  await expandFloatingCameraPanels(page, cameraRoles);
  await expectCameraImages(page, cameraRoles, "floating");
  await assertStableCanvasAndSelection();
  const frameZeroCameraSources = await cameraImageSources(page, cameraRoles, "floating");
  await page.waitForTimeout(1_200);

  await recordingLayoutCommand(page, "全部相机停靠");
  await waitForRecordingPanels(page, ["canvas", "camera-view"], ["tri-view"]);
  const gallery = page.locator('[data-workbench-panel="camera-view"]');
  await expect(gallery.locator("[data-camera-dock-panel] section")).toHaveCount(cameraRoles.length);
  await expectCameraImages(page, cameraRoles, "docked");
  await assertStableCanvasAndSelection();
  await page.waitForTimeout(1_500);

  await page.getByRole("button", { name: "隐藏相机视图", exact: true }).click();
  await waitForRecordingPanels(page, ["canvas"], ["camera-view", "tri-view"]);
  await page.waitForTimeout(800);
  await recordingLayoutCommand(page, "相机视图");
  await waitForRecordingPanels(page, ["canvas", "camera-view"], ["tri-view"]);
  await expectCameraImages(page, cameraRoles, "docked");
  await assertStableCanvasAndSelection();
  await page.waitForTimeout(1_400);

  // 用 Scene 时间轴的真实下一帧按钮切换任务，核对整组相机和 renderer 在新帧仍
  // 有完整内容；URL 是公开任务切换证据，不能只依赖内部状态。
  const nextFrame = page.getByRole("button", { name: "下一帧", exact: true });
  await expect(nextFrame).toBeVisible({ timeout: 10_000 });
  await nextFrame.click();
  await expect(page).toHaveURL(new RegExp(`task=${frame1.id}`), { timeout: 15_000 });
  await waitForPointcloudFrame(page);
  await waitForRecordingPanels(page, ["canvas", "camera-view"], ["tri-view"]);
  await expectCameraImages(page, cameraRoles, "docked");
  await expect
    .poll(async () => cameraImageSources(page, cameraRoles, "docked"))
    .not.toEqual(frameZeroCameraSources);
  await page.waitForTimeout(2_200);

  return {
    drawStartMs,
    drawEndMs: Date.now(),
    sourceAnnotationId: source.id,
    frameIds: [frame0.id, frame1.id],
    cameraRoles,
  };
}
