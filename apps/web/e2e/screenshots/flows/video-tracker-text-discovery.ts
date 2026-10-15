import { openContextToolbar } from "../../fixtures/context-toolbar";
/** Discover the two anchored buses with real text inference and verify persisted tracks. */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type {
  VideoTrackerJob,
  VideoTrackerJobPreview,
  VideoTrackerDecisionPayload,
} from "../../../src/api/videoTracker";
import { normalizedBboxIoU, recordingAnchor, type NormalizedBbox } from "./_canvas";
import {
  openVideoTimeline,
  currentVideoFrame,
  parkVideoPointer,
  assertVideoTimelineVisible,
} from "./_video-timeline";
import type { DrawWindow } from "./rotated-bbox";

type SavedTrack = {
  id: string;
  class_name: string;
  version: number;
  geometry: {
    type: string;
    track_id?: string;
    keyframes?: Array<{ frame_index: number; bbox?: NormalizedBbox }>;
  };
};

function matchTargets<T>(
  items: T[],
  targets: NormalizedBbox[],
  bbox: (item: T) => NormalizedBbox | undefined,
) {
  // Exhaustive one-to-one assignment avoids a greedy match consuming the other bus.
  let best: { items: T[]; overlaps: number[]; score: number } | undefined;
  function visit(selected: T[], overlaps: number[], remaining: T[]) {
    if (selected.length === targets.length) {
      const score = overlaps.reduce((sum, value) => sum + value, 0);
      if (!best || score > best.score) best = { items: selected, overlaps, score };
      return;
    }
    for (const item of remaining) {
      const actual = bbox(item);
      const overlap = actual ? normalizedBboxIoU(actual, targets[selected.length]!) : 0;
      if (overlap >= 0.6)
        visit(
          [...selected, item],
          [...overlaps, overlap],
          remaining.filter((other) => other !== item),
        );
    }
  }
  visit([], [], items);
  if (!best)
    throw new Error(
      "[video-tracker-text-discovery] Cannot match both bus anchors one-to-one at IoU >= 0.6",
    );
  return best;
}

function verifySaved(
  tracks: SavedTrack[],
  targets: NormalizedBbox[],
  className: string,
  from: number,
  to: number,
) {
  expect(tracks).toHaveLength(targets.length);
  for (const track of tracks) {
    expect(track.class_name).toBe(className);
    expect(track.version).toBeGreaterThan(0);
    expect(track.geometry.type).toBe("video_track_bbox");
    expect(track.geometry.track_id).toBeTruthy();
    const frames = new Set(
      track.geometry.keyframes
        ?.filter((keyframe) => keyframe.bbox)
        .map((keyframe) => keyframe.frame_index),
    );
    for (let frame = from; frame <= to; frame += 1)
      expect(frames.has(frame), `Saved ${track.id} missing F${frame}`).toBe(true);
  }
  expect(new Set(tracks.map((track) => track.geometry.track_id)).size).toBe(targets.length);
  return matchTargets(
    tracks,
    targets,
    (track) => track.geometry.keyframes?.find((frame) => frame.frame_index === 0)?.bbox,
  );
}

export async function runVideoTrackerTextDiscovery(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onJobCreated?: (jobId: string) => void,
  onAnnotationsCreated?: (ids: string[]) => void,
): Promise<DrawWindow & { evidence: object }> {
  const taskId = catalog.projects.video_demo.tasks.tracking.id;
  const annotationsPath = `/tasks/${taskId}/annotations`;
  const annotationsResponse = () =>
    page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname.endsWith(annotationsPath) &&
        response.ok(),
      { timeout: 30_000 },
    );
  const baselineResponse = annotationsResponse();
  await openVideoTimeline(page, catalog);
  const baseline = (await (await baselineResponse).json()) as SavedTrack[];
  const baselineIds = new Set(baseline.map((item) => item.id));
  const anchors = [
    recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0),
    recordingAnchor(catalog, "video_demo", "tracking", "right_bus_f0", 0),
  ];
  expect(anchors.map((anchor) => anchor.label)).toEqual(["bus", "bus"]);
  const targets = anchors.map(({ bbox: [x, y, right, bottom] }) => ({
    x,
    y,
    w: right - x,
    h: bottom - y,
  }));
  const drawStartMs = Date.now();
  await page.getByTestId("workbench-ai-tracker").click();
  const dialog = page.getByTestId("video-tracker-propagate-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toBeInViewport({ ratio: 1 });
  await parkVideoPointer(page);
  await assertVideoTimelineVisible(page);
  const model = dialog.locator("#tracker-model");
  await expect(model).toBeEnabled();
  await expect(model.locator('option[value="sam3_video"]')).toHaveCount(1);
  expect(
    await model
      .locator('option[value="sam3_video"]')
      .evaluate((option: HTMLOptionElement) => option.disabled),
  ).toBe(false);
  await model.selectOption("sam3_video");
  await dialog.getByTestId("tracker-direction-forward").click();
  await dialog.locator("#tracker-range-preset").selectOption("10");
  await dialog.getByTestId("tracker-target-class").selectOption("bus");
  await dialog.getByTestId("tracker-output-geometry").selectOption("bbox");
  await dialog.getByTestId("tracker-text-input").fill("bus");
  await page.waitForTimeout(1_200);

  // Register cleanup as soon as the real job is created, even if subsequent assertions fail.
  const created = page
    .waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith(`/tasks/${taskId}/video:track`),
      { timeout: 30_000 },
    )
    .then(async (response) => {
      expect(response.ok()).toBe(true);
      const job = (await response.json()) as VideoTrackerJob;
      onJobCreated?.(job.id);
      return { job, request: response.request().postDataJSON() as Record<string, unknown> };
    });
  const previewResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      /\/video-tracker-jobs\/[^/]+\/preview$/.test(new URL(response.url()).pathname) &&
      response.ok(),
    { timeout: 180_000 },
  );
  await dialog.getByRole("button", { name: "开始发现", exact: true }).click();
  const { job, request } = await created;
  expect(request.model_key).toBe("sam3_video");
  expect(request.text).toBe("bus");
  expect(request.output_geometry).toBe("bbox");
  expect(request.from_frame).toBe(0);
  expect(request.to_frame).toBe(10);
  const preview = (await (await previewResponse).json()) as VideoTrackerJobPreview;
  expect(preview.job_id).toBe(job.id);
  expect(preview.output_geometry).toBe("bbox");
  const frameZero = preview.results.filter(
    (result) => result.frame_index === 0 && !result.outside && result.geometry.type === "bbox",
  );
  const match = matchTargets(frameZero, targets, (result) =>
    result.geometry.type === "bbox" ? result.geometry : undefined,
  );
  const selectedIds = match.items.map((result) => result.instance_id ?? "1");
  expect(new Set(selectedIds).size).toBe(2);
  await page.getByTestId("video-tool-btn-select").click();
  await page
    .getByTestId("tracker-review-tool-capsule")
    .waitFor({ state: "visible", timeout: 120_000 });
  await openContextToolbar(page, "tracker-review");
  const review = page.getByTestId("video-tracker-review-bar");
  await expect(review).toBeVisible({ timeout: 10_000 });
  await expect(review).toBeInViewport({ ratio: 1 });
  const instances = [...new Set(preview.results.map((result) => result.instance_id ?? "1"))];
  for (const instanceId of instances)
    await review
      .getByTestId(`tracker-review-instance-${instanceId}`)
      .setChecked(selectedIds.includes(instanceId));
  await review.getByTestId("tracker-review-from-frame").fill("0");
  await review.getByTestId("tracker-review-to-frame").fill("10");
  // Editing the review bar leaves the pointer outside the stage and hides playback controls.
  const parkBelowReview = async () => {
    const bounds = await page.getByTestId("video-konva-stage").boundingBox();
    if (!bounds) throw new Error("Text discovery video stage is not visible");
    // The review card covers the usual top-edge parking point.
    await page.mouse.move(bounds.x + bounds.width * 0.5, bounds.y + bounds.height * 0.65);
  };
  await parkBelowReview();
  await assertVideoTimelineVisible(page);
  // Native frame controls keep the timeline geometry and actual decoded frame in agreement.
  for (let frame = 1; frame <= 9; frame += 1) {
    await page.getByRole("button", { name: "下一帧", exact: true }).click();
    await expect.poll(() => currentVideoFrame(page)).toBe(frame);
    await page.waitForTimeout(100);
  }
  await parkBelowReview();
  await page.waitForTimeout(900);
  for (let frame = 8; frame >= 4; frame -= 1) {
    await page.getByRole("button", { name: "上一帧", exact: true }).click();
    await expect.poll(() => currentVideoFrame(page)).toBe(frame);
  }
  await parkBelowReview();
  await assertVideoTimelineVisible(page);
  await page.waitForTimeout(900);
  const decisionResponse = () =>
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith(`/video-tracker-jobs/${job.id}/decisions`),
      { timeout: 30_000 },
    );
  const acceptedResponse = decisionResponse();
  const savedResponse = annotationsResponse().then(async (response) => {
    const all = (await response.json()) as SavedTrack[];
    const added = all.filter((item) => !baselineIds.has(item.id));
    onAnnotationsCreated?.(added.map((item) => item.id));
    return added;
  });
  await openContextToolbar(page, "tracker-review");
  await review.getByTestId("tracker-review-accept").click();
  const [accepted, saved] = await Promise.all([acceptedResponse, savedResponse]);
  expect(accepted.ok()).toBe(true);
  const decision = accepted.request().postDataJSON() as VideoTrackerDecisionPayload;
  expect(decision.decision).toBe("accept");
  expect([...(decision.instance_ids ?? [])].sort()).toEqual([...selectedIds].sort());
  expect([decision.from_frame, decision.to_frame]).toEqual([0, 10]);
  const acceptedJob = (await accepted.json()) as VideoTrackerJob;
  const savedMatch = verifySaved(saved, targets, "bus", 0, 10);
  let rejectedEvidence: { request: unknown; job: VideoTrackerJob } | null = null;
  if (acceptedJob.status === "partially_reviewed" || acceptedJob.status === "pending_review") {
    const acceptedCount = preview.results.filter(
      (result) =>
        selectedIds.includes(result.instance_id ?? "1") &&
        result.frame_index >= 0 &&
        result.frame_index <= 10,
    ).length;
    await expect(review).toContainText(`已审 ${acceptedCount}/`);
    const remaining = review.locator('input[data-testid^="tracker-review-instance-"]');
    for (let index = 0; index < (await remaining.count()); index += 1)
      await remaining.nth(index).setChecked(true);
    await expect(review.getByTestId("tracker-review-discard")).toBeEnabled();
    const rejectedResponse = decisionResponse();
    await review.getByTestId("tracker-review-discard").click();
    const rejected = await rejectedResponse;
    expect(rejected.ok()).toBe(true);
    expect(rejected.request().postDataJSON().decision).toBe("reject");
    rejectedEvidence = {
      request: rejected.request().postDataJSON(),
      job: (await rejected.json()) as VideoTrackerJob,
    };
  }
  await expect(review).toBeHidden();
  await parkVideoPointer(page);
  await page.waitForTimeout(1_500);
  const drawEndMs = Date.now();
  const reloadedResponse = annotationsResponse();
  await page.reload();
  const reloaded = (await (await reloadedResponse).json()) as SavedTrack[];
  const reloadedAdded = reloaded.filter((item) => !baselineIds.has(item.id));
  expect(reloadedAdded.map((item) => item.id).sort()).toEqual(saved.map((item) => item.id).sort());
  verifySaved(reloadedAdded, targets, "bus", 0, 10);
  expect(
    reloadedAdded
      .map((item) => ({ id: item.id, version: item.version, geometry: item.geometry }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  ).toEqual(
    saved
      .map((item) => ({ id: item.id, version: item.version, geometry: item.geometry }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  );
  return {
    drawStartMs,
    drawEndMs,
    evidence: {
      model: "sam3_video",
      job,
      request,
      preview,
      selectedIds,
      previewAnchorIoUs: match.overlaps,
      decision,
      rejected: rejectedEvidence,
      acceptedAnnotationIds: saved.map((item) => item.id),
      acceptedTracks: saved,
      savedAnchorIoUs: savedMatch.overlaps,
      reload: { verified: true, tracks: reloadedAdded },
    },
  };
}
