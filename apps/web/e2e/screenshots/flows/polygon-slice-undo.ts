/**
 * 流程录制：把已保存的简单多边形沿一条穿越切线切割成两个对象，再撤销 / 重做一次。
 *
 * 输出：营销母版 polygon/slice-undo（见 apps/web/scripts/derive-doc-media.mjs）
 *
 * screenshot catalog 的 image_demo.annotating 任务先用真实多边形工具画出并保存来源多边形（录制窗口外），
 * 之后切到选择工具选中它 → 右键对象打开上下文菜单 →「切割多边形」→ 从对象外逐点画一条穿越切线 →
 *「预览切割」显示两块预览 →「确认切割」真实落库。随后 Ctrl+Z 撤销、Ctrl+Shift+Z 重做，
 * 每次都等真实 restore 响应并读回标注列表，证明同两个已保存对象回来、来源几何回到原样。
 *
 * 返回 { drawStartMs, drawEndMs } 供 finalize 裁剪；evidence 记录真实 API 读回结果。
 */
import { expect, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  commitPendingAnnotationClass,
  mediaPoint,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import { prepareImageDrawing } from "./_image-drawing";
import type { DrawWindow } from "./rotated-bbox";

/** 画布上真实保存的标注（只取本流程用到的字段）。 */
interface TaskAnnotation {
  id: string;
  version: number;
  class_name: string;
  is_locked: boolean;
  geometry: { type: string; points?: number[][] };
}

/** POST /annotations/polygon-slices:commit 的真实回包。 */
interface SliceResult {
  operation_id: string;
  slice_operation_id: string;
  source_annotation_id: string;
  created_annotation_id: string;
  result_versions: Record<string, number>;
  restore_expires_at: string;
}

export interface PolygonSliceUndoEvidence {
  sourceId: string;
  createdId: string;
  commit: SliceResult;
  /** 预览面板显示的两块几何（与落库几何一致）。 */
  preview: Array<{ points: number[][] }>;
  afterCommit: TaskAnnotation[];
  afterUndo: TaskAnnotation[];
  afterRedo: TaskAnnotation[];
  restoreExpiresAt: string;
}

/** 穿越来源多边形的切线：两端都在对象外，横向斜穿整个车辆轮廓。 */
const CUT_PATH: Array<[number, number]> = [
  [0.34, 0.61],
  [0.62, 0.63],
];

async function readTaskAnnotations(page: Page, taskId: string): Promise<TaskAnnotation[]> {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/tasks/${id}/annotations`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok) throw new Error(`[polygon-slice-undo] 读回标注失败: HTTP ${response.status}`);
    return (await response.json()) as TaskAnnotation[];
  }, taskId);
}

async function json<T>(response: Response): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

/** 用真实多边形工具逐点画出并保存来源多边形（录制窗口之外的准备步骤）。 */
async function drawSavedPolygon(
  page: Page,
  points: Array<[number, number]>,
  taskId: string,
  label: string,
): Promise<TaskAnnotation> {
  const stage = page.getByTestId("workbench-stage");
  await page.getByTestId("tool-btn-polygon").click();
  const bounds = await renderedMediaBounds(stage);
  for (const point of points) {
    const { x, y } = mediaPoint(bounds, point);
    await page.mouse.move(x, y);
    await page.waitForTimeout(220);
    await page.mouse.click(x, y);
    await page.waitForTimeout(420);
  }
  // 悬停回到首点展示闭合预览线，再用 Enter 闭合提交。
  const first = mediaPoint(bounds, points[0]);
  await page.mouse.move(first.x, first.y);
  await page.waitForTimeout(600);
  await page.keyboard.press("Enter");
  const created = await commitPendingAnnotationClass(page, { label, taskId });
  await expect(stage).toHaveAttribute("data-pending-drawing", "false");
  return created as unknown as TaskAnnotation;
}

/** Ctrl+Z / Ctrl+Shift+Z 触发真实的切割恢复接口。 */
async function restore(
  page: Page,
  taskId: string,
  direction: "undo" | "redo",
): Promise<SliceResult> {
  const pending = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.startsWith(`/api/v1/tasks/${taskId}/annotations/slices/`) &&
      new URL(response.url()).pathname.endsWith(":restore"),
    { timeout: 20_000 },
  );
  await page.keyboard.press(direction === "undo" ? "Control+z" : "Control+Shift+z");
  return json<SliceResult>(await pending);
}

export async function runPolygonSliceUndo(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow & { evidence: PolygonSliceUndoEvidence }> {
  const task = catalog.projects.image_demo.tasks.annotating;
  const anchor = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");
  const stage = page.getByTestId("workbench-stage");
  const controls = page.getByTestId("polygon-slice-controls");

  await prepareImageDrawing(page, catalog);
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });

  // 准备（不进素材）：用真实界面画出并保存来源多边形，再按故事线选中它。
  const source = await drawSavedPolygon(page, anchor.polygon, task.id, anchor.label);
  expect(source.geometry.type, "来源必须是已保存的简单多边形").toBe("polygon");
  expect(source.is_locked).toBe(false);
  await page.getByTestId("tool-btn-select").click();
  const bounds = await renderedMediaBounds(stage);
  const inside = mediaPoint(bounds, anchor.point);
  const selectedItem = page.getByTestId(`box-list-item-${source.id}`);
  await expect(selectedItem).toBeVisible({ timeout: 10_000 });
  await page.mouse.click(inside.x, inside.y);
  await expect(selectedItem).toHaveClass(/border-brand/);
  await expect(stage).toHaveAttribute("data-user-box-count", "1");

  const drawStartMs = Date.now();

  // ── 已保存对象 → 右键上下文菜单 → 切割多边形 ──
  await page.waitForTimeout(1766);
  await page.mouse.click(inside.x, inside.y, { button: "right" });
  await page.getByRole("menuitem", { name: "切割多边形", exact: true }).click();
  await expect(controls).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(1966);

  // ── 从对象外逐点画穿越切线，点间停顿展示切线预览 ──
  for (const point of CUT_PATH) {
    const { x, y } = mediaPoint(bounds, point);
    await page.mouse.move(x, y);
    await page.waitForTimeout(986);
    await page.mouse.click(x, y);
    await page.waitForTimeout(1186);
  }
  await page.waitForTimeout(1366);

  // ── 两块预览：先看真实的两区域结果，再确认 ──
  await controls.getByRole("button", { name: "预览切割", exact: true }).click();
  await expect(controls).toContainText("预览 2 块");
  const preview = JSON.parse((await controls.getAttribute("data-slice-preview")) ?? "[]") as Array<{
    points: number[][];
  }>;
  await page.waitForTimeout(2466);

  const commitPending = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith("/annotations/polygon-slices:commit"),
    { timeout: 20_000 },
  );
  await controls.getByRole("button", { name: "确认切割", exact: true }).click();
  const commit = await json<SliceResult>(await commitPending);
  await expect(controls).toBeHidden({ timeout: 10_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "2");
  const createdId = commit.created_annotation_id;
  await expect(page.getByTestId(`box-list-item-${createdId}`)).toBeVisible({ timeout: 10_000 });
  const afterCommit = await readTaskAnnotations(page, task.id);
  await page.waitForTimeout(2166);

  // ── 撤销：两个对象回到来源一个，来源几何恢复原样 ──
  const undone = await restore(page, task.id, "undo");
  await expect(stage).toHaveAttribute("data-user-box-count", "1", { timeout: 10_000 });
  await expect(page.getByTestId(`box-list-item-${createdId}`)).toHaveCount(0);
  const afterUndo = await readTaskAnnotations(page, task.id);
  await page.waitForTimeout(2366);

  // ── 重做：同两个已保存对象回来 ──
  const redone = await restore(page, task.id, "redo");
  await expect(stage).toHaveAttribute("data-user-box-count", "2", { timeout: 10_000 });
  await expect(page.getByTestId(`box-list-item-${createdId}`)).toBeVisible({ timeout: 10_000 });
  const afterRedo = await readTaskAnnotations(page, task.id);
  await page.waitForTimeout(2166);

  // ── 真实结果断言：来源保留原 ID、几何与属性，新对象是同一次切割的产物 ──
  expect(afterCommit.map((item) => item.id).sort()).toEqual([source.id, createdId].sort());
  expect(afterUndo).toHaveLength(1);
  const restored = afterUndo.find((item) => item.id === source.id)!;
  expect(restored.geometry).toEqual(source.geometry);
  expect(afterRedo.map((item) => item.id).sort()).toEqual([source.id, createdId].sort());
  const redoneIds = new Set(afterRedo.map((item) => item.id));
  expect(redoneIds.size).toBe(2);
  expect(undone.restore_expires_at).toBe(commit.restore_expires_at);
  expect(redone.restore_expires_at).toBe(commit.restore_expires_at);
  expect(preview).toHaveLength(2);

  const drawEndMs = Date.now();
  return {
    drawStartMs,
    drawEndMs,
    evidence: {
      sourceId: source.id,
      createdId,
      commit,
      preview,
      afterCommit,
      afterUndo,
      afterRedo,
      restoreExpiresAt: commit.restore_expires_at,
    },
  };
}
