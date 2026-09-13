/**
 * 流程录制：在题图上圈点说明，随评论发送，不修改任何已保存标注。
 *
 * 输出：.artifacts/marketing/<run>/masters/task-drawing-comment → docs-site/public/media/discussion/task-drawing.mp4
 *
 * 准备（不进母版）：把右侧栏拖宽，让讨论面板与评论行在母版里可读。
 *
 * 母版窗口：不选中任何标注（发送目标停在「当前任务」）→ 点「在题图上绘制」→
 * 在题图上圈出一辆尚未保存为标注的车辆 → 浮动工具条的「完成」把这段笔触挂到输入框
 * （显示「批注 · 1 条」）→ 不写任何正文直接发送 → 在「任务留言」来源标签下读回这条
 * 只有绘图的评论 → 点评论行把这条绘图固定叠加回画布。
 *
 * 母版窗口之外仍做真实回读：读取任务标注列表与每个标注的版本号，证明这次圈点没有改动
 * 任何已保存标注；读取服务端 canvas_drawing 的 shapes 坐标并与画布上实际画出的点比对；
 * 最后按 id 精确删除本次发送的评论。返回 { drawStartMs, drawEndMs, evidence }。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { discussionTargetKey } from "../../../src/pages/Workbench/state/discussionTypes";
import {
  hidePredictions,
  mediaPoint,
  movePointerPathAtRefreshRate,
  openImageAnnotate,
  recordingAnchor,
  renderedMediaBounds,
  type MediaBounds,
} from "./_canvas";
import { waitForRecordingPanels } from "./_workbench-layout";
import { widenDiscussionColumn } from "./discussion-target-drafts";
import type { DrawWindow } from "./rotated-bbox";

type Point = [number, number];

const COMMENT_ENDPOINT = "/api/v1/feedbacks";

interface SavedAnnotation {
  id: string;
  version: number;
  class_name: string;
  geometry: { type: string; x?: number; y?: number; w?: number; h?: number };
}

interface StoredShape {
  points: number[];
  stroke?: string;
}

interface DiscussionItem {
  data: {
    body: string;
    task_id: string;
    annotation_id?: string | null;
    canvas_drawing?: { shapes?: StoredShape[] } | null;
  };
  source: string;
}

interface DiscussionPage {
  items: DiscussionItem[];
}

const pathOf = (url: string) => new URL(url).pathname;

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
        throw new Error(`[task-drawing-comment] ${path}: HTTP ${response.status} ${text}`);
      }
      return text ? JSON.parse(text) : null;
    },
    { path, method: options.method ?? "GET", body: options.body },
  ) as Promise<T>;
}

/** 已保存标注的包围盒中心；用于确认圈点区域里没有已保存对象。 */
function annotationCenter(row: SavedAnnotation): Point | null {
  const { geometry } = row;
  if (
    typeof geometry.x !== "number" ||
    typeof geometry.y !== "number" ||
    typeof geometry.w !== "number" ||
    typeof geometry.h !== "number"
  ) {
    return null;
  }
  return [geometry.x + geometry.w / 2, geometry.y + geometry.h / 2];
}

/** 掩膜 / 多边形类标注几何不含 x/y/w/h，这里按 id 只做数量与版本核对。 */
const isCenterInside = (center: Point, region: { center: Point; rx: number; ry: number }) =>
  ((center[0] - region.center[0]) / region.rx) ** 2 +
    ((center[1] - region.center[1]) / region.ry) ** 2 <=
  1;

/** 在归一化媒体坐标里生成一圈闭合的椭圆路径，模拟「圈出这个对象」。 */
function ellipsePath(center: Point, rx: number, ry: number, steps = 18): Point[] {
  const points: Point[] = [];
  for (let step = 0; step <= steps; step += 1) {
    const angle = (step / steps) * Math.PI * 2;
    points.push([center[0] + rx * Math.cos(angle), center[1] + ry * Math.sin(angle)]);
  }
  return points;
}

const flatten = (path: Point[]) => path.flatMap((point) => point);

export async function runTaskDrawingComment(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow & { evidence: Record<string, unknown> }> {
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  const stage = page.getByTestId("workbench-stage");
  const reader = () => page.locator('[data-workbench-panel="discussion"]');
  const editor = (): Locator => reader().getByRole("textbox", { name: "留言" });
  const destination = () => reader().getByRole("combobox", { name: "发送目标" });
  const taskTargetKey = discussionTargetKey({
    projectId: project.id,
    taskId: task.id,
    kind: "task",
  });
  const vehicle = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");

  await openImageAnnotate(page, catalog);
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await waitForRecordingPanels(page, [
    "canvas",
    "task-queue",
    "class-palette",
    "inspector",
    "discussion",
  ]);
  await hidePredictions(page);
  await page.getByTestId("tool-btn-select").click();
  await widenDiscussionColumn(page);
  await reader().getByRole("tab", { name: /^评论/ }).click();
  await expect(editor()).toBeVisible();

  // 不选中任何标注：发送目标停在当前任务，输入框是任务自己的草稿。
  await expect(destination()).toHaveValue(taskTargetKey);
  const selectedRows = page.locator('[data-testid^="box-list-item-"][class*="border-brand"]');
  await expect(selectedRows).toHaveCount(0);

  // 母版窗口之外先取回基准：这次圈点不得改动任何已保存标注。
  const before = await apiFetch<SavedAnnotation[]>(page, `/api/v1/tasks/${task.id}/annotations`);
  const beforeSignature = before
    .map((row) => `${row.id}@${row.version}:${row.class_name}`)
    .sort()
    .join("|");

  // 圈点目标必须真的「尚未保存为标注」：候选区域取锚点车辆与相邻厢式货车，
  // 选第一个没有被任何已保存标注中心落在其中的区域；两个都被占则直接失败。
  const vehicleXs = vehicle.polygon.map((point) => point[0]);
  const vehicleYs = vehicle.polygon.map((point) => point[1]);
  const candidates = [
    {
      name: "相邻厢式货车尾部",
      center: [0.487, 0.37] as Point,
      rx: 0.055,
      ry: 0.14,
    },
    {
      name: "锚点车辆车身",
      center: [
        (Math.min(...vehicleXs) + Math.max(...vehicleXs)) / 2,
        (Math.min(...vehicleYs) + Math.max(...vehicleYs)) / 2,
      ] as Point,
      rx: (Math.max(...vehicleXs) - Math.min(...vehicleXs)) / 2 + 0.03,
      ry: (Math.max(...vehicleYs) - Math.min(...vehicleYs)) / 2 + 0.03,
    },
  ];
  const centers = before.map(annotationCenter).filter((center): center is Point => center !== null);
  const target = candidates.find(
    (candidate) => !centers.some((center) => isCenterInside(center, candidate)),
  );
  if (!target) {
    throw new Error("[task-drawing-comment] 所有候选区域都已有已保存标注，无法演示漏标对象");
  }
  const circlePath = ellipsePath(target.center, target.rx, target.ry);

  // ── 母版窗口 ──
  const drawStartMs = Date.now();
  await page.waitForTimeout(1300);

  await reader().getByRole("button", { name: "在题图上绘制" }).click();
  await expect(reader().getByRole("button", { name: "正在绘制…" })).toBeVisible();
  const doneButton = page.getByRole("button", { name: "完成", exact: true });
  await expect(doneButton).toBeVisible({ timeout: 10_000 });
  // 浮动工具条（颜色 swatch + 撤销 / 清空 / 取消 / 完成）挂在「完成」按钮的父节点上。
  const toolbar = doneButton.locator("..");
  await expect(toolbar).toContainText("0 条");
  await toolbar.getByRole("button", { name: "黄", exact: true }).click();
  await page.waitForTimeout(900);

  // 在题图上圈出这个漏标对象：真实指针按住拖动，闭合一圈。
  const bounds: MediaBounds = await renderedMediaBounds(stage);
  const start = mediaPoint(bounds, circlePath[0]!);
  const path = circlePath.map((point) => mediaPoint(bounds, point));
  await page.mouse.move(start.x, start.y);
  await page.waitForTimeout(200);
  await page.mouse.down();
  await movePointerPathAtRefreshRate(page, path, 2200);
  await page.mouse.up();
  await expect(toolbar).toContainText("1 条", { timeout: 10_000 });
  await page.waitForTimeout(1200);

  // 「完成」把这段笔触挂到输入框；工具条消失，输入框显示自己的批注条数。
  await doneButton.click();
  await expect(doneButton).toHaveCount(0);
  const drawingBadge = reader().getByRole("button", { name: /^批注 · 1 条$/ });
  await expect(drawingBadge).toBeVisible({ timeout: 10_000 });
  await expect(editor()).toBeEmpty();
  await page.waitForTimeout(1400);

  // 不写正文直接发送：只有绘图的评论也必须能提交。
  const pending = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && pathOf(response.url()) === COMMENT_ENDPOINT,
  );
  await reader().getByRole("button", { name: "发送", exact: true }).click();
  const response = await pending;
  expect(response.ok(), await response.text()).toBe(true);
  const sent = (await response.json()) as { id: string };
  const drawnPoints = flatten(circlePath);

  // 读回这条已发布绘图：评论行里应出现带 1 条笔触的预览。
  const row = reader()
    .getByTestId("discussion-comment-row")
    .filter({ has: page.locator('svg[viewBox="0 0 1 1"]') })
    .first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  await expect(row.getByTestId("discussion-source-chip")).toHaveText("任务留言");
  const previewStroke = row.locator('svg[viewBox="0 0 1 1"] polyline');
  await expect(previewStroke).toHaveCount(1);
  const previewPoints = (await previewStroke.getAttribute("points")) ?? "";
  const postedPoints = previewPoints
    .split(/[\s,]+/)
    .filter((token) => token.length > 0)
    .map(Number);
  expect(postedPoints.length).toBe(drawnPoints.length);
  drawnPoints.forEach((value, index) => {
    expect(Math.abs(postedPoints[index]! - value)).toBeLessThan(1e-6);
  });
  await page.waitForTimeout(1600);

  // 打开这条已发布绘图：点评论行把笔触固定叠加回画布（悬停态即真实回放）。
  await expect(row).not.toHaveClass(/border-brand/);
  await row.hover();
  await page.waitForTimeout(700);
  await row.click({ position: { x: 12, y: 12 } });
  await expect(row).toHaveClass(/border-brand/, { timeout: 10_000 });
  await page.waitForTimeout(2400);

  const drawEndMs = Date.now();

  // ── 母版窗口之外：真实回读 + 精确清理 ──
  const after = await apiFetch<SavedAnnotation[]>(page, `/api/v1/tasks/${task.id}/annotations`);
  const afterSignature = after
    .map((row) => `${row.id}@${row.version}:${row.class_name}`)
    .sort()
    .join("|");
  expect(afterSignature).toBe(beforeSignature);

  const taskScope = await apiFetch<DiscussionPage>(
    page,
    `/api/v1/tasks/${task.id}/discussion/page?scope=task`,
  );
  // 按本次画出的坐标定位这条评论，避免与夹具里既有的留言混淆。
  const storedItem = taskScope.items.find((item) => {
    const points = item.data.canvas_drawing?.shapes?.[0]?.points;
    return (
      Boolean(points) &&
      points!.length === drawnPoints.length &&
      drawnPoints.every((value, index) => Math.abs(Number(points![index]) - value) < 1e-6)
    );
  });
  if (!storedItem) throw new Error("[task-drawing-comment] 服务端未回读到本次绘图的评论");
  const storedShapes = storedItem.data.canvas_drawing?.shapes ?? [];
  expect(storedShapes).toHaveLength(1);
  const storedPoints = storedShapes[0]!.points;
  expect(storedPoints.length).toBe(drawnPoints.length);
  expect(storedItem.data.body.trim()).toBe("");
  expect(storedItem.source).toBe("feedback");
  await apiFetch(page, `${COMMENT_ENDPOINT}/${sent.id}`, { method: "DELETE" });

  const evidence = {
    target: {
      region: target.name,
      center: target.center,
      rx: target.rx,
      ry: target.ry,
      reason: "圈点区域没有任何已保存标注的中心，用来演示漏标对象",
    },
    drawing: {
      shape_count: 1,
      stroke: "#f59e0b",
      points: flatten(circlePath),
      attached_badge: "批注 · 1 条",
    },
    sent_comment: {
      id: sent.id,
      source: "feedback",
      source_chip: "任务留言",
      body: "",
      task_id: task.id,
      canvas_drawing: { shapes: storedShapes },
    },
    posted_drawing: {
      row_preview_points: postedPoints,
      pinned_on_canvas: true,
      pinned_row_key: (await row.getAttribute("data-comment-key")) ?? null,
    },
    saved_annotations: {
      count: before.length,
      signature: beforeSignature,
      unchanged: afterSignature === beforeSignature,
    },
    comment_cleanup: `deleted ${COMMENT_ENDPOINT}/${sent.id}`,
  };
  return { drawStartMs, drawEndMs, evidence };
}
