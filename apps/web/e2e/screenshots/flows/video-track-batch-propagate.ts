import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 流程录制：两条已有公交车轨迹多选后，以一个真实 SAM3 作业批量延展、跨帧复核并回填原轨迹。
 */
import { expect, type Locator, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate, normalizedBboxIoU, recordingAnchor } from "./_canvas";
import type { NormalizedBbox } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

export interface VideoTrackBatchPropagateCleanupRecord {
  projectId: string;
  taskId: string;
  sourceAnnotationIds: string[];
  videoTrackerJobIds: string[];
}

function assertAcceptedSources(
  payload: unknown,
  sourceIds: string[],
  expectedFrameZero: Array<[number, number, number, number]>,
): void {
  if (!Array.isArray(payload)) {
    throw new Error("[video-track-batch-propagate] 接受后的标注刷新结果不是数组");
  }
  for (const [index, sourceId] of sourceIds.entries()) {
    const source = payload.find(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        (item as Record<string, unknown>).id === sourceId,
    ) as Record<string, unknown> | undefined;
    const geometry = source?.geometry;
    const keyframes =
      typeof geometry === "object" && geometry !== null
        ? (geometry as Record<string, unknown>).keyframes
        : null;
    if (!Array.isArray(keyframes) || keyframes.length < 11) {
      throw new Error(
        `[video-track-batch-propagate] 源轨迹 ${sourceId} 未回填完整 F0–F10：` +
          `${Array.isArray(keyframes) ? keyframes.length : 0} 个关键帧`,
      );
    }
    const frameZero = keyframes.find(
      (keyframe) =>
        typeof keyframe === "object" &&
        keyframe !== null &&
        (keyframe as Record<string, unknown>).frame_index === 0,
    ) as Record<string, unknown> | undefined;
    const bbox = frameZero?.bbox as NormalizedBbox | undefined;
    const expected = expectedFrameZero[index]!;
    const expectedBbox = {
      x: expected[0],
      y: expected[1],
      w: expected[2] - expected[0],
      h: expected[3] - expected[1],
    };
    if (!bbox || normalizedBboxIoU(bbox, expectedBbox) < 0.9) {
      throw new Error(`[video-track-batch-propagate] 源轨迹 ${sourceId} 的人工 F0 整车框未被保留`);
    }
  }
}

async function moveBetweenRows(page: Page, from: Locator, to: Locator): Promise<void> {
  const [fromBox, toBox] = await Promise.all([from.boundingBox(), to.boundingBox()]);
  if (!fromBox || !toBox) {
    throw new Error("[video-track-batch-propagate] 轨迹清单行不可见");
  }
  const start = { x: fromBox.x + fromBox.width * 0.5, y: fromBox.y + fromBox.height * 0.5 };
  const end = { x: toBox.x + toBox.width * 0.5, y: toBox.y + toBox.height * 0.5 };
  await page.mouse.move(start.x, start.y);
  await movePointerAtRefreshRate(page, start, end, 500);
}

async function scrubCandidateFrames(page: Page, timeline: Locator): Promise<void> {
  const box = await timeline.boundingBox();
  if (!box) throw new Error("[video-track-batch-propagate] 时间轴不可见");
  const y = box.y + box.height * 0.52;
  const frameZero = { x: box.x + 2, y };
  const later = { x: box.x + box.width * 0.12, y };
  const review = { x: box.x + box.width * 0.06, y };

  await page.mouse.move(frameZero.x, frameZero.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, frameZero, later, 1_400);
  await page.mouse.up();
  await page.getByText(/^F (?:7|8|9) \/ 71$/).waitFor({ timeout: 5_000 });
  await page.waitForTimeout(1_000);

  await page.mouse.down();
  await movePointerAtRefreshRate(page, later, review, 900);
  await page.mouse.up();
  await page.getByText(/^F (?:3|4|5) \/ 71$/).waitFor({ timeout: 5_000 });
  await page.waitForTimeout(1_200);
}

export async function runVideoTrackBatchPropagate(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  sourceAnnotationIds: string[],
  onJobCreated: (jobId: string) => void,
): Promise<DrawWindow> {
  if (sourceAnnotationIds.length !== 2) {
    throw new Error("[video-track-batch-propagate] 母版必须且只能使用两条源轨迹");
  }
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  const anchors = [
    recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0),
    recordingAnchor(catalog, "video_demo", "tracking", "right_bus_f0", 0),
  ];
  if (anchors.some((anchor) => anchor.label !== "bus")) {
    throw new Error("[video-track-batch-propagate] 双轨迹夹具必须对齐两辆完整公交车");
  }

  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("video-konva-stage");
  const timeline = page.getByTestId("video-timeline-shell");
  await timeline.waitFor({ state: "visible", timeout: 15_000 });
  await stage.waitFor({ state: "visible", timeout: 10_000 });
  await page.addStyleTag({
    content: '[data-testid="video-frame-preview-popover"] { display: none !important; }',
  });
  const rows = page.getByTestId("video-track-row");
  await rows.first().waitFor({ state: "visible", timeout: 10_000 });
  if ((await rows.count()) !== 2) {
    throw new Error(
      `[video-track-batch-propagate] 录制任务应只有 2 条夹具轨迹，实际为 ${await rows.count()}`,
    );
  }
  await page.waitForTimeout(900);

  const drawStartMs = Date.now();
  const firstRow = rows.nth(0);
  const secondRow = rows.nth(1);
  await firstRow.click();
  await page.waitForTimeout(500);
  await moveBetweenRows(page, firstRow, secondRow);
  await page.keyboard.down("Control");
  await secondRow.click();
  await page.keyboard.up("Control");
  await firstRow.getAttribute("aria-selected").then((value) => {
    if (value !== "true") throw new Error("[video-track-batch-propagate] 第一条轨迹未保持多选");
  });
  await secondRow.getAttribute("aria-selected").then((value) => {
    if (value !== "true") throw new Error("[video-track-batch-propagate] 第二条轨迹未进入多选");
  });
  const batchToolbar = page.getByTestId("video-track-batch-toolbar");
  await batchToolbar.waitFor({ state: "visible", timeout: 5_000 });
  const selectionSummaries = page.getByText("已选 2 条轨迹", { exact: true });
  await selectionSummaries.first().waitFor({ state: "visible", timeout: 5_000 });
  if ((await selectionSummaries.count()) < 2) {
    throw new Error("[video-track-batch-propagate] 没有同时显示画布浮卡和右栏批量工具条");
  }
  const batchCard = page.locator("[data-floating-panel]").filter({ hasText: "已选 2 条轨迹" });
  await batchCard.waitFor({ state: "visible", timeout: 5_000 });
  const [batchCardBox, batchToolbarBox] = await Promise.all([
    batchCard.boundingBox(),
    batchToolbar.boundingBox(),
  ]);
  if (!batchCardBox || !batchToolbarBox) {
    throw new Error("[video-track-batch-propagate] 无法验证批量浮卡与右栏工具条的构图");
  }
  const overlapsToolbar =
    batchCardBox.x < batchToolbarBox.x + batchToolbarBox.width &&
    batchCardBox.x + batchCardBox.width > batchToolbarBox.x &&
    batchCardBox.y < batchToolbarBox.y + batchToolbarBox.height &&
    batchCardBox.y + batchCardBox.height > batchToolbarBox.y;
  if (overlapsToolbar) {
    throw new Error("[video-track-batch-propagate] 画布批量浮卡遮住了右栏批量工具条");
  }
  await page.waitForTimeout(1_800);

  await batchToolbar.getByRole("button", { name: "批量延展轨迹" }).click();
  const dialog = page.getByTestId("video-tracker-propagate-dialog");
  await dialog.waitFor({ state: "visible", timeout: 5_000 });
  const impact = await dialog.getByTestId("tracker-impact-summary").textContent();
  if (impact?.trim() !== "批量延展 2 条轨迹 · 「bus」") {
    throw new Error(`[video-track-batch-propagate] 作用范围摘要错误：${impact ?? "missing"}`);
  }

  const modelSelect = dialog.locator("#tracker-model");
  const modelValues = await modelSelect
    .locator("option")
    .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value));
  const model = ["sam3_video_interactive", "sam2_video"].find((value) =>
    modelValues.includes(value),
  );
  if (!model) throw new Error("[video-track-batch-propagate] 没有可用的真实点框追踪模型");
  await modelSelect.selectOption(model);
  await dialog.locator("#tracker-range-preset").selectOption("10");
  await page.waitForTimeout(1_500);

  const serverErrors: string[] = [];
  const collectServerError = (response: Response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  };
  page.on("response", collectServerError);
  try {
    const created = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/tasks/${task.id}/video:track`) &&
        response.status() === 202,
      { timeout: 20_000 },
    );
    await dialog.getByRole("button", { name: "开始批量延展" }).click();
    const createdPayload = (await (await created).json()) as { id?: string };
    if (!createdPayload.id) throw new Error("[video-track-batch-propagate] 追踪作业未返回 ID");
    onJobCreated(createdPayload.id);

    await page.getByTestId("video-tool-btn-select").click();
    await page
      .getByTestId("tracker-review-tool-capsule")
      .waitFor({ state: "visible", timeout: 120_000 });
    await openContextToolbar(page, "tracker-review");
    const reviewBar = page.getByTestId("video-tracker-review-bar");
    await reviewBar.waitFor({ state: "visible", timeout: 120_000 });
    await expect(reviewBar.getByTestId("tracker-review-scope-summary")).toContainText(
      /所选待审 \d+/,
    );
    await reviewBar.getByTestId("tracker-review-instance-1").waitFor({ timeout: 5_000 });
    await reviewBar.getByTestId("tracker-review-instance-2").waitFor({ timeout: 5_000 });
    await page.waitForTimeout(1_500);

    // F0 是人工定义的整车种子框；将接受窗口从 F1 开始，
    // 用真实审阅控件明确保留人工关键帧，而不是绕过保护逻辑。
    const fromFrame = reviewBar.getByTestId("tracker-review-from-frame");
    await fromFrame.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("1", { delay: 120 });
    await expect(reviewBar.getByTestId("tracker-review-scope-summary")).toContainText(
      "所选待审 20",
    );
    await page.waitForTimeout(800);
    await scrubCandidateFrames(page, timeline);
    await openContextToolbar(page, "tracker-review");

    const accepted = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/decisions") &&
        response.ok(),
      { timeout: 20_000 },
    );
    const annotationsRefreshed = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        response.url().endsWith(`/tasks/${task.id}/annotations`) &&
        response.ok(),
      { timeout: 20_000 },
    );
    await reviewBar.getByTestId("tracker-review-accept").click();
    const [, annotationsResponse] = await Promise.all([accepted, annotationsRefreshed]);
    assertAcceptedSources(
      await annotationsResponse.json(),
      sourceAnnotationIds,
      anchors.map((anchor) => anchor.bbox),
    );
    await expect(reviewBar).toContainText("已审 20/22");
    // Partial acceptance retains the selected frame window. Select the remaining seed frames explicitly.
    const remainingInstances = reviewBar.locator('input[data-testid^="tracker-review-instance-"]');
    for (let index = 0; index < (await remainingInstances.count()); index += 1)
      await remainingInstances.nth(index).setChecked(true);
    await reviewBar.getByTestId("tracker-review-from-frame").fill("0");
    await reviewBar.getByTestId("tracker-review-to-frame").fill("0");
    await expect(reviewBar.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 2");
    await page.waitForTimeout(700);
    const rejectedSeedFrames = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/decisions") &&
        response.ok(),
      { timeout: 20_000 },
    );
    await reviewBar.getByTestId("tracker-review-discard").click();
    await rejectedSeedFrames;
    await reviewBar.waitFor({ state: "hidden", timeout: 5_000 });
    await page.waitForTimeout(1_800);
  } finally {
    page.off("response", collectServerError);
  }
  if (serverErrors.length > 0) {
    throw new Error(
      `[video-track-batch-propagate] 完整链路出现服务端错误：${serverErrors.join(", ")}`,
    );
  }
  return { drawStartMs, drawEndMs: Date.now() };
}
