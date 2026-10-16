import { openContextToolbar } from "../../fixtures/context-toolbar";
/** Record a seeded range brush, real tracking, and the saved trajectory within that range. */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { VideoTrackerJob, VideoTrackerJobPreview } from "../../../src/api/videoTracker";
import {
  mediaBbox,
  movePointerAtRefreshRate,
  normalizedBboxIoU,
  recordingAnchor,
  renderedMediaBounds,
  type NormalizedBbox,
} from "./_canvas";
import {
  openVideoTimeline,
  currentVideoFrame,
  parkVideoPointer,
  assertVideoTimelineVisible,
} from "./_video-timeline";
import type { DrawWindow } from "./rotated-bbox";

type SavedTrack = {
  id: string;
  task_id: string;
  class_name: string;
  version: number;
  geometry: {
    type: string;
    track_id: string;
    keyframes: Array<{ frame_index: number; bbox: NormalizedBbox }>;
  };
};

export async function runVideoTrackerRange(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onJobCreated?: (jobId: string) => void,
  onAnnotationsCreated?: (ids: string[]) => void,
): Promise<DrawWindow & { evidence: object }> {
  const taskId = catalog.projects.video_demo.tasks.tracking.id;
  const annotationsResponse = () =>
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname === `/api/v1/tasks/${taskId}/annotations` &&
        response.ok(),
      { timeout: 30_000 },
    );
  const baselineResponse = annotationsResponse();
  const maxFrame = await openVideoTimeline(page, catalog);
  const baselineIds = new Set(
    ((await (await baselineResponse).json()) as SavedTrack[]).map((item) => item.id),
  );
  const timeline = page.getByTestId("video-timeline-shell");
  const stage = page.getByTestId("video-konva-stage");
  const drawStartMs = Date.now();
  await page.getByTestId("workbench-ai-tracker").click();
  const dialog = page.getByTestId("video-tracker-propagate-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toBeInViewport({ ratio: 1 });
  const modelSelect = dialog.locator("#tracker-model");
  const models = await modelSelect.locator("option").evaluateAll((options) =>
    options.flatMap((option) => {
      const value = option as HTMLOptionElement;
      return value.disabled ? [] : [value.value];
    }),
  );
  const model = ["sam3_video_interactive", "sam2_video"].find((value) => models.includes(value));
  if (!model) throw new Error("Video range recording requires a real enabled interactive tracker");
  await modelSelect.selectOption(model);
  await dialog.getByTestId("tracker-direction-forward").click();
  await dialog.locator("#tracker-range-preset").selectOption("30");
  await dialog.getByTestId("tracker-output-geometry").selectOption("bbox");
  const anchors = [
    recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0),
    recordingAnchor(catalog, "video_demo", "tracking", "right_bus_f0", 0),
  ];
  const anchor = anchors[0]!;
  expect(anchors.every((target) => target.label === anchor.label)).toBe(true);
  await dialog.getByTestId("tracker-target-class").selectOption(anchor.label);
  await parkVideoPointer(page);
  await assertVideoTimelineVisible(page);
  await page.waitForTimeout(900);

  await page.getByTestId("tracker-seed-mode-box").click();
  const media = await renderedMediaBounds(stage);
  for (const [index, target] of anchors.entries()) {
    if (index > 0) await dialog.getByTestId("tracker-seed-new-target").click();
    await dialog.getByTestId("tracker-seed-toggle").click();
    const targetBox = mediaBbox(media, target.bbox);
    await page.mouse.move(targetBox.start.x, targetBox.start.y);
    await page.mouse.down();
    await movePointerAtRefreshRate(page, targetBox.start, targetBox.end, 700);
    await page.mouse.up();
    await page.waitForTimeout(650);
    await expect(dialog.getByTestId(`tracker-seed-target-${index + 1}`)).toContainText("1 框");
    await dialog.getByTestId("tracker-seed-toggle").click();
  }
  await parkVideoPointer(page);
  await page.waitForTimeout(700);

  const endFrame = 20;
  const bounds = await timeline.boundingBox();
  if (!bounds) throw new Error("Video timeline is not visible");
  const start = { x: bounds.x + 2, y: bounds.y + bounds.height * 0.55 };
  const end = { x: bounds.x + (bounds.width * endFrame) / maxFrame, y: start.y };
  await page.keyboard.down("Shift");
  try {
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    await movePointerAtRefreshRate(page, start, end, 1100, { shiftKey: true });
    await page.mouse.up();
  } finally {
    await page.keyboard.up("Shift");
  }
  await expect(dialog.getByTestId("tracker-range-custom")).toBeVisible();
  await expect(dialog.getByTestId("tracker-range-custom").locator("..")).toContainText(
    `F0 → F${endFrame}`,
  );
  expect(await currentVideoFrame(page), "Range brushing must not seek away from the seed").toBe(0);
  await expect(page.getByTestId("video-propagate-range")).toBeVisible();
  await parkVideoPointer(page);
  await assertVideoTimelineVisible(page);
  await page.waitForTimeout(1500);

  const createdResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/tasks/${taskId}/video:track`,
    { timeout: 30_000 },
  );
  const previewResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      /\/video-tracker-jobs\/[^/]+\/preview$/.test(new URL(response.url()).pathname) &&
      response.ok(),
    { timeout: 180_000 },
  );
  await dialog.getByRole("button", { name: "开始发现", exact: true }).click();
  const created = await createdResponse;
  expect(created.ok()).toBe(true);
  const job = (await created.json()) as VideoTrackerJob;
  onJobCreated?.(job.id);
  const request = created.request().postDataJSON();
  expect(request).toMatchObject({
    from_frame: 0,
    to_frame: endFrame,
    model_key: model,
    direction: "forward",
    output_geometry: "bbox",
    target_class_name: anchor.label,
  });
  expect(request.prompt.seeds).toHaveLength(anchors.length);
  const expectedBoxes = anchors.map((target) => ({
    x: target.bbox[0],
    y: target.bbox[1],
    w: target.bbox[2] - target.bbox[0],
    h: target.bbox[3] - target.bbox[1],
  }));
  for (const [index, target] of anchors.entries()) {
    const submitted = request.prompt.seeds.find(
      (seed: { obj_id: number }) => seed.obj_id === index + 1,
    );
    expect(submitted).toBeTruthy();
    expect(submitted.prompts).toHaveLength(1);
    expect(submitted.prompts[0]).toMatchObject({ frame_index: 0 });
    expect(submitted.prompts[0].points ?? []).toEqual([]);
    expect(normalizedBboxIoU(submitted.prompts[0].bbox, expectedBoxes[index]!)).toBeGreaterThan(
      0.95,
    );
    expect(target.label).toBe(anchor.label);
  }
  const preview = (await (await previewResponse).json()) as VideoTrackerJobPreview;
  expect(preview).toMatchObject({
    job_id: job.id,
    from_frame: 0,
    to_frame: endFrame,
    output_geometry: "bbox",
  });
  expect(preview.results.length).toBeGreaterThanOrEqual((endFrame + 1) * anchors.length);
  expect(new Set(preview.results.map((result) => result.instance_id)).size).toBe(anchors.length);
  expect(
    preview.results.every((result) => result.frame_index >= 0 && result.frame_index <= endFrame),
  ).toBe(true);
  await page.getByTestId("video-tool-btn-select").click();
  await page
    .getByTestId("tracker-review-tool-capsule")
    .waitFor({ state: "visible", timeout: 120_000 });
  await openContextToolbar(page, "tracker-review");
  const review = page.getByTestId("video-tracker-review-bar");
  await expect(review).toBeVisible({ timeout: 10_000 });
  await page.getByRole("tab", { name: "标注详情", exact: true }).click();
  await expect(page.getByTestId("video-propagate-range")).toHaveCount(0);
  await parkVideoPointer(page);
  await page.waitForTimeout(1400);
  await page
    .getByRole("slider", { name: "视频帧时间轴", exact: true })
    .fill(String(Math.round((10 / maxFrame) * 10000)));
  await expect.poll(() => currentVideoFrame(page)).toBe(10);
  await parkVideoPointer(page);
  await page.waitForTimeout(1400);

  const acceptedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/video-tracker-jobs/${job.id}/decisions`,
    { timeout: 30_000 },
  );
  const savedResponse = annotationsResponse().then(async (response) => {
    const added = ((await response.json()) as SavedTrack[]).filter(
      (item) => !baselineIds.has(item.id),
    );
    onAnnotationsCreated?.(added.map((item) => item.id));
    return added;
  });
  await openContextToolbar(page, "tracker-review");
  await review.getByTestId("tracker-review-accept").click();
  const [accepted, saved] = await Promise.all([acceptedResponse, savedResponse]);
  expect(accepted.ok()).toBe(true);
  const acceptedJob = (await accepted.json()) as VideoTrackerJob;
  expect(acceptedJob.status).toBe("accepted");
  expect(accepted.request().postDataJSON()).toMatchObject({
    decision: "accept",
    from_frame: 0,
    to_frame: endFrame,
  });
  expect(saved).toHaveLength(anchors.length);
  for (const track of saved) {
    expect(track).toMatchObject({
      task_id: taskId,
      class_name: anchor.label,
      geometry: { type: "video_track_bbox" },
    });
    expect(track.geometry.track_id).toBeTruthy();
    expect(track.version).toBeGreaterThan(0);
    expect(track.geometry.keyframes.map((frame) => frame.frame_index)).toEqual(
      Array.from({ length: endFrame + 1 }, (_, i) => i),
    );
  }
  const matched = expectedBoxes.map((expectedBox) => {
    const track = saved.find(
      (item) => normalizedBboxIoU(item.geometry.keyframes[0]!.bbox, expectedBox) >= 0.6,
    );
    expect(
      track,
      "Each saved track must cover its seeded bus rather than a local part",
    ).toBeTruthy();
    return {
      track: track!,
      overlap: normalizedBboxIoU(track!.geometry.keyframes[0]!.bbox, expectedBox),
    };
  });
  expect(new Set(matched.map(({ track }) => track.id)).size).toBe(anchors.length);
  expect(new Set(saved.map((track) => track.geometry.track_id)).size).toBe(anchors.length);
  await expect(review).toBeHidden();
  await expect(page.getByTestId("video-track-row").filter({ hasText: anchor.label })).toHaveCount(
    anchors.length,
  );
  await parkVideoPointer(page);
  await assertVideoTimelineVisible(page);
  await page.waitForTimeout(1800);
  const drawEndMs = Date.now();
  const reloadedResponse = annotationsResponse();
  await page.reload();
  const reloaded = (await (await reloadedResponse).json()) as SavedTrack[];
  for (const track of saved) {
    expect(reloaded.find((item) => item.id === track.id)).toMatchObject({
      id: track.id,
      version: track.version,
      geometry: track.geometry,
    });
  }
  return {
    drawStartMs,
    drawEndMs,
    evidence: {
      model,
      job,
      request,
      preview,
      acceptedJob,
      acceptedAnnotationIds: saved.map((track) => track.id),
      acceptedTracks: saved,
      seedAnchorIoU: matched.map(({ track, overlap }) => ({ annotationId: track.id, overlap })),
      reload: {
        verified: true,
        tracks: reloaded.filter((item) => saved.some((track) => track.id === item.id)),
      },
    },
  };
}
