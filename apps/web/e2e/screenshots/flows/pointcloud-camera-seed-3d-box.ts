/**
 * 由真实视锥点拟合生成可复核提案，再用 3D PSR 数值控件按原始 2D 提示人工收尺寸，
 * 并保留提案与 PATCH 回读几何；该能力不依赖 ML 后端，也不存在候选采纳步骤。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog, SeedTaskAnnotation } from "../../fixtures/seed";
import { movePointerAtRefreshRate, recordingAnchor } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";
import { recordingLayoutCommand, waitForRecordingPanels } from "./_workbench-layout";

type PointcloudBoxGeometry = {
  type: string;
  center: number[];
  size: number[];
  rotation: number[];
};

export interface PointcloudCameraSeedResult extends DrawWindow {
  created: { taskId: string; annotationId: string };
  /** The untouched geometry returned by the real camera-seed POST. */
  geometry: PointcloudBoxGeometry;
  /** The geometry read back from the real PSR-field PATCH after visual refinement. */
  refinedGeometry: PointcloudBoxGeometry;
}

interface CreatedPointcloudAnnotation {
  id: string;
  task_id: string;
  annotation_type: string;
  class_name: string;
  geometry: PointcloudBoxGeometry;
}

function normalizedRect(
  box: { x: number; y: number; width: number; height: number },
  bbox: [number, number, number, number],
) {
  return {
    start: { x: box.x + box.width * bbox[0], y: box.y + box.height * bbox[1] },
    end: { x: box.x + box.width * bbox[2], y: box.y + box.height * bbox[3] },
  };
}

interface NormalizedProjectionBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Read the actual rendered 3D projection overlay, not a synthetic geometry estimate. */
async function readProjectionBounds(
  cameraCanvas: Locator,
): Promise<NormalizedProjectionBounds | null> {
  return cameraCanvas.evaluate((element) => {
    if (!(element instanceof HTMLCanvasElement) || !element.width || !element.height) return null;
    const context = element.getContext("2d");
    if (!context) return null;
    const { data } = context.getImageData(0, 0, element.width, element.height);
    let minX = element.width;
    let minY = element.height;
    let maxX = -1;
    let maxY = -1;
    for (let offset = 3; offset < data.length; offset += 4) {
      if (data[offset] <= 8) continue;
      const pixel = (offset - 3) / 4;
      const x = pixel % element.width;
      const y = Math.floor(pixel / element.width);
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
    if (maxX < minX || maxY < minY) return null;
    return {
      x: minX / element.width,
      y: minY / element.height,
      width: (maxX - minX + 1) / element.width,
      height: (maxY - minY + 1) / element.height,
    };
  });
}

/** Locate the rendered box, then use the product's double-click focus gesture. */
async function focusCreatedBox(page: Page, annotationId: string): Promise<void> {
  const viewport = page.getByTestId("pc-viewport");
  const point = await viewport.evaluate((element, id) => {
    const scene = (
      element as HTMLElement & {
        __pointCloudScene?: {
          boxGroups: Map<
            string,
            {
              position: { clone: () => { project: (camera: unknown) => { x: number; y: number } } };
            }
          >;
          camera: unknown;
          getViewState: () => { target: number[] };
        };
      }
    ).__pointCloudScene;
    const group = scene?.boxGroups.get(id);
    if (!scene || !group) throw new Error("Rendered 3D box is unavailable for focus");
    const projected = group.position.clone().project(scene.camera);
    const bounds = element.getBoundingClientRect();
    return {
      x: bounds.left + ((projected.x + 1) * bounds.width) / 2,
      y: bounds.top + ((1 - projected.y) * bounds.height) / 2,
      previousTarget: scene.getViewState().target,
    };
  }, annotationId);
  await page.mouse.dblclick(point.x, point.y);
  await expect
    .poll(() =>
      viewport.evaluate((element) => {
        return (
          element as HTMLElement & {
            __pointCloudScene?: {
              getViewState: () => { target: number[] };
            };
          }
        ).__pointCloudScene?.getViewState().target;
      }),
    )
    .not.toEqual(point.previousTarget);
  await page.waitForTimeout(1500);
}

export async function runPointcloudCameraSeed3dBox(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated?: (created: PointcloudCameraSeedResult["created"]) => void,
): Promise<PointcloudCameraSeedResult> {
  const project = catalog.projects.pointcloud_demo;
  const task = project.tasks.frame_000;
  const anchor = recordingAnchor(catalog, "pointcloud_demo", "frame_000", "foreground_object");
  const annotationPath = `/api/v1/tasks/${task.id}/annotations`;
  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  await page.waitForLoadState("domcontentloaded");

  await page.getByTestId("pc-viewport").waitFor({ timeout: 20_000 });
  await recordingLayoutCommand(page, "传感器融合");
  await recordingLayoutCommand(page, "全部相机停靠");
  await waitForRecordingPanels(page, ["canvas", "camera-view"]);
  const frontCamera = page
    .locator("[data-camera-dock-panel]")
    .getByRole("region", { name: "CAM_FRONT", exact: true });
  await expect(frontCamera).toBeVisible({ timeout: 10_000 });
  const cameraImage = frontCamera.locator("img");
  await expect(cameraImage).toBeVisible({ timeout: 10_000 });
  await cameraImage.evaluate(async (image: HTMLImageElement) => {
    if (image.complete && image.naturalWidth > 0) return;
    await new Promise<void>((resolve, reject) => {
      image.addEventListener("load", () => resolve(), { once: true });
      image.addEventListener("error", () => reject(new Error("相机图加载失败")), { once: true });
    });
  });
  await page.waitForTimeout(4_000);

  const drawStartMs = Date.now();
  await page.waitForTimeout(1_400);

  await frontCamera.getByRole("button", { name: "放大CAM_FRONT", exact: true }).click();
  const seedButton = page.getByRole("button", { name: "种框 ⊹" });
  await seedButton.waitFor({ state: "visible", timeout: 5_000 });
  const modalBody = page.getByTestId("camera-modal-body");
  await page.waitForTimeout(1_200);
  await seedButton.click();

  const cameraCanvas = modalBody.getByLabel(/^CAM_FRONT 相机投影(?:，|$)/);
  await expect(cameraCanvas).toBeVisible();
  const box = await cameraCanvas.boundingBox();
  if (!box) throw new Error("[pointcloud-camera-seed-3d-box] 放大相机画布不可见");
  const rect = normalizedRect(box, anchor.bbox);

  await page.mouse.move(rect.start.x, rect.start.y);
  await page.waitForTimeout(450);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, rect.start, rect.end, 950);
  await page.waitForTimeout(300);
  const createdResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === annotationPath &&
      response.status() === 201,
    { timeout: 10_000 },
  );
  await page.mouse.up();

  const createdResponse = await createdResponsePromise;
  const created = (await createdResponse.json()) as CreatedPointcloudAnnotation;
  onCreated?.({ taskId: task.id, annotationId: created.id });
  if (
    created.task_id !== task.id ||
    created.annotation_type !== "box_3d" ||
    created.class_name !== anchor.label ||
    created.geometry.type !== "box_3d" ||
    ![...created.geometry.center, ...created.geometry.size, ...created.geometry.rotation].every(
      Number.isFinite,
    ) ||
    created.geometry.size.some((value) => value <= 0)
  ) {
    throw new Error(
      `[pointcloud-camera-seed-3d-box] 3D 框落库结果无效: ${JSON.stringify(created)}`,
    );
  }

  await expect(page.locator(`[data-testid="box-list-item-${created.id}"]`)).toBeVisible({
    timeout: 10_000,
  });
  await page.waitForTimeout(1_500);
  const proposalProjection = await readProjectionBounds(cameraCanvas);
  if (!proposalProjection) {
    throw new Error("[pointcloud-camera-seed-3d-box] 相机图未渲染已保存 3D 框投影");
  }
  const targetWidth = anchor.bbox[2] - anchor.bbox[0];
  const targetHeight = anchor.bbox[3] - anchor.bbox[1];
  const targetCenterX = (anchor.bbox[0] + anchor.bbox[2]) / 2;
  const targetCenterY = (anchor.bbox[1] + anchor.bbox[3]) / 2;
  const sizeScale = Math.min(
    1,
    (targetWidth * 0.96) / proposalProjection.width,
    (targetHeight * 0.96) / proposalProjection.height,
  );
  if (!Number.isFinite(sizeScale) || sizeScale <= 0 || sizeScale >= 0.98) {
    throw new Error(
      `[pointcloud-camera-seed-3d-box] 种框提案无需收缩或投影目标无效: ${JSON.stringify({
        proposalProjection,
        target: anchor.bbox,
      })}`,
    );
  }

  await page.getByRole("button", { name: "关闭 ✕" }).click();
  await expect(page.getByRole("button", { name: "种框 ⊹" })).toBeHidden();
  await page.waitForTimeout(1_200);

  await recordingLayoutCommand(page, "框体精修");
  await waitForRecordingPanels(page, ["canvas", "tri-view"], ["camera-view"]);
  await expect(page.getByTestId("tri-view-renderer-panel")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByLabel(/^俯视精修视图，缩放 /)).toBeVisible();
  await focusCreatedBox(page, created.id);
  await page.waitForTimeout(1_500);
  const selectionPanel = page.getByTestId("three-d-selection-panel").first();
  await expect(selectionPanel).toBeVisible({ timeout: 5_000 });
  const expandDetails = selectionPanel.getByRole("button", { name: "展开详情" });
  if (await expandDetails.isVisible()) await expandDetails.click();
  const sizeFields = ["l", "w", "h"].map((field) =>
    selectionPanel.getByLabel(field, { exact: true }),
  );
  const currentSize = await Promise.all(
    sizeFields.map(async (field) => Number(await field.inputValue())),
  );
  if (!currentSize.every((value) => Number.isFinite(value) && value > 0)) {
    throw new Error("[pointcloud-camera-seed-3d-box] PSR 尺寸字段无效");
  }

  const updatePath = `/api/v1/tasks/${task.id}/annotations/${created.id}`;
  const updatePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname === updatePath &&
      response.ok(),
    { timeout: 10_000 },
  );
  for (const [index, field] of sizeFields.entries()) {
    await field.fill((currentSize[index] * sizeScale).toFixed(2));
  }
  await selectionPanel.getByText("尺寸 长宽高 (m)", { exact: true }).click();
  const updated = (await (await updatePromise).json()) as SeedTaskAnnotation;
  const refinedGeometry = updated.geometry as PointcloudBoxGeometry;
  if (
    updated.id !== created.id ||
    updated.task_id !== task.id ||
    refinedGeometry.type !== "box_3d" ||
    ![...refinedGeometry.center, ...refinedGeometry.size, ...refinedGeometry.rotation].every(
      Number.isFinite,
    ) ||
    refinedGeometry.size.some((value, index) => value >= created.geometry.size[index])
  ) {
    throw new Error(
      `[pointcloud-camera-seed-3d-box] 人工尺寸修正未从 API 读回: ${JSON.stringify(updated)}`,
    );
  }
  await page.waitForTimeout(1_500);

  const viewport = page.getByTestId("pc-viewport");
  const viewportBox = await viewport.boundingBox();
  if (!viewportBox) throw new Error("[pointcloud-camera-seed-3d-box] 点云视口不可见");
  const orbitStart = {
    x: viewportBox.x + viewportBox.width * 0.34,
    y: viewportBox.y + viewportBox.height * 0.55,
  };
  const orbitEnd = {
    x: viewportBox.x + viewportBox.width * 0.45,
    y: viewportBox.y + viewportBox.height * 0.48,
  };
  await page.mouse.move(orbitStart.x, orbitStart.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, orbitStart, orbitEnd, 700);
  await page.mouse.up();
  await page.waitForTimeout(2_500);

  // 回到同步相机图完成因果闭环：初始 2D 提示已生成真实 3D 框，
  // 核对空间包围后，最后再展示该框稳定重投影到原目标。
  await recordingLayoutCommand(page, "传感器融合");
  await waitForRecordingPanels(page, ["canvas", "camera-view"]);
  await frontCamera.getByRole("button", { name: "放大CAM_FRONT", exact: true }).click();
  await expect(page.getByRole("button", { name: "关闭 ✕" })).toBeVisible();
  await expect(page.getByRole("button", { name: "种框 ⊹" })).toBeVisible();
  await page.waitForTimeout(2_800);
  const finalProjection = await readProjectionBounds(cameraCanvas);
  if (
    !finalProjection ||
    finalProjection.width > targetWidth * 1.12 ||
    finalProjection.width < targetWidth * 0.6 ||
    finalProjection.height > targetHeight * 1.12 ||
    finalProjection.height < targetHeight * 0.6 ||
    Math.abs(finalProjection.x + finalProjection.width / 2 - targetCenterX) > targetWidth * 0.16 ||
    Math.abs(finalProjection.y + finalProjection.height / 2 - targetCenterY) > targetHeight * 0.16
  ) {
    throw new Error(
      `[pointcloud-camera-seed-3d-box] 精修框仍未与二维目标对齐: ${JSON.stringify({
        finalProjection,
        target: anchor.bbox,
      })}`,
    );
  }
  await page.waitForTimeout(2_400);

  return {
    drawStartMs,
    drawEndMs: Date.now(),
    created: { taskId: task.id, annotationId: created.id },
    geometry: created.geometry,
    refinedGeometry,
  };
}
