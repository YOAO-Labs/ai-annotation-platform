import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 高清母版：在漂移帧用笔刷添加、橡皮扣除纠正 Mask，再以原生 Mask seed 向后续帧重传播。
 */
import { expect, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  cocoRleBounds,
  validateCocoRle,
} from "../../../src/pages/Workbench/stage/shared/geometry/maskRle";
import {
  commitPendingAnnotationClass,
  mediaPoint,
  movePointerPathAtRefreshRate,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

type Point = { x: number; y: number };

async function stroke(page: Page, points: Point[], durationMs: number): Promise<void> {
  const first = points[0];
  if (!first || points.length < 2) {
    throw new Error("[video-mask-correction-propagate] 笔迹至少需要两个点");
  }
  await page.mouse.move(first.x, first.y);
  await page.mouse.down();
  await movePointerPathAtRefreshRate(page, points, durationMs);
  await page.mouse.up();
  await page.waitForTimeout(250);
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

async function scrubCorrectionCandidates(
  page: Page,
  timeline: ReturnType<Page["getByTestId"]>,
): Promise<void> {
  await timeline.waitFor({ state: "visible", timeout: 10_000 });
  await seekFrame(page, 18);
  await page.waitForTimeout(1_100);
  await seekFrame(page, 10);
  await page.waitForTimeout(1_100);
}

function assertUpdatedMaskTrack(payload: unknown, annotationId: string): void {
  if (!Array.isArray(payload)) {
    throw new Error("[video-mask-correction-propagate] 标注刷新没有返回数组");
  }
  const annotation = payload.find(
    (item) =>
      typeof item === "object" &&
      item !== null &&
      String((item as Record<string, unknown>).id) === annotationId,
  ) as Record<string, unknown> | undefined;
  if (!annotation) {
    throw new Error("[video-mask-correction-propagate] 纠错后原 Mask 轨迹消失");
  }
  const geometry = annotation.geometry;
  if (
    typeof geometry !== "object" ||
    geometry === null ||
    (geometry as Record<string, unknown>).type !== "video_track_mask"
  ) {
    throw new Error("[video-mask-correction-propagate] 纠错结果不再是视频 Mask 轨迹");
  }
  const keyframes = (geometry as Record<string, unknown>).keyframes;
  if (!Array.isArray(keyframes)) {
    throw new Error("[video-mask-correction-propagate] 纠错轨迹缺少关键帧");
  }
  const manualFrames = keyframes
    .filter(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        (item as Record<string, unknown>).source === "manual",
    )
    .map((item) => Number((item as Record<string, unknown>).frame_index));
  const predictionFrames = keyframes
    .filter(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        (item as Record<string, unknown>).source === "prediction",
    )
    .map((item) => Number((item as Record<string, unknown>).frame_index));
  if (!manualFrames.includes(0) || !manualFrames.includes(5)) {
    throw new Error(
      `[video-mask-correction-propagate] F0/F5 人工关键帧不完整: ${manualFrames.join(",")}`,
    );
  }
  if (predictionFrames.length < 8 || !predictionFrames.every((frame) => frame > 5)) {
    throw new Error(
      "[video-mask-correction-propagate] 后续纠错候选没有写回原轨迹: " + predictionFrames.join(","),
    );
  }
}

async function readJson<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (url) => {
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok) throw new Error(`Mask correction readback: HTTP ${response.status}`);
    return (await response.json()) as T;
  }, path);
}

async function assertCorrectedMaskGeometry(
  page: Page,
  payload: unknown,
  annotationId: string,
  expectedBbox: [number, number, number, number],
): Promise<void> {
  if (!Array.isArray(payload)) return;
  const annotation = payload.find(
    (item) =>
      typeof item === "object" &&
      item !== null &&
      String((item as Record<string, unknown>).id) === annotationId,
  ) as Record<string, unknown> | undefined;
  const geometry = annotation?.geometry as Record<string, unknown> | undefined;
  const keyframes = Array.isArray(geometry?.keyframes) ? geometry.keyframes : [];
  const corrected = keyframes.find(
    (item) =>
      typeof item === "object" &&
      item !== null &&
      Number((item as Record<string, unknown>).frame_index) === 5 &&
      (item as Record<string, unknown>).source === "manual",
  ) as Record<string, unknown> | undefined;
  const original = keyframes.find(
    (item) =>
      typeof item === "object" &&
      item !== null &&
      Number((item as Record<string, unknown>).frame_index) === 0,
  ) as Record<string, unknown> | undefined;
  const correctedRef = corrected?.mask as Record<string, unknown> | undefined;
  const originalRef = original?.mask as Record<string, unknown> | undefined;
  if (
    !correctedRef ||
    correctedRef.encoding !== "coco_rle_ref" ||
    typeof correctedRef.sha256 !== "string" ||
    correctedRef.sha256.length !== 64
  ) {
    throw new Error("[video-mask-correction-propagate] F5 人工关键帧缺少有效 Mask 内容引用");
  }
  if (correctedRef.sha256 === originalRef?.sha256) {
    throw new Error("[video-mask-correction-propagate] F5 纠错帧仍复用了 F0 Mask 内容");
  }

  const correctedRle = validateCocoRle(
    await readJson<unknown>(page, `/api/v1/annotations/${annotationId}/mask-content/5`),
  );
  const bounds = cocoRleBounds(correctedRle);
  if (!bounds) throw new Error("[video-mask-correction-propagate] F5 纠错 Mask 没有前景像素");
  const [expectedMinX, expectedMinY, expectedMaxX, expectedMaxY] = expectedBbox;
  const tolerance = 0.03;
  if (
    bounds.x < expectedMinX - tolerance ||
    bounds.y < expectedMinY - tolerance ||
    bounds.x + bounds.w > expectedMaxX + tolerance ||
    bounds.y + bounds.h > expectedMaxY + tolerance
  ) {
    throw new Error(
      `[video-mask-correction-propagate] F5 纠错 Mask 超出前景驾驶室范围: ` +
        `${JSON.stringify(bounds)} vs ${JSON.stringify(expectedBbox)}`,
    );
  }
}

export async function runVideoMaskCorrectionPropagate(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.video_demo;
  await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.tracking.id}`);
  const stage = page.getByTestId("video-konva-stage");
  const timeline = page.getByTestId("video-timeline-shell");
  await timeline.waitFor({ timeout: 15_000 });
  await stage.waitFor({ timeout: 10_000 });
  await page.addStyleTag({
    content: '[data-testid="video-frame-preview-popover"] { display: none !important; }',
  });
  await page.waitForTimeout(900);

  const bounds = await renderedMediaBounds(stage);
  const initial = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
  const drift = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f5", 5);
  if (initial.label !== "truck" || drift.label !== initial.label) {
    throw new Error("[video-mask-correction-propagate] 纠错锚点不是同一辆卡车");
  }
  if (initial.brush_strokes.length === 0) {
    throw new Error("[video-mask-correction-propagate] 初始卡车缺少 Mask 笔刷锚点");
  }

  // 录制窗口外创建一条有意带漏分与右侧外溢的 F0 Mask；正式母版从 F5 错误边界开始。
  await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
  await page.getByTestId("video-tool-btn-mask-track").click();
  await page.getByTestId("mask-tool-capsule").waitFor({ state: "visible", timeout: 5000 });
  await page.getByTestId("mask-settings-trigger").click();
  await page.getByLabel("更多 Mask 工具").click();
  const toolbar = page.getByTestId("mask-toolbar");
  await toolbar.waitFor({ timeout: 10_000 });
  const initialPath = initial.brush_strokes.flatMap((path, index) => {
    const points = path.map((point) => mediaPoint(bounds, point));
    return index % 2 === 0 ? points : points.reverse();
  });
  await stroke(page, initialPath, 1_400);
  await stroke(
    page,
    [
      mediaPoint(bounds, [0.7, 0.52]),
      mediaPoint(bounds, [0.746, 0.52]),
      mediaPoint(bounds, [0.746, 0.72]),
      mediaPoint(bounds, [0.7, 0.72]),
    ],
    650,
  );
  const confirmed = toolbar.getByTitle("确认 (Enter)");
  await confirmed.waitFor({ state: "visible", timeout: 10_000 });
  await confirmed.click();
  await commitPendingAnnotationClass(page, {
    label: initial.label,
    taskId: project.tasks.tracking.id,
  });
  await toolbar.waitFor({ state: "hidden", timeout: 15_000 });

  const trackRow = page.locator('[data-testid^="video-mask-track-"]').last();
  await trackRow.waitFor({ timeout: 15_000 });
  const testId = await trackRow.getAttribute("data-testid");
  const annotationId = testId?.replace("video-mask-track-", "");
  if (!annotationId) {
    throw new Error("[video-mask-correction-propagate] 无法读取初始 Mask 轨迹 ID");
  }
  const collapseSelection = page.getByRole("button", { name: "收起浮窗" });
  await collapseSelection.waitFor({ state: "visible", timeout: 5_000 });
  await collapseSelection.click();
  for (let frame = 0; frame < 5; frame += 1) {
    await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(150);
  }
  await page
    .getByText(/保持 F0/)
    .first()
    .waitFor({ state: "visible", timeout: 10_000 });
  await page.waitForTimeout(700);

  const serverErrors: string[] = [];
  const collectServerError = (response: Response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  };
  page.on("response", collectServerError);
  const drawStartMs = Date.now();
  try {
    await page.waitForTimeout(1_200);
    await page.getByLabel(/展开选中信息卡.*可拖动/).click();
    const editMaskButton = page.getByTitle("编辑当前帧 Mask");
    await editMaskButton.waitFor({ state: "visible", timeout: 10_000 });
    const hitTest = await editMaskButton.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const target = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      );
      return {
        targetMatches: target === element || (target instanceof Node && element.contains(target)),
        targetDescription:
          target instanceof HTMLElement
            ? `${target.tagName.toLowerCase()}[title="${target.getAttribute("title") ?? ""}"]`
            : (target?.nodeName ?? null),
      };
    });
    expect(
      hitTest.targetMatches,
      `Mask edit button is physically occluded by ${hitTest.targetDescription}`,
    ).toBe(true);
    await editMaskButton.click();
    await expect(page.getByTestId("video-tool-btn-mask-track")).toHaveAttribute(
      "aria-pressed",
      "true",
      { timeout: 10_000 },
    );
    await toolbar.waitFor({ timeout: 10_000 });
    const collapseEditorSelection = page.getByRole("button", { name: "收起浮窗" });
    if (await collapseEditorSelection.isVisible()) await collapseEditorSelection.click();
    await toolbar.getByTitle("笔刷 (B)").click();
    // F5 的真实目标是前景白色 Skyline 驾驶室；其车顶约在 y=.45，
    // 上方 y=.38-.43 是后方公交/车辆。把补入笔迹限制在驾驶室上沿内，
    // 避免 SAM3 把后方高车厢一起吸进纠错 seed。
    await stroke(
      page,
      [
        mediaPoint(bounds, [0.515, 0.47]),
        mediaPoint(bounds, [0.675, 0.47]),
        mediaPoint(bounds, [0.675, 0.51]),
        mediaPoint(bounds, [0.515, 0.51]),
      ],
      1_050,
    );
    await page.waitForTimeout(800);

    await toolbar.getByTitle("橡皮 (E)").click();
    await page.waitForTimeout(450);
    await stroke(page, [mediaPoint(bounds, [0.48, 0.425]), mediaPoint(bounds, [0.72, 0.425])], 900);
    await stroke(page, [mediaPoint(bounds, [0.73, 0.44]), mediaPoint(bounds, [0.73, 0.75])], 1_050);
    // The deliberate F0 spill ends at x=.746; the brush radius leaves a thin
    // fringe around x=.763, so overlap a second vertical erase stroke there.
    await stroke(page, [mediaPoint(bounds, [0.75, 0.44]), mediaPoint(bounds, [0.75, 0.75])], 1_050);
    await page.waitForTimeout(900);

    await toolbar.getByRole("button", { name: "保存并传播" }).click();
    const correctionDialog = page.getByRole("dialog", { name: "保存 Mask 纠错帧" });
    await correctionDialog.waitFor({ state: "visible", timeout: 5_000 });
    await correctionDialog.getByText(/原生 Mask seed/).waitFor({ timeout: 5_000 });
    await page.waitForTimeout(900);
    await correctionDialog.getByRole("radio", { name: "更晚帧 →" }).click();
    await correctionDialog.getByText(/生效窗口 F5–F20/).waitFor({ timeout: 3_000 });
    await page.waitForTimeout(1_100);

    const keyframeSaved = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(
            `/tasks/${project.tasks.tracking.id}/video/tracks/${annotationId}/mask-keyframes/5`,
          ) &&
        response.ok(),
      { timeout: 25_000 },
    );
    const jobCreated = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(
            `/tasks/${project.tasks.tracking.id}/video/tracks/${annotationId}/correction-jobs`,
          ) &&
        response.status() === 202,
      { timeout: 25_000 },
    );
    await correctionDialog.getByRole("button", { name: "保存并启动传播" }).click();
    await Promise.all([keyframeSaved, jobCreated]);

    await page
      .getByTestId("tracker-review-tool-capsule")
      .waitFor({ state: "visible", timeout: 120_000 });
    await openContextToolbar(page, "tracker-review");
    const review = page.getByTestId("video-tracker-review-bar");
    await expectCorrectionSummary(review);
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText(/所选待审 \d+/);
    await page.waitForTimeout(1_200);

    await scrubCorrectionCandidates(page, timeline);
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
        response.url().endsWith(`/tasks/${project.tasks.tracking.id}/annotations`) &&
        response.ok(),
      { timeout: 20_000 },
    );
    await review.getByTestId("tracker-review-accept").click();
    const [, annotationsResponse] = await Promise.all([accepted, annotationsRefreshed]);
    const annotationsPayload = await annotationsResponse.json();
    assertUpdatedMaskTrack(annotationsPayload, annotationId);
    await assertCorrectedMaskGeometry(
      page,
      annotationsPayload,
      annotationId,
      [0.48, 0.44, 0.71, 0.84],
    );
    await review.waitFor({ state: "hidden", timeout: 8_000 });
    // 采纳后切一帧再返回，展示正式轨迹 Mask；等待 Konva 的 Mask 与标签层都完成重绘，
    // 防止旧 ImageBitmap 阻塞同帧队列后把“保持 F0”残影录进母版。
    await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(450);
    await page.keyboard.press("ArrowLeft");
    const canvasSettled = await page.waitForFunction(
      () => {
        const runtime = (
          window as typeof window & {
            Konva?: {
              stages?: Array<{
                getLayers: () => Array<{ name: () => string; _waitingForDraw?: boolean }>;
                find: (selector: string) => Array<{
                  text?: () => string;
                  image?: () => { width?: number; height?: number };
                  getLayer: () => { name: () => string } | null;
                }>;
              }>;
            };
          }
        ).Konva;
        const stage = runtime?.stages?.[0];
        if (!stage) return false;
        const currentLabelVisible = stage
          .find("Text")
          .some((node) => node.getLayer()?.name() === "overlay" && node.text?.() === "#1 · truck");
        const canvasLayersSettled = stage
          .getLayers()
          .filter((layer) =>
            ["video-mask-layer", "overlay", "ai", "interaction"].includes(layer.name()),
          )
          .every((layer) => !layer._waitingForDraw);
        const maskVisible = stage.find(".raster-mask-fill").some((node) => {
          const image = node.image?.();
          return (image?.width ?? 0) > 0 && (image?.height ?? 0) > 0;
        });
        return currentLabelVisible && canvasLayersSettled && maskVisible;
      },
      null,
      { timeout: 10_000 },
    );
    await canvasSettled.dispose();
    await page.waitForTimeout(900);
  } finally {
    page.off("response", collectServerError);
  }

  if (serverErrors.length > 0) {
    throw new Error(
      `[video-mask-correction-propagate] 纠错与传播期间出现服务端错误: ${serverErrors.join(", ")}`,
    );
  }
  return { drawStartMs, drawEndMs: Date.now() };
}

async function expectCorrectionSummary(review: ReturnType<Page["getByRole"]>): Promise<void> {
  const summary = review.getByTestId("tracker-review-correction-summary");
  await summary.waitFor({ state: "visible", timeout: 5_000 });
  const text = (await summary.textContent()) ?? "";
  for (const expected of ["F5 人工纠错帧", "向更晚帧", "原生 Mask seed", "保护人工帧"]) {
    if (!text.includes(expected)) {
      throw new Error(`[video-mask-correction-propagate] 纠错摘要缺少“${expected}”：${text}`);
    }
  }
}
