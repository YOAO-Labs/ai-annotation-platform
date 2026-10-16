import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 高清母版：SAM3 combo 先按文本发现，再用逐对象 PVS memory 跨窗追踪并人工采纳。
 */
import { expect, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate, normalizedBboxIoU, recordingAnchor } from "./_canvas";
import type { NormalizedBbox } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

function assertComboTracks(
  payload: unknown,
  targetClass: string,
  expectedTargets: Array<[number, number, number, number]>,
): void {
  if (!Array.isArray(payload)) {
    throw new Error("[video-tracker-combo-discovery] 标注刷新没有返回数组");
  }

  const tracks: NormalizedBbox[] = [];
  for (const item of payload) {
    if (
      typeof item !== "object" ||
      item === null ||
      (item as Record<string, unknown>).class_name !== targetClass
    ) {
      continue;
    }
    const geometry = (item as Record<string, unknown>).geometry;
    if (typeof geometry !== "object" || geometry === null) continue;
    const keyframes = (geometry as Record<string, unknown>).keyframes;
    if (!Array.isArray(keyframes) || keyframes.length < 24) {
      throw new Error(
        `[video-tracker-combo-discovery] ${targetClass} 轨迹关键帧不足: ` +
          `${Array.isArray(keyframes) ? keyframes.length : 0}`,
      );
    }
    const frameZero = keyframes.find(
      (keyframe) =>
        typeof keyframe === "object" &&
        keyframe !== null &&
        (keyframe as Record<string, unknown>).frame_index === 0,
    ) as Record<string, unknown> | undefined;
    const bbox = frameZero?.bbox;
    if (typeof bbox === "object" && bbox !== null) {
      tracks.push(bbox as unknown as NormalizedBbox);
    }
  }

  if (tracks.length !== expectedTargets.length) {
    throw new Error(
      `[video-tracker-combo-discovery] 应采纳 ${expectedTargets.length} 条轨迹，实际为 ${tracks.length}`,
    );
  }

  const unmatched = new Set(tracks.map((_, index) => index));
  for (const expected of expectedTargets) {
    const expectedBbox = {
      x: expected[0],
      y: expected[1],
      w: expected[2] - expected[0],
      h: expected[3] - expected[1],
    };
    let bestIndex = -1;
    let bestOverlap = 0;
    for (const index of unmatched) {
      const overlap = normalizedBboxIoU(tracks[index]!, expectedBbox);
      if (overlap > bestOverlap) {
        bestIndex = index;
        bestOverlap = overlap;
      }
    }
    if (bestIndex < 0 || bestOverlap < 0.6) {
      throw new Error(
        "[video-tracker-combo-discovery] combo 轨迹没有完整命中左右公交车: " +
          `bestIoU=${bestOverlap.toFixed(3)}, expected=${JSON.stringify(expectedBbox)}, ` +
          `actual=${JSON.stringify(tracks)}`,
      );
    }
    unmatched.delete(bestIndex);
  }
}

async function scrubAcrossComboWindows(
  page: Page,
  timeline: ReturnType<Page["getByTestId"]>,
): Promise<void> {
  const box = await timeline.boundingBox();
  if (!box) throw new Error("[video-tracker-combo-discovery] 时间轴不可见");
  const y = box.y + box.height * 0.5;
  const frameZero = { x: box.x + 2, y };
  const secondWindow = { x: box.x + box.width * 0.38, y };
  const firstWindow = { x: box.x + box.width * 0.14, y };

  await page.mouse.move(frameZero.x, frameZero.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, frameZero, secondWindow, 2_100);
  await page.mouse.up();
  await page.getByText(/^F 2[5-9] \/ 71$/).waitFor({ timeout: 3_000 });
  await page.waitForTimeout(1_100);

  await page.mouse.down();
  await movePointerAtRefreshRate(page, secondWindow, firstWindow, 1_500);
  await page.mouse.up();
  await page.getByText(/^F (?:9|10|11) \/ 71$/).waitFor({ timeout: 3_000 });
  await page.waitForTimeout(1_100);
}

export async function runVideoTrackerComboDiscovery(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.video_demo;
  await page.evaluate(() => {
    localStorage.setItem("wb:video-tracker-panel-position", JSON.stringify({ left: 1090, top: 8 }));
    localStorage.removeItem("wb:video-tracker-panel-size");
  });
  await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.tracking.id}`);
  const stage = page.getByTestId("video-konva-stage");
  const timeline = page.getByTestId("video-timeline-shell");
  await timeline.waitFor({ timeout: 15_000 });
  await stage.waitFor({ timeout: 10_000 });
  await page.addStyleTag({
    content: '[data-testid="video-frame-preview-popover"] { display: none !important; }',
  });
  await page.waitForTimeout(900);

  const leftBus = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0);
  const rightBus = recordingAnchor(catalog, "video_demo", "tracking", "right_bus_f0", 0);
  if (leftBus.label !== "bus" || rightBus.label !== leftBus.label) {
    throw new Error("[video-tracker-combo-discovery] 录制锚点不是两辆独立公交车");
  }

  await page.getByTestId("workbench-ai-tracker").click();
  const dialog = page.getByTestId("video-tracker-propagate-dialog");
  await dialog.waitFor({ timeout: 5_000 });
  await page.waitForTimeout(800);

  const model = dialog.locator("#tracker-model");
  const modelValues = await model
    .locator("option")
    .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value));
  if (!modelValues.includes("sam3_video_combo")) {
    throw new Error("[video-tracker-combo-discovery] 当前项目没有真实 SAM3 combo 能力");
  }
  await model.selectOption("sam3_video_combo");
  await page.waitForTimeout(700);
  await dialog.getByTestId("tracker-target-class").selectOption(leftBus.label);
  await page.waitForTimeout(500);
  await dialog.getByTestId("tracker-output-geometry").selectOption("bbox");
  await page.waitForTimeout(500);
  const textInput = dialog.getByTestId("tracker-text-input");
  await textInput.click();
  await textInput.pressSequentially("bus", { delay: 120 });
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_000);

  const serverErrors: string[] = [];
  const collectServerError = (response: Response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  };
  page.on("response", collectServerError);
  try {
    await dialog.getByRole("button", { name: "开始发现" }).click();
    await page.getByTestId("video-tool-btn-select").click();
    await page
      .getByTestId("tracker-review-tool-capsule")
      .waitFor({ state: "visible", timeout: 120_000 });
    await openContextToolbar(page, "tracker-review");
    const review = page.getByTestId("video-tracker-review-bar");
    await review.waitFor({ state: "visible", timeout: 120_000 });
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 217");
    const instances = review.locator('input[data-testid^="tracker-review-instance-"]');
    if ((await instances.count()) !== 7) {
      throw new Error(
        `[video-tracker-combo-discovery] combo 应保持 7 个跨窗身份，实际为 ${await instances.count()}`,
      );
    }
    await page.waitForTimeout(1_000);

    for (const instanceId of ["1", "3", "5", "6", "7"]) {
      await review.getByTestId(`tracker-review-instance-${instanceId}`).click();
      await page.waitForTimeout(150);
    }
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 62");
    await page.waitForTimeout(1_200);

    await scrubAcrossComboWindows(page, timeline);
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
    assertComboTracks(await annotationsResponse.json(), leftBus.label, [
      leftBus.bbox,
      rightBus.bbox,
    ]);

    await expect(review).toContainText("已审 62/217");
    // Accepted instances leave the current selection; explicitly select the remaining targets.
    const remainingInstances = review.locator('input[data-testid^="tracker-review-instance-"]');
    for (let index = 0; index < (await remainingInstances.count()); index += 1)
      await remainingInstances.nth(index).setChecked(true);
    await expect(review.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 155");
    await page.waitForTimeout(800);
    const rejected = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/decisions") &&
        response.ok(),
      { timeout: 20_000 },
    );
    await review.getByTestId("tracker-review-discard").click();
    await rejected;
    await review.waitFor({ state: "hidden", timeout: 5_000 });
    await page.waitForTimeout(1_000);
  } finally {
    page.off("response", collectServerError);
  }

  if (serverErrors.length > 0) {
    throw new Error(
      `[video-tracker-combo-discovery] combo 发现与采纳期间出现服务端错误: ${serverErrors.join(", ")}`,
    );
  }
  return { drawStartMs, drawEndMs: Date.now() };
}
