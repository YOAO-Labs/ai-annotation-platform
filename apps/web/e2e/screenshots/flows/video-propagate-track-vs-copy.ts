import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 流程录制：在同一条真实中间卡车轨迹上对比纯几何复制与 SAM3 AI 延展。
 */
import { expect, type Locator, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate, normalizedBboxIoU, recordingAnchor } from "./_canvas";
import type { NormalizedBbox } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

const TARGET_FRAME = 30;

function annotationKeyframes(
  payload: unknown,
  annotationId: string,
): Array<Record<string, unknown>> {
  if (!payload || typeof payload !== "object") {
    throw new Error("[video-propagate-track-vs-copy] 标注更新未返回对象");
  }
  const annotation = payload as Record<string, unknown>;
  if (annotation.id !== annotationId) {
    throw new Error("[video-propagate-track-vs-copy] 标注更新返回了错误的轨迹");
  }
  const geometry = annotation.geometry;
  const keyframes =
    typeof geometry === "object" && geometry !== null
      ? (geometry as Record<string, unknown>).keyframes
      : null;
  if (!Array.isArray(keyframes)) {
    throw new Error("[video-propagate-track-vs-copy] 轨迹缺少关键帧数据");
  }
  return keyframes as Array<Record<string, unknown>>;
}

function assertSourceFramePreserved(
  keyframes: Array<Record<string, unknown>>,
  expected: NormalizedBbox,
): void {
  const frameZero = keyframes.find((keyframe) => keyframe.frame_index === 0);
  const bbox = frameZero?.bbox as NormalizedBbox | undefined;
  if (!bbox || normalizedBboxIoU(bbox, expected) < 0.9) {
    throw new Error("[video-propagate-track-vs-copy] AI 回填后没有保留 F0 人工整车框");
  }
}

async function seekFrame(page: Page, frameIndex: number): Promise<void> {
  const slider = page.getByRole("slider", { name: "视频帧时间轴" }).first();
  await slider.fill(String(Math.round((frameIndex / 71) * 10_000)));
  await expect(page.getByTestId("video-konva-stage")).toHaveAttribute(
    "data-video-frame-index",
    String(frameIndex),
    { timeout: 10_000 },
  );
}

async function scrubToFrame(
  page: Page,
  timeline: Locator,
  fromFrame: number,
  toFrame: number,
  durationMs: number,
): Promise<void> {
  const box = await timeline.boundingBox();
  if (!box) throw new Error("[video-propagate-track-vs-copy] 时间轴不可见");
  const y = box.y + box.height * 0.52;
  const inset = 2;
  const width = Math.max(1, box.width - inset * 2);
  const start = { x: box.x + inset + width * (fromFrame / 71), y };
  const end = { x: box.x + inset + width * (toFrame / 71), y };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, start, end, durationMs);
  await page.mouse.up();
  // 绘制过程使用真实拖动保留连续帧；收尾用原生 range 精确对齐目标帧。
  await seekFrame(page, toFrame);
}

async function waitForTrackUpdate(
  page: Page,
  taskId: string,
  annotationId: string,
): Promise<Response> {
  return page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().endsWith(`/tasks/${taskId}/annotations/${annotationId}`) &&
      response.ok(),
    { timeout: 20_000 },
  );
}

export async function runVideoPropagateTrackVsCopy(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  sourceAnnotationId: string,
  onJobCreated: (jobId: string) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  const source = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
  if (source.label !== "truck") {
    throw new Error("[video-propagate-track-vs-copy] 对比源必须是 F0 中间完整卡车");
  }

  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("video-konva-stage");
  const timeline = page.getByTestId("video-timeline-shell");
  await timeline.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-video-frame-index", "0", { timeout: 15_000 });
  await page.addStyleTag({
    content: '[data-testid="video-frame-preview-popover"] { display: none !important; }',
  });

  const rows = page.getByTestId("video-track-row");
  await rows.first().waitFor({ state: "visible", timeout: 10_000 });
  if ((await rows.count()) !== 1) {
    throw new Error(
      `[video-propagate-track-vs-copy] 录制任务应只有 1 条源轨迹，实际为 ${await rows.count()}`,
    );
  }
  await rows.first().click();
  await page.getByTestId("video-tool-btn-select").click();
  const selectionCapsule = page.getByLabel("展开选中信息卡(可拖动)", { exact: true });
  if (await selectionCapsule.isVisible()) await selectionCapsule.click();
  const copyButton = page.getByRole("button", { name: "复制后续", exact: true });
  const aiButton = page.getByRole("button", { name: "延展此轨迹", exact: true });
  await copyButton.waitFor({ state: "visible", timeout: 5_000 });
  await aiButton.waitFor({ state: "visible", timeout: 5_000 });

  const serverErrors: string[] = [];
  const collectServerError = (response: Response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  };
  page.on("response", collectServerError);
  const drawStartMs = Date.now();
  try {
    // 第一段：纯几何复制。中间卡车在 F30 的位置和尺寸变化足够大，
    // 可以直观展示原框不会跟随目标。
    await page.waitForTimeout(1_200);
    await copyButton.hover();
    await copyButton.click();
    const copyDialog = page.getByTestId("video-keyframes-propagate-dialog");
    await copyDialog.waitFor({ state: "visible", timeout: 5_000 });
    await copyDialog.getByRole("button", { name: "30", exact: true }).click();
    await expect(copyDialog).toContainText("F0 → F30");
    await page.waitForTimeout(900);

    const copied = waitForTrackUpdate(page, task.id, sourceAnnotationId);
    await copyDialog.getByRole("button", { name: "复制", exact: true }).click();
    const copiedKeyframes = annotationKeyframes(await (await copied).json(), sourceAnnotationId);
    if (copiedKeyframes.length !== TARGET_FRAME + 1) {
      throw new Error(
        `[video-propagate-track-vs-copy] 几何复制应生成 31 个关键帧，实际为 ${copiedKeyframes.length}`,
      );
    }
    await page.getByText("已传播到 30 帧", { exact: true }).waitFor({ timeout: 5_000 });
    await scrubToFrame(page, timeline, 0, TARGET_FRAME, 1_350);
    await page.waitForTimeout(1_250);

    // 撤销是几何复制的重要边界：它是一次可回退的标注更改，不是 AI 作业。
    const undone = waitForTrackUpdate(page, task.id, sourceAnnotationId);
    await page.keyboard.press("Control+Z");
    const restoredKeyframes = annotationKeyframes(await (await undone).json(), sourceAnnotationId);
    if (restoredKeyframes.length !== 1) {
      throw new Error(
        `[video-propagate-track-vs-copy] 撤销后应恢复为 1 个源关键帧，实际为 ${restoredKeyframes.length}`,
      );
    }
    await scrubToFrame(page, timeline, TARGET_FRAME, 0, 850);
    await page.waitForTimeout(650);

    // 第二段：同一源轨迹发起真实 SAM3 延展，并通过候选审阅后回填。
    await aiButton.hover();
    await aiButton.click();
    const aiDialog = page.getByTestId("video-tracker-propagate-dialog");
    await aiDialog.waitFor({ state: "visible", timeout: 5_000 });
    const impact = await aiDialog.getByTestId("tracker-impact-summary").textContent();
    if (impact?.trim() !== `延展当前轨迹「${source.label}」`) {
      throw new Error(
        `[video-propagate-track-vs-copy] AI 作用范围摘要错误：${impact ?? "missing"}`,
      );
    }
    const modelSelect = aiDialog.locator("#tracker-model");
    const modelValues = await modelSelect
      .locator("option")
      .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value));
    const model = ["sam3_video_interactive", "sam2_video"].find((value) =>
      modelValues.includes(value),
    );
    if (!model) throw new Error("[video-propagate-track-vs-copy] 没有可用的真实点框追踪模型");
    await modelSelect.selectOption(model);
    await aiDialog.locator("#tracker-range-preset").selectOption(String(TARGET_FRAME));
    await expect(aiDialog).toContainText("F0 → F30");
    await page.waitForTimeout(1_000);

    const created = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/tasks/${task.id}/video/tracks/${sourceAnnotationId}:propagate`) &&
        response.status() === 202,
      { timeout: 20_000 },
    );
    await aiDialog.getByRole("button", { name: "开始延展", exact: true }).click();
    const createdPayload = (await (await created).json()) as { id?: string };
    if (!createdPayload.id) {
      throw new Error("[video-propagate-track-vs-copy] AI 延展作业未返回 ID");
    }
    onJobCreated(createdPayload.id);

    await page.getByTestId("video-tool-btn-select").click();
    await page
      .getByTestId("tracker-review-tool-capsule")
      .waitFor({ state: "visible", timeout: 120_000 });
    await openContextToolbar(page, "tracker-review");
    const review = page.getByTestId("video-tracker-review-bar");
    await review.waitFor({ state: "visible", timeout: 120_000 });
    await review.getByTestId("tracker-review-instance-1").waitFor({ timeout: 5_000 });
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText(/所选待审 \d+/);
    const fromFrame = review.getByTestId("tracker-review-from-frame");
    await fromFrame.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("1", { delay: 100 });
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 30");
    await scrubToFrame(page, timeline, 0, TARGET_FRAME, 1_500);
    await openContextToolbar(page, "tracker-review");
    await page.waitForTimeout(1_350);

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
    await review.getByTestId("tracker-review-accept").click();
    const [, refreshedResponse] = await Promise.all([accepted, annotationsRefreshed]);
    const refreshed = await refreshedResponse.json();
    if (!Array.isArray(refreshed)) {
      throw new Error("[video-propagate-track-vs-copy] AI 回填后的标注刷新结果不是数组");
    }
    const sourceAfterAi = refreshed.find(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        (item as Record<string, unknown>).id === sourceAnnotationId,
    ) as Record<string, unknown> | undefined;
    const aiKeyframes = sourceAfterAi ? annotationKeyframes(sourceAfterAi, sourceAnnotationId) : [];
    if (aiKeyframes.length < TARGET_FRAME + 1) {
      throw new Error(
        `[video-propagate-track-vs-copy] AI 未回填完整 F0–F30：${aiKeyframes.length} 个关键帧`,
      );
    }
    const expectedF0 = {
      x: source.bbox[0],
      y: source.bbox[1],
      w: source.bbox[2] - source.bbox[0],
      h: source.bbox[3] - source.bbox[1],
    };
    assertSourceFramePreserved(aiKeyframes, expectedF0);
    const frameThirty = aiKeyframes.find((keyframe) => keyframe.frame_index === TARGET_FRAME);
    const frameThirtyBbox = frameThirty?.bbox as NormalizedBbox | undefined;
    if (
      !frameThirtyBbox ||
      frameThirtyBbox.w < expectedF0.w * 0.85 ||
      frameThirtyBbox.h < expectedF0.h * 1.2
    ) {
      throw new Error(
        "[video-propagate-track-vs-copy] F30 AI 结果未保持完整卡车框，拒绝把雨刮器或局部零件冒充卡车",
      );
    }

    await expect(review).toContainText("已审 30/31");
    // Partial acceptance retains the selected frame window. Select the remaining seed frames explicitly.
    const remainingInstances = review.locator('input[data-testid^="tracker-review-instance-"]');
    for (let index = 0; index < (await remainingInstances.count()); index += 1)
      await remainingInstances.nth(index).setChecked(true);
    await review.getByTestId("tracker-review-from-frame").fill("0");
    await review.getByTestId("tracker-review-to-frame").fill("0");
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 1");
    const discardedSeed = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/decisions") &&
        response.ok(),
      { timeout: 20_000 },
    );
    await review.getByTestId("tracker-review-discard").click();
    await discardedSeed;
    await review.waitFor({ state: "hidden", timeout: 8_000 });
    await page.waitForTimeout(1_600);
  } finally {
    page.off("response", collectServerError);
  }

  if (serverErrors.length > 0) {
    throw new Error(
      `[video-propagate-track-vs-copy] 完整链路出现服务端错误：${serverErrors.join(", ")}`,
    );
  }
  return { drawStartMs, drawEndMs: Date.now() };
}
