import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 流程录制：按目标外观选择点、正负点或整车框，分别演示三种追踪种子。
 */
import { expect, type Page, type Response } from "@playwright/test";
import type {
  VideoTrackerJob,
  VideoTrackerJobPreview,
  VideoTrackerPropagatePayload,
} from "../../../src/api/videoTracker";
import { assertVideoTimelineVisible, currentVideoFrame, parkVideoPointer } from "./_video-timeline";
import { readVideoRecordingJson, setVideoRecordingTimeline } from "./_video-keyframe-recording";
import type { RecordedVideoTrack } from "./_video-keyframe-evidence";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  mediaBbox,
  mediaPoint,
  movePointerAtRefreshRate,
  normalizedBboxIoU,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import type { NormalizedBbox } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

export type VideoMultiSeedVariant = "cross-frame-points" | "positive-negative" | "box-seed";

const VARIANT_LABELS: Record<VideoMultiSeedVariant, string> = {
  "cross-frame-points": "跨帧多正点",
  "positive-negative": "正负点修正",
  "box-seed": "整车框种子",
};

function assertAcceptedTracksCoverTargets(
  payload: unknown,
  targetClass: string,
  expectedTargets: Array<[number, number, number, number]>,
  variant: VideoMultiSeedVariant,
): void {
  if (!Array.isArray(payload)) {
    throw new Error(`[video-multi-seed:${variant}] 标注刷新没有返回数组`);
  }
  const tracks: Array<{
    frameZero: NormalizedBbox;
    keyframes: Array<{ frameIndex: number; bbox: NormalizedBbox }>;
  }> = [];
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
        `[video-multi-seed:${variant}] ${targetClass} 轨迹有效关键帧不足: ` +
          `${Array.isArray(keyframes) ? keyframes.length : 0}`,
      );
    }
    const parsedKeyframes = keyframes.flatMap((keyframe) => {
      if (typeof keyframe !== "object" || keyframe === null) return [];
      const record = keyframe as Record<string, unknown>;
      const frameIndex = record.frame_index;
      const bbox = record.bbox;
      if (typeof frameIndex !== "number" || typeof bbox !== "object" || bbox === null) {
        return [];
      }
      return [{ frameIndex, bbox: bbox as NormalizedBbox }];
    });
    const frameZero = parsedKeyframes.find((keyframe) => keyframe.frameIndex === 0);
    if (frameZero) tracks.push({ frameZero: frameZero.bbox, keyframes: parsedKeyframes });
  }
  if (tracks.length < expectedTargets.length) {
    throw new Error(
      `[video-multi-seed:${variant}] 接受后仅有 ${tracks.length}/${expectedTargets.length} 条有效 ${targetClass} 轨迹`,
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
      const overlap = normalizedBboxIoU(tracks[index]!.frameZero, expectedBbox);
      if (overlap > bestOverlap) {
        bestIndex = index;
        bestOverlap = overlap;
      }
    }
    if (bestIndex < 0 || bestOverlap < 0.6) {
      throw new Error(
        `[video-multi-seed:${variant}] 双目标结果只命中局部或漏掉目标主体: ` +
          `bestIoU=${bestOverlap.toFixed(3)}, expected=${JSON.stringify(expectedBbox)}, ` +
          `actual=${JSON.stringify(tracks)}`,
      );
    }
    const acceptedTrack = tracks[bestIndex]!;
    const minimumWidth = expectedBbox.w * 0.62;
    const minimumHeight = expectedBbox.h * 0.62;
    for (const frameIndex of [4, 15, 30]) {
      const keyframe = acceptedTrack.keyframes.find((item) => item.frameIndex === frameIndex);
      if (!keyframe)
        throw new Error(`[video-multi-seed:${variant}] 缺少 F${frameIndex} 的追踪结果`);
      if (keyframe.bbox.w < minimumWidth || keyframe.bbox.h < minimumHeight) {
        throw new Error(
          `[video-multi-seed:${variant}] ${targetClass} 轨迹在 F${frameIndex} 缩成局部目标: ` +
            `expected>=${minimumWidth.toFixed(3)}×${minimumHeight.toFixed(3)}, ` +
            `actual=${keyframe.bbox.w.toFixed(3)}×${keyframe.bbox.h.toFixed(3)}`,
        );
      }
    }
    unmatched.delete(bestIndex);
  }
}

async function moveToFrame(page: Page, frame: number): Promise<void> {
  const current = await currentVideoFrame(page);
  if (frame === 0) {
    await page.getByRole("button", { name: "回到首帧", exact: true }).click();
  } else {
    const direction = frame > current ? "下一帧" : "上一帧";
    for (let next = current; next !== frame; ) {
      next += frame > current ? 1 : -1;
      await page.getByRole("button", { name: direction, exact: true }).click();
      await expect.poll(() => currentVideoFrame(page)).toBe(next);
      await page.waitForTimeout(120);
    }
  }
  await expect.poll(() => currentVideoFrame(page)).toBe(frame);
  await parkVideoPointer(page);
  await assertVideoTimelineVisible(page);
}

async function scrubPendingTrackerFrames(page: Page): Promise<void> {
  const slider = page.getByRole("slider", { name: "视频帧时间轴", exact: true });
  const box = await slider.boundingBox();
  if (!box) throw new Error("[video-multi-seed] 视频帧时间轴不可见");
  const readout = await page.getByTestId("video-timeline-window-readout").innerText();
  const maximum = /全部\s*·\s*F0–F?(\d+)/.exec(readout);
  if (!maximum) throw new Error(`Expected full video range: ${readout}`);
  const maxFrame = Number(maximum[1]);
  const y = box.y + box.height / 2;
  // Native range thumbs have a small inset; assert the actual resulting frame, not pixel ratios.
  let from = { x: box.x + 8, y };
  for (const frame of [26, 10]) {
    const to = { x: box.x + 8 + ((box.width - 16) * frame) / maxFrame, y };
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await movePointerAtRefreshRate(page, from, to, 1500);
    await page.mouse.up();
    await expect
      .poll(async () => Math.abs((await currentVideoFrame(page)) - frame))
      .toBeLessThanOrEqual(1);
    await parkVideoPointer(page);
    await assertVideoTimelineVisible(page);
    await page.waitForTimeout(900);
    from = to;
  }
}

interface SubmittedSeed {
  obj_id: number;
  prompts: Array<{ frame_index: number; points?: number[][]; bbox?: NormalizedBbox }>;
}

export async function runVideoMultiSeedTracking(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  variant: VideoMultiSeedVariant,
  onJobCreated?: (jobId: string) => void,
  onAnnotationsCreated?: (ids: string[]) => void,
): Promise<DrawWindow & { evidence: Record<string, unknown> }> {
  const project = catalog.projects.video_demo;
  const label = VARIANT_LABELS[variant];
  await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.tracking.id}`);
  const stage = page.getByTestId("video-konva-stage");
  await expect(stage).toBeVisible({ timeout: 20_000 });
  await expect
    .poll(() =>
      page
        .getByTestId("video-konva-source")
        .evaluate((video: HTMLVideoElement) => video.readyState),
    )
    .toBeGreaterThanOrEqual(2);
  await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", true);
  await setVideoRecordingTimeline(page, true);
  await moveToFrame(page, 0);
  const reset = page.getByRole("button", { name: "适配全部帧", exact: true });
  if (await reset.isEnabled()) await reset.click();
  const annotationsPath = `/api/v1/tasks/${project.tasks.tracking.id}/annotations`;
  const baseline = await readVideoRecordingJson<RecordedVideoTrack[]>(page, annotationsPath);
  const baselineIds = new Set(baseline.map((annotation) => annotation.id));
  const drawStartMs = Date.now();
  const dialog = page.getByTestId("video-tracker-propagate-dialog");
  if (!(await dialog.isVisible())) await page.getByTestId("workbench-ai-tracker").click();
  await dialog.waitFor({ timeout: 5_000 });

  const modelSelect = dialog.locator("#tracker-model");
  const modelValues = await modelSelect
    .locator("option")
    .evaluateAll((options) =>
      options
        .filter((option) => !(option as HTMLOptionElement).disabled)
        .map((option) => (option as HTMLOptionElement).value),
    );
  const seedModel = ["sam3_video_interactive", "sam2_video"].find((value) =>
    modelValues.includes(value),
  );
  if (!seedModel) throw new Error(`[video-multi-seed:${variant}] 没有可用的交互式视频模型`);
  await modelSelect.selectOption(seedModel);
  await dialog.getByTestId("tracker-direction-forward").click();
  await dialog.locator("#tracker-range-preset").selectOption("30");
  await dialog.getByTestId("tracker-output-geometry").selectOption("bbox");
  await parkVideoPointer(page);
  await assertVideoTimelineVisible(page);
  await expect(dialog).toBeInViewport({ ratio: 1 });
  const stageBox = await stage.boundingBox();
  const panelBox = await dialog.boundingBox();
  expect(stageBox).toBeTruthy();
  expect(panelBox).toBeTruthy();
  expect(
    stageBox!.x + stageBox!.width <= panelBox!.x + 1 ||
      panelBox!.x + panelBox!.width <= stageBox!.x + 1,
    "Docked tracker panel must not overlap the video targets",
  ).toBe(true);

  const leftBusFrameZero = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0);
  const leftBusFrameFour = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f4", 4);
  const rightBusFrameZero = recordingAnchor(catalog, "video_demo", "tracking", "right_bus_f0", 0);
  const rightBusFrameFour = recordingAnchor(catalog, "video_demo", "tracking", "right_bus_f4", 4);
  const targets = [
    { frameZero: leftBusFrameZero, frameFour: leftBusFrameFour },
    { frameZero: rightBusFrameZero, frameFour: rightBusFrameFour },
  ];
  if (targets.some((target) => !target.frameFour.negative_point)) {
    throw new Error(`[video-multi-seed:${variant}] 双目标 F4 锚点缺少负点`);
  }
  if (targets.some((target) => target.frameZero.label !== leftBusFrameZero.label)) {
    throw new Error(`[video-multi-seed:${variant}] 同次多目标追踪不能混用不同类别`);
  }
  await dialog.getByTestId("tracker-target-class").selectOption(leftBusFrameZero.label);

  const toggle = page.getByTestId("tracker-seed-toggle");
  const media = await renderedMediaBounds(stage);
  if (variant === "box-seed") {
    await page.getByTestId("tracker-seed-mode-box").click();
  }
  for (const [index, target] of targets.entries()) {
    const targetId = index + 1;
    if (targetId > 1) {
      await dialog.getByTestId("tracker-seed-new-target").click();
    }

    await toggle.click();
    if (variant === "box-seed") {
      const targetBox = mediaBbox(media, target.frameZero.bbox);
      await page.mouse.move(targetBox.start.x, targetBox.start.y);
      await page.mouse.down();
      await movePointerAtRefreshRate(page, targetBox.start, targetBox.end, 700);
      await page.mouse.up();
      await page.waitForTimeout(650);
    } else {
      for (const normalizedPoint of [
        target.frameZero.point,
        ...target.frameZero.additional_points,
      ]) {
        const point = mediaPoint(media, normalizedPoint);
        await page.mouse.click(point.x, point.y);
        await page.waitForTimeout(450);
      }
    }
    await toggle.click();

    const targetSummary = page.getByTestId(`tracker-seed-target-${targetId}`);
    if (variant === "box-seed") {
      await targetSummary
        .filter({ hasText: "1 框" })
        .filter({ hasText: "F0" })
        .waitFor({ timeout: 3_000 });
      continue;
    }

    await moveToFrame(page, 4);
    await toggle.click();
    for (const normalizedPoint of [target.frameFour.point, ...target.frameFour.additional_points]) {
      const positive = mediaPoint(media, normalizedPoint);
      await page.mouse.click(positive.x, positive.y);
      await page.waitForTimeout(450);
    }

    if (variant === "positive-negative") {
      const negative = mediaPoint(media, target.frameFour.negative_point!);
      await page.keyboard.down("Alt");
      await page.mouse.click(negative.x, negative.y);
      await page.keyboard.up("Alt");
      await parkVideoPointer(page);
      await page.waitForTimeout(900);
    }

    await toggle.click();
    const positivePointCount =
      2 + target.frameZero.additional_points.length + target.frameFour.additional_points.length;
    await targetSummary
      .filter({ hasText: "F0、F4" })
      .filter({ hasText: `${positivePointCount + (variant === "positive-negative" ? 1 : 0)} 点` })
      .waitFor({ timeout: 3_000 });
    await moveToFrame(page, 0);
  }
  await page.waitForTimeout(900);

  const serverErrors: string[] = [];
  const collectServerError = (response: Response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  };
  page.on("response", collectServerError);
  try {
    const created = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/tasks/${project.tasks.tracking.id}/video:track`) &&
        response.status() === 202,
    );
    await dialog.getByRole("button", { name: "开始发现" }).click();
    const createdResponse = await created;
    const job = (await createdResponse.json()) as VideoTrackerJob;
    if (!job.id) throw new Error("Video tracker did not return a job ID");
    onJobCreated?.(job.id);
    const submitted = createdResponse.request().postDataJSON() as VideoTrackerPropagatePayload;
    expect(submitted.model_key).toBe(seedModel);
    expect(submitted.direction).toBe("forward");
    expect(submitted.from_frame).toBe(0);
    expect(submitted.to_frame).toBe(30);
    expect(submitted.output_geometry).toBe("bbox");
    expect(submitted.target_class_name).toBe(leftBusFrameZero.label);
    expect(submitted.source_annotation_id ?? null).toBeNull();
    expect(submitted.source_annotation_ids ?? []).toEqual([]);
    const seeds = submitted.prompt?.seeds as SubmittedSeed[];
    expect(seeds).toHaveLength(2);
    expect(new Set(seeds.map((seed) => seed.obj_id)).size).toBe(2);
    for (const [index, target] of targets.entries()) {
      const seed = seeds.find((item) => item.obj_id === index + 1);
      if (!seed) throw new Error(`Missing submitted seed target ${index + 1}`);
      expect(seed.prompts.map((prompt) => prompt.frame_index)).toEqual(
        variant === "box-seed" ? [0] : [0, 4],
      );
      for (const prompt of seed.prompts) {
        const anchor = prompt.frame_index === 0 ? target.frameZero : target.frameFour;
        if (variant === "box-seed") {
          expect(prompt.bbox).toBeTruthy();
          const expected = {
            x: anchor.bbox[0],
            y: anchor.bbox[1],
            w: anchor.bbox[2] - anchor.bbox[0],
            h: anchor.bbox[3] - anchor.bbox[1],
          };
          expect(normalizedBboxIoU(prompt.bbox!, expected)).toBeGreaterThan(0.95);
          expect(prompt.points ?? []).toEqual([]);
        } else {
          const positives = [anchor.point, ...anchor.additional_points];
          const negatives =
            variant === "positive-negative" && prompt.frame_index === 4
              ? [anchor.negative_point!]
              : [];
          const expectedPoints = [
            ...positives.map(([x, y]) => [x, y, 1]),
            ...negatives.map(([x, y]) => [x, y, 0]),
          ];
          expect(prompt.points).toHaveLength(expectedPoints.length);
          for (const [pointIndex, point] of expectedPoints.entries()) {
            const actual = prompt.points![pointIndex]!;
            expect(actual[0]).toBeCloseTo(point[0]!, 2);
            expect(actual[1]).toBeCloseTo(point[1]!, 2);
            expect(actual[2]).toBe(point[2]);
          }
          expect(prompt.bbox).toBeUndefined();
        }
      }
    }
    await page.getByTestId("video-tool-btn-select").click();
    await page
      .getByTestId("tracker-review-tool-capsule")
      .waitFor({ state: "visible", timeout: 120_000 });
    await openContextToolbar(page, "tracker-review");
    const review = page.getByTestId("video-tracker-review-bar");
    await review.waitFor({ state: "visible", timeout: 120_000 });
    const preview = await readVideoRecordingJson<VideoTrackerJobPreview>(
      page,
      `/api/v1/video-tracker-jobs/${job.id}/preview`,
    );
    expect(preview.job_id).toBe(job.id);
    expect(preview.status).toBe("pending_review");
    expect(preview.output_geometry).toBe("bbox");
    expect(preview.results.length).toBeGreaterThan(0);
    expect(new Set(preview.results.map((result) => result.instance_id)).size).toBe(2);
    for (const result of preview.results) {
      expect(result.frame_index).toBeGreaterThanOrEqual(0);
      expect(result.frame_index).toBeLessThanOrEqual(30);
      expect(result.geometry.type).toBe("bbox");
    }
    await page.waitForTimeout(1_200);
    await scrubPendingTrackerFrames(page);
    await openContextToolbar(page, "tracker-review");
    const accepted = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/video-tracker-jobs/${job.id}/decisions`) &&
        response.ok(),
      { timeout: 20_000 },
    );
    const annotationsRefreshed = page
      .waitForResponse(
        (response) =>
          response.request().method() === "GET" &&
          response.url().endsWith(`/tasks/${project.tasks.tracking.id}/annotations`) &&
          response.ok(),
        { timeout: 20_000 },
      )
      .then(async (response) => {
        const saved = (await response.json()) as RecordedVideoTrack[];
        const added = saved.filter((annotation) => !baselineIds.has(annotation.id));
        onAnnotationsCreated?.(added.map((annotation) => annotation.id));
        return added;
      });
    await review.getByTestId("tracker-review-accept").click();
    const [decisionResponse, added] = await Promise.all([accepted, annotationsRefreshed]);
    expect(added).toHaveLength(2);
    for (const annotation of added) {
      expect(annotation.task_id).toBe(project.tasks.tracking.id);
      expect(annotation.geometry.type).toBe("video_track_bbox");
      expect(annotation.geometry.track_id).toBeTruthy();
      expect(annotation.version).toBeGreaterThanOrEqual(1);
    }
    expect(new Set(added.map((annotation) => annotation.geometry.track_id)).size).toBe(2);
    assertAcceptedTracksCoverTargets(
      added,
      leftBusFrameZero.label,
      targets.map((target) => target.frameZero.bbox),
      variant,
    );
    await review.waitFor({ state: "hidden", timeout: 5_000 });
    await parkVideoPointer(page);
    await assertVideoTimelineVisible(page);
    await page.waitForTimeout(1_200);
    const drawEndMs = Date.now();
    const finalJob = await readVideoRecordingJson<VideoTrackerJob>(
      page,
      `/api/v1/video-tracker-jobs/${job.id}`,
    );
    expect(finalJob.status).toBe("accepted");
    expect(finalJob.model_key).toBe(seedModel);
    expect(finalJob.error_message).toBeNull();
    await page.reload();
    await expect(stage).toBeVisible({ timeout: 20_000 });
    const reloaded = await readVideoRecordingJson<RecordedVideoTrack[]>(page, annotationsPath);
    for (const annotation of added) {
      const persisted = reloaded.find((item) => item.id === annotation.id);
      expect(persisted?.version).toBe(annotation.version);
      expect(persisted?.geometry).toEqual(annotation.geometry);
    }
    expect(reloaded.filter((annotation) => !baselineIds.has(annotation.id))).toHaveLength(2);
    if (serverErrors.length > 0)
      throw new Error(`[video-multi-seed:${variant}] ${label}: ${serverErrors.join(", ")}`);
    return {
      drawStartMs,
      drawEndMs,
      evidence: {
        variant,
        submitted,
        created_job: job,
        preview,
        final_job: finalJob,
        decision: await decisionResponse.json(),
        accepted_annotations: added,
        reload_verified: true,
      },
    };
  } finally {
    page.off("response", collectServerError);
  }
}
