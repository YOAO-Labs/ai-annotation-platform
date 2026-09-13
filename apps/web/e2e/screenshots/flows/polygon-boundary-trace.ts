/**
 * 流程录制：沿已保存多边形的边界追踪出相邻多边形的共享边界。
 *
 * 输出：营销母版 polygon/boundary-trace（见 apps/web/scripts/derive-doc-media.mjs）
 *
 * screenshot catalog 的 image_demo.annotating 任务先用真实多边形工具画出并保存来源多边形（车辆轮廓，
 * 录制窗口之外）。随后在来源旁边开始画相邻多边形：落两个自由点 →「沿已有边界」→ 在来源边界上
 * 依次点击起点与终点 → 对比顺/逆时针两条候选路径（点数与「较短」标记）→ 确认较短那条 →
 * 草稿沿共享边界补全 → Enter 闭合 → 选 car 保存。最后读回两个对象，证明相邻多边形与来源共享
 * 边界、来源版本与几何未变。
 *
 * 返回 { drawStartMs, drawEndMs } 供 finalize 裁剪；evidence 记录真实 API 读回结果。
 */
import { expect, type Page } from "@playwright/test";
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
  geometry: { type: string; points?: number[][] };
}

export interface PolygonBoundaryTraceEvidence {
  sourceId: string;
  sourceVersion: number;
  /** 追踪后来源标注的版本与几何，必须与追踪前完全一致。 */
  sourceAfter: { version: number; geometry: TaskAnnotation["geometry"] };
  sourceUnchanged: boolean;
  savedId: string;
  /** 新多边形的顶点：两个自由点 + 沿来源边界追加的弧。 */
  savedPoints: number[][];
  /** 与来源共享的边界点（应等于来源轮廓上的连续顶点）。 */
  sharedBoundary: number[][];
  directionOptions: string[];
  confirmedDirection: string;
  draftCountAfterConfirm: number;
}

/** 相邻多边形的两个自由点：位于来源右侧，避开来源轮廓。 */
const NEIGHBOR_FREE_POINTS: Array<[number, number]> = [
  [0.665, 0.46],
  [0.665, 0.8],
];

async function readTaskAnnotations(page: Page, taskId: string): Promise<TaskAnnotation[]> {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/tasks/${id}/annotations`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok)
      throw new Error(`[polygon-boundary-trace] 读回标注失败: HTTP ${response.status}`);
    return (await response.json()) as TaskAnnotation[];
  }, taskId);
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

/** 归一化坐标比较（点击落点有 1 像素级取整，共享边界则来自来源几何本身）。 */
function pointCloseTo(actual: number[], expected: number[], tolerance: number): boolean {
  return (
    actual.length === expected.length &&
    actual.every((value, index) => Math.abs(value - expected[index]) <= tolerance)
  );
}

export async function runPolygonBoundaryTrace(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow & { evidence: PolygonBoundaryTraceEvidence }> {
  const task = catalog.projects.image_demo.tasks.annotating;
  const anchor = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");
  const stage = page.getByTestId("workbench-stage");
  const controls = page.getByTestId("polygon-boundary-trace");
  if (anchor.polygon.length < 5) {
    throw new Error("[polygon-boundary-trace] primary_vehicle 缺少可追踪的轮廓锚点");
  }

  await prepareImageDrawing(page, catalog);
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });

  // 准备（不进素材）：画出并保存来源多边形（真实车辆轮廓），随后保持多边形工具。
  const source = await drawSavedPolygon(page, anchor.polygon, task.id, anchor.label);
  expect(source.geometry.type, "来源必须是已保存的简单多边形").toBe("polygon");
  const bounds = await renderedMediaBounds(stage);
  await expect(page.getByTestId("tool-btn-polygon")).toHaveAttribute("aria-pressed", "true");

  const drawStartMs = Date.now();

  // ── 相邻区域先落两个自由点，再进入「沿已有边界」──
  await page.waitForTimeout(1_100);
  for (const point of NEIGHBOR_FREE_POINTS) {
    const { x, y } = mediaPoint(bounds, point);
    await page.mouse.move(x, y);
    await page.waitForTimeout(280);
    await page.mouse.click(x, y);
    await page.waitForTimeout(460);
  }
  await expect(stage).toHaveAttribute("data-polygon-draft-count", "2");
  await page.waitForTimeout(700);

  await controls.getByRole("button", { name: "沿已有边界", exact: true }).click();
  await expect(controls).toContainText("在已保存的多边形边界上点击起点");

  // ── 在来源边界上点起点与终点（取来源轮廓的两个真实顶点）──
  const traceStart = mediaPoint(bounds, anchor.polygon[4]);
  await page.mouse.move(traceStart.x, traceStart.y);
  await page.waitForTimeout(420);
  await page.mouse.click(traceStart.x, traceStart.y);
  await expect(controls).toContainText("在同一条边界上点击终点");
  await page.waitForTimeout(900);

  const traceEnd = mediaPoint(bounds, anchor.polygon[2]);
  await page.mouse.move(traceEnd.x, traceEnd.y);
  await page.waitForTimeout(420);
  await page.mouse.click(traceEnd.x, traceEnd.y);
  await expect(controls.getByRole("button", { name: "确认追加", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await page.waitForTimeout(900);

  // ── 对比两个方向：先看非默认方向，再回到「较短」的那条并确认 ──
  const clockwise = controls.getByRole("button", { name: /^顺时针/ });
  const counterclockwise = controls.getByRole("button", { name: /^逆时针/ });
  const directionOptions = [
    (await clockwise.textContent()) ?? "",
    (await counterclockwise.textContent()) ?? "",
  ];
  const shorter = directionOptions[0]!.includes("较短") ? clockwise : counterclockwise;
  const other = shorter === clockwise ? counterclockwise : clockwise;
  await other.click();
  await expect(other).toHaveAttribute("aria-pressed", "true");
  await page.waitForTimeout(1_500);
  await shorter.click();
  await expect(shorter).toHaveAttribute("aria-pressed", "true");
  await page.waitForTimeout(1_200);
  const confirmedDirection = (await shorter.textContent()) ?? "";
  await controls.getByRole("button", { name: "确认追加", exact: true }).click();
  await expect(stage).toHaveAttribute("data-polygon-draft-count", "5", { timeout: 10_000 });
  await page.waitForTimeout(1_400);

  // ── Enter 闭合相邻多边形并选择 car 保存 ──
  await page.keyboard.press("Enter");
  const saved = await commitPendingAnnotationClass(page, { label: anchor.label, taskId: task.id });
  await expect(stage).toHaveAttribute("data-pending-drawing", "false");
  await page.waitForTimeout(1_400);

  // ── 读回两个对象：相邻多边形存在，来源版本与几何未变 ──
  const after = await readTaskAnnotations(page, task.id);
  const sourceAfter = after.find((item) => item.id === source.id);
  if (!sourceAfter) throw new Error("[polygon-boundary-trace] 读回时来源多边形已丢失");
  const savedPoints = ((saved.geometry as { points?: number[][] }).points ?? []).map((point) =>
    point.map(Number),
  );
  const sourcePoints = (source.geometry.points ?? []).map((point) => point.map(Number));
  const sharedBoundary = savedPoints.slice(NEIGHBOR_FREE_POINTS.length);
  const sourceUnchanged =
    sourceAfter.version === source.version &&
    JSON.stringify(sourceAfter.geometry) === JSON.stringify(source.geometry);
  expect(sourceUnchanged, "追踪不得修改来源对象").toBe(true);
  expect(after.map((item) => item.id).sort()).toEqual([source.id, String(saved.id)].sort());
  expect(savedPoints).toHaveLength(NEIGHBOR_FREE_POINTS.length + 3);
  NEIGHBOR_FREE_POINTS.forEach((point, index) => {
    expect(
      pointCloseTo(savedPoints[index]!, point, 0.01),
      `第 ${index} 个自由点应落在点击位置附近`,
    ).toBe(true);
  });
  for (const point of sharedBoundary) {
    expect(
      sourcePoints.some((candidate) => pointCloseTo(point, candidate, 1e-6)),
      "共享边界必须来自来源轮廓上的真实顶点",
    ).toBe(true);
  }
  await page.waitForTimeout(1_200);

  const drawEndMs = Date.now();
  return {
    drawStartMs,
    drawEndMs,
    evidence: {
      sourceId: source.id,
      sourceVersion: source.version,
      sourceAfter: { version: sourceAfter.version, geometry: sourceAfter.geometry },
      sourceUnchanged,
      savedId: String(saved.id),
      savedPoints,
      sharedBoundary,
      directionOptions,
      confirmedDirection,
      draftCountAfterConfirm: 5,
    },
  };
}
