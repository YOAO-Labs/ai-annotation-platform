/**
 * 流程录制：已保存 Raster Mask 的直线切割为两个实例。
 *
 * 输出：.artifacts/marketing/<run>/masters/mask-slice-instances → docs-site/public/media/mask/slice-instances.mp4
 *
 * 准备（不进母版）：按截图库 image_demo.annotating 的真实媒体尺寸（1280×720）用正式
 * mask-content API 上传一个覆盖前后相邻两辆车的 Raster Mask（后车 = catalog
 * primary_vehicle 多边形锚点，前车 = 同一张 screenshot_03 上量得的厢式货车尾部轮廓），
 * 再以 region 工具单元落库为 raster_mask 标注。
 *
 * 母版窗口：选中这个已保存 Mask → 进入「编辑 Mask」→ 高级菜单选「直线切割为两个实例」
 * → 拖动有向直线沿两车之间切开 → 查看两块彩色预览与「面积 X → Y+Z px」→ 点击
 * 「提交 2 个实例」→ 依次选中两个结果实例。
 *
 * 母版窗口之外仍做真实回读：mask-content 像素数量、分割是否互斥且并集等于来源、
 * 以及一次撤销 / 重做验证同一对已保存实例回来。返回 { drawStartMs, drawEndMs, evidence }：
 * finalize 用它裁掉准备与回读段，测试体把 evidence 写入 flowBehaviorEvidence 并精确清理两个实例。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { closeMaskSettings, openMaskSettings } from "../../fixtures/mask-toolbar";
import {
  hidePredictions,
  mediaPoint,
  movePointerPathAtRefreshRate,
  openImageAnnotate,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

type Point = [number, number];

interface CocoRle {
  encoding: "coco_rle";
  size: [number, number];
  counts: number[];
}

interface MaskReference {
  encoding: "coco_rle_ref";
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
}

interface AnnotationRow {
  id: string;
  version: number;
  class_name: string;
  geometry: { type: string; mask?: MaskReference };
}

interface SliceReceipt {
  slice_operation_id: string;
  source_annotation_id: string;
  created_annotation_id: string;
  result_versions: Record<string, number>;
  restore_expires_at: string;
}

interface CommitResponse {
  operation_id: string;
  slice_restore: SliceReceipt | null;
}

interface TaskSize {
  image_width: number | null;
  image_height: number | null;
}

/**
 * 前车（白色厢式货车）在 screenshot_03 后视投影里的轮廓，归一化媒体坐标。
 * 数值来自对该真实夹具图 (538,181)-(708,361) px 区域的量取：车顶较窄、车身外扩、
 * 底部落在 y≈0.489，与 primary_vehicle 车顶 (y≈0.494) 之间留出切开缝。
 */
const VAN_REAR_POLYGON: Point[] = [
  [0.445, 0.2514],
  [0.527, 0.2514],
  [0.538, 0.278],
  [0.545, 0.352],
  [0.5524, 0.489],
  [0.421, 0.489],
  [0.428, 0.352],
  [0.435, 0.278],
];

/** 有向切割线：沿两车结合部水平切开，x 覆盖 0.34→0.64（两侧都超出车身宽度）。 */
const CUT_PATH: [Point, Point] = [
  [0.34, 0.4915],
  [0.64, 0.4915],
];

const pathOf = (url: string) => new URL(url).pathname;

/** 按列优先（COCO）顺序把归一化多边形栅格化成 RLE；与后端 coco_rle 契约一致。 */
function encodeNormalizedMask(width: number, height: number, polygons: Point[][]): CocoRle {
  const pixels = new Uint8Array(width * height);
  for (const polygon of polygons) {
    if (polygon.length < 3) throw new Error("[mask-slice-instances] 多边形至少需要三个顶点");
    for (let y = 0; y < height; y += 1) {
      const scanY = (y + 0.5) / height;
      const crossings: number[] = [];
      for (let index = 0; index < polygon.length; index += 1) {
        const [x1, y1] = polygon[index]!;
        const [x2, y2] = polygon[(index + 1) % polygon.length]!;
        if ((y1 <= scanY && scanY < y2) || (y2 <= scanY && scanY < y1)) {
          crossings.push(x1 + ((scanY - y1) * (x2 - x1)) / (y2 - y1));
        }
      }
      crossings.sort((left, right) => left - right);
      for (let index = 0; index + 1 < crossings.length; index += 2) {
        const left = Math.max(0, Math.round(crossings[index]! * width));
        const right = Math.min(width, Math.round(crossings[index + 1]! * width));
        for (let x = left; x < right; x += 1) pixels[x * height + y] = 1;
      }
    }
  }
  const counts = [0];
  let last = 0;
  for (let index = 0; index < pixels.length; index += 1) {
    const next = pixels[index]!;
    if (next !== last) {
      counts.push(0);
      last = next;
    }
    counts[counts.length - 1] += 1;
  }
  return { encoding: "coco_rle", size: [height, width], counts };
}

function decodeMask(rle: CocoRle): Uint8Array {
  const [height, width] = rle.size;
  const output = new Uint8Array(width * height);
  let cursor = 0;
  rle.counts.forEach((count, index) => {
    if (index % 2 === 1) output.fill(1, cursor, cursor + count);
    cursor += count;
  });
  return output;
}

const maskArea = (rle: CocoRle) =>
  rle.counts.reduce((total, count, index) => (index % 2 === 1 ? total + count : total), 0);

async function apiFetch<T>(
  page: Page,
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  return page.evaluate(
    async ({ path, method, body }) => {
      const response = await fetch(path, {
        method,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("token") ?? ""}`,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      if (!response.ok) {
        throw new Error(`[mask-slice-instances] ${path}: HTTP ${response.status} ${text}`);
      }
      return text ? JSON.parse(text) : null;
    },
    { path, method: options.method ?? "GET", body: options.body },
  ) as Promise<T>;
}

/** 上传内容并落库为已保存的 raster_mask 标注；两个资源都由调用方在 finally 清理。 */
async function seedSavedMask(
  page: Page,
  taskId: string,
  rle: CocoRle,
  label: string,
): Promise<{ reference: MaskReference; annotation: AnnotationRow }> {
  const reference = await apiFetch<MaskReference>(page, `/api/v1/tasks/${taskId}/mask-content`, {
    method: "POST",
    body: rle,
  });
  const annotation = await apiFetch<AnnotationRow>(page, `/api/v1/tasks/${taskId}/annotations`, {
    method: "POST",
    body: {
      annotation_type: "raster_mask",
      tool_unit_id: "region",
      class_name: label,
      geometry: { type: "raster_mask", mask: reference },
    },
  });
  return { reference, annotation };
}

async function readTaskAnnotations(page: Page, taskId: string): Promise<AnnotationRow[]> {
  return apiFetch<AnnotationRow[]>(page, `/api/v1/tasks/${taskId}/annotations`);
}

async function readMaskContent(page: Page, annotationId: string): Promise<CocoRle> {
  return apiFetch<CocoRle>(page, `/api/v1/annotations/${annotationId}/mask-content`);
}

/** 撤销 / 重做一次：等待真实的 :restore 响应，返回服务端结果。 */
async function restore(page: Page, taskId: string, direction: "undo" | "redo") {
  const pending = page.waitForResponse(
    (response) =>
      pathOf(response.url()).startsWith(`/api/v1/tasks/${taskId}/annotations/slices/`) &&
      pathOf(response.url()).endsWith(":restore"),
  );
  await page.keyboard.press(direction === "undo" ? "Control+z" : "Control+Shift+z");
  const response = await pending;
  expect(response.ok(), await response.text()).toBe(true);
  return response.json() as Promise<Record<string, unknown>>;
}

export async function runMaskSliceInstances(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow & { evidence: Record<string, unknown> }> {
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  const stage = page.getByTestId("workbench-stage");
  const controls = page.getByTestId("mask-toolbar");
  const vehicle = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");
  if (vehicle.polygon.length < 3)
    throw new Error("[mask-slice-instances] 缺少 primary_vehicle 多边形");

  await openImageAnnotate(page, catalog);
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await hidePredictions(page);
  await expect(stage).toHaveAttribute("data-ai-box-count", "0");

  // ── 准备（不进母版）：真实媒体尺寸 + 真实 mask-content 上传 ──
  const media = await apiFetch<TaskSize>(page, `/api/v1/tasks/${task.id}`);
  if (!media.image_width || !media.image_height) {
    throw new Error("[mask-slice-instances] image_demo.annotating 缺少真实媒体尺寸");
  }
  const sourceRle = encodeNormalizedMask(media.image_width, media.image_height, [
    VAN_REAR_POLYGON,
    vehicle.polygon,
  ]);
  const sourceArea = maskArea(sourceRle);
  const sourcePixels = decodeMask(sourceRle);
  const seeded = await seedSavedMask(page, task.id, sourceRle, vehicle.label);
  const sourceId = seeded.annotation.id;
  expect(seeded.annotation.geometry).toMatchObject({
    type: "raster_mask",
    mask: { sha256: seeded.reference.sha256, size: sourceRle.size },
  });

  await page.reload();
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await page.getByTestId("tool-btn-select").click();
  const sourceRow = page.getByTestId(`box-list-item-${sourceId}`);
  await sourceRow.click();
  await expect(sourceRow).toHaveClass(/border-brand/);
  // 行内统计来自真实内容解码；这一步证明夹具 Mask 覆盖了两辆相邻车辆的区域。
  await expect(sourceRow).toContainText(/\d+ px · \d+ 组件 · \d+ 孔洞 · AABB/, { timeout: 20_000 });
  await expect(sourceRow).not.toContainText("load_failed");

  // ── 母版窗口 ──
  const drawStartMs = Date.now();
  await page.waitForTimeout(1650);

  await page.locator('button[aria-label="编辑 Mask"]:visible').last().click();
  await openMaskSettings(page);
  await expect(controls).toContainText("就绪", { timeout: 20_000 });
  await page.waitForTimeout(1850);

  await controls.getByTitle("Mask 高级工具").click();
  await page.getByRole("menuitem", { name: "直线切割为两个实例", exact: true }).click();
  await expect(page.getByRole("menu")).toBeHidden();
  await expect(controls).toContainText("拖动两点定义直线");
  await page.waitForTimeout(1150);

  // 收起设置面板再拖线，避免面板遮挡画布；松手后重开预览两块结果与像素数量。
  await closeMaskSettings(page);
  const bounds = await renderedMediaBounds(stage);
  const from = mediaPoint(bounds, CUT_PATH[0]);
  const to = mediaPoint(bounds, CUT_PATH[1]);
  await page.mouse.move(from.x, from.y);
  await page.waitForTimeout(450);
  await page.mouse.down();
  await movePointerPathAtRefreshRate(page, [from, to], 800);
  await page.mouse.up();
  await openMaskSettings(page);

  await expect(page.getByTestId("mask-primary-action")).toHaveText("提交 2 个实例");
  await expect(controls).toContainText("直线切割");
  await expect(controls).toContainText(/面积 \d+ → \d+\+\d+ px/);
  await page.waitForTimeout(2850); // 停留展示两块彩色预览与像素数量

  const commitPath = `/api/v1/tasks/${task.id}/annotations/mask-mutations:commit`;
  const commitRequest = page.waitForRequest((request) => pathOf(request.url()) === commitPath);
  const commitResponse = page.waitForResponse((response) => pathOf(response.url()) === commitPath);
  await page.getByTestId("mask-primary-action").click();
  const [request, response] = await Promise.all([commitRequest, commitResponse]);
  expect(response.ok(), await response.text()).toBe(true);
  const receipt = (await response.json()) as CommitResponse;
  if (!receipt.slice_restore) throw new Error("[mask-slice-instances] 提交未返回切割回执");
  const commitBody = request.postDataJSON() as {
    cut_path: [Point, Point];
    report: { source_areas: number[]; result_areas: number[] };
  };
  const keptId = receipt.slice_restore.source_annotation_id;
  const createdId = receipt.slice_restore.created_annotation_id;
  await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0);

  const keptRow = page.getByTestId(`box-list-item-${keptId}`);
  const createdRow = page.getByTestId(`box-list-item-${createdId}`);
  await expect(keptRow).toHaveCount(1, { timeout: 15_000 });
  await expect(createdRow).toHaveCount(1);
  await page.waitForTimeout(1850);

  // 依次选中两个结果实例，展示真实落库的两个对象。
  // 提交切割后应用会自动选中保留实例并展开浮动信息卡（z-popover-elevated），
  // 它会盖住所有列表点击；与 named-preset 一致，每次点击前先收起浮窗。
  const selectionCardCollapse = page.getByRole("button", { name: "收起浮窗", exact: true });
  if (await selectionCardCollapse.isVisible()) await selectionCardCollapse.click();
  await keptRow.click();
  await expect(keptRow).toHaveClass(/border-brand/);
  await page.waitForTimeout(1950);
  if (await selectionCardCollapse.isVisible()) await selectionCardCollapse.click();
  await createdRow.click();
  await expect(createdRow).toHaveClass(/border-brand/);
  await page.waitForTimeout(1950);

  const drawEndMs = Date.now();

  // ── 母版窗口之外：真实回读、分割校验与撤销 / 重做 ──
  const saved = await readTaskAnnotations(page, task.id);
  const [keptRle, createdRle] = await Promise.all([
    readMaskContent(page, keptId),
    readMaskContent(page, createdId),
  ]);
  const keptPixels = decodeMask(keptRle);
  const createdPixels = decodeMask(createdRle);
  expect(keptPixels.length).toBe(sourcePixels.length);
  expect(createdPixels.length).toBe(sourcePixels.length);
  const disjoint = !keptPixels.some((pixel, index) => pixel === 1 && createdPixels[index] === 1);
  const complete = keptPixels.every(
    (pixel, index) => (pixel === 1 || createdPixels[index] === 1) === (sourcePixels[index] === 1),
  );
  expect(disjoint, "两个结果实例不得重叠像素").toBe(true);
  expect(complete, "两个结果实例的并集必须等于来源 Mask").toBe(true);
  expect(keptRle.size).toEqual(sourceRle.size);
  expect(createdRle.size).toEqual(sourceRle.size);
  expect(commitBody.report.source_areas).toEqual([sourceArea]);
  expect(commitBody.report.result_areas).toEqual([maskArea(keptRle), maskArea(createdRle)]);
  // 切割线是真实指针拖拽产生的：提交的 cut_path 是归一化坐标四舍五入的浮点，
  // 不与常量逐位相等，只验证两个端点都落在预期直线上且顺序一致。
  expect(commitBody.cut_path).toHaveLength(2);
  commitBody.cut_path.forEach((point, index) => {
    expect(Math.abs(point[0] - CUT_PATH[index][0])).toBeLessThan(0.005);
    expect(Math.abs(point[1] - CUT_PATH[index][1])).toBeLessThan(0.005);
  });
  expect(saved.map((row) => row.id).sort()).toEqual([keptId, createdId].sort());
  expect(saved.every((row) => row.class_name === vehicle.label)).toBe(true);

  // 撤销 / 重做：同一对已保存实例必须回来，像素数量不变。
  await restore(page, task.id, "undo");
  const afterUndo = await readTaskAnnotations(page, task.id);
  await restore(page, task.id, "redo");
  const afterRedo = await readTaskAnnotations(page, task.id);
  expect(afterUndo.map((row) => row.id)).toEqual([keptId]);
  expect(afterRedo.map((row) => row.id).sort()).toEqual([keptId, createdId].sort());
  const afterRedoRles = await Promise.all([
    readMaskContent(page, keptId),
    readMaskContent(page, createdId),
  ]);
  expect(afterRedoRles.map(maskArea)).toEqual([maskArea(keptRle), maskArea(createdRle)]);

  const evidence = {
    media: { width: media.image_width, height: media.image_height },
    mask_fixture: {
      source_annotation_id: sourceId,
      label: vehicle.label,
      sha256: seeded.reference.sha256,
      runs: seeded.reference.runs,
      bytes: seeded.reference.bytes,
      area_px: sourceArea,
      geometry_source: "catalog primary_vehicle 多边形 + 量取的相邻厢式货车尾部轮廓",
    },
    cut_path: CUT_PATH,
    preview_report: commitBody.report,
    commit: receipt,
    readback: {
      saved_annotations: saved.map((row) => ({
        id: row.id,
        class_name: row.class_name,
        version: row.version,
        area_px: row.id === keptId ? maskArea(keptRle) : maskArea(createdRle),
      })),
      partition:
        disjoint && complete ? "disjoint, complete, union equals the source mask" : "invalid",
      kept_area_px: maskArea(keptRle),
      created_area_px: maskArea(createdRle),
    },
    undo_redo: {
      after_undo_active_ids: afterUndo.map((row) => row.id),
      after_redo_active_ids: afterRedo.map((row) => row.id),
      after_redo_areas_px: afterRedoRles.map(maskArea),
    },
    // 测试体清理用：来源 Mask（切割后成为保留实例）与新建实例。
    cleanup_annotation_ids: [keptId, createdId],
  };
  return { drawStartMs, drawEndMs, evidence };
}
