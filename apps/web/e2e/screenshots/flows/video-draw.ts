/** Manual F0/F8 keyframes on one truck track, with visible interpolation and persisted readback. */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  mediaBbox,
  movePointerAtRefreshRate,
  recordingAnchor,
  renderedMediaBounds,
  selectVideoRecordingClass,
  commitPendingAnnotationClass,
} from "./_canvas";
import {
  anchorVideoBbox,
  assertVideoBboxNear,
  expectedVideoInterpolation,
  inspectVideoTrack,
  registerVideoTrack,
  verifyVideoKeyframeUpdate,
  type VideoTrackCreated,
} from "./_video-keyframe-evidence";
import {
  collapseVideoSelectionCard,
  holdVideoRecording,
  openVideoKeyframeRecording,
  readSavedVideoTracks,
  seekVideoRecordingFrame,
  setVideoRecordingTimeline,
  waitForVideoRecordingFrame,
  waitForVideoRecordingShape,
  type VideoKeyframeRecordingWindow,
} from "./_video-keyframe-recording";

export async function runVideoDraw(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: VideoTrackCreated,
): Promise<VideoKeyframeRecordingWindow> {
  const { taskId, manifest, stage } = await openVideoKeyframeRecording(page, catalog);
  const firstAnchor = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
  const secondAnchor = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f8", 8);
  expect(secondAnchor.label).toBe(firstAnchor.label);
  await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
  await page.getByTestId("video-tool-btn-track").click();
  await selectVideoRecordingClass(page, stage, firstAnchor.label);
  const drawStartMs = Date.now();
  await holdVideoRecording(page, 900);
  const first = mediaBbox(await renderedMediaBounds(stage), firstAnchor.bbox);
  await page.mouse.move(first.start.x, first.start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, first.start, first.end, 700);
  await page.mouse.up();
  const initial = inspectVideoTrack(
    await commitPendingAnnotationClass(page, {
      label: firstAnchor.label,
      taskId,
      onCreated: (id, annotation) => registerVideoTrack(id, annotation, onCreated),
    }),
    taskId,
    firstAnchor.label,
    [0],
  );
  assertVideoBboxNear(initial.geometry.keyframes[0].bbox, anchorVideoBbox(firstAnchor.bbox));
  const row = page
    .getByTestId("video-track-row")
    .filter({ hasText: initial.geometry.track_id.slice(0, 8) });
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  await expect(row).toHaveAttribute("aria-selected", "true");
  await collapseVideoSelectionCard(page);
  await waitForVideoRecordingShape(page, manifest, initial.geometry.keyframes[0].bbox, {
    dashed: false,
  });
  await holdVideoRecording(page, 1_000);

  await seekVideoRecordingFrame(page, manifest, 8);
  await expect(row).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("video-sticky-track-hint")).toContainText("画框延展到本帧");
  await holdVideoRecording(page, 700);
  const second = mediaBbox(await renderedMediaBounds(stage), secondAnchor.bbox);
  const pending = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname === `/api/v1/tasks/${taskId}/annotations/${initial.id}`,
    { timeout: 20_000 },
  );
  // Reverse the drag to start outside the held F0 reference, preserving the intended F8 bbox.
  await page.mouse.move(second.end.x, second.end.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, second.end, second.start, 700);
  await page.mouse.up();
  const response = await pending;
  expect(response.ok(), `F8 keyframe save HTTP ${response.status()}`).toBeTruthy();
  const updated = verifyVideoKeyframeUpdate(
    initial,
    await response.json(),
    8,
    anchorVideoBbox(secondAnchor.bbox),
  );
  await expect(page.getByTestId("class-picker-popover")).toHaveCount(0);
  await expect(row).toContainText("2 关键帧");
  await waitForVideoRecordingShape(page, manifest, updated.geometry.keyframes[1].bbox, {
    dashed: false,
  });
  await collapseVideoSelectionCard(page);
  await holdVideoRecording(page, 1_000);

  // Arrow keys demonstrate both directions; F4 must be rendered interpolation, not a saved keyframe.
  await setVideoRecordingTimeline(page, true);
  await seekVideoRecordingFrame(page, manifest, 0);
  const interpolated = expectedVideoInterpolation(updated, 4);
  for (let frame = 1; frame <= 8; frame += 1) {
    await page.keyboard.press("ArrowRight");
    await waitForVideoRecordingFrame(page, manifest, frame);
    if (frame === 4)
      await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });
    await page.waitForTimeout(260);
  }
  for (let frame = 7; frame >= 4; frame -= 1) {
    await page.keyboard.press("ArrowLeft");
    await waitForVideoRecordingFrame(page, manifest, frame);
    await page.waitForTimeout(260);
  }
  await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });
  for (let frame = 5; frame <= 8; frame += 1) {
    await page.keyboard.press("ArrowRight");
    await waitForVideoRecordingFrame(page, manifest, frame);
    await page.waitForTimeout(260);
  }
  await waitForVideoRecordingShape(page, manifest, updated.geometry.keyframes[1].bbox, {
    dashed: false,
  });
  await holdVideoRecording(page, 1_800);
  const drawEndMs = Date.now();
  const saved = await readSavedVideoTracks(page, taskId);
  expect(saved.map((annotation) => annotation.id)).toEqual([initial.id]);
  expect(saved[0].geometry).toEqual(updated.geometry);
  await page.reload();
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  const reloaded = await readSavedVideoTracks(page, taskId);
  expect(reloaded.map((annotation) => annotation.id)).toEqual([initial.id]);
  expect(inspectVideoTrack(reloaded[0], taskId, firstAnchor.label, [0, 8]).geometry).toEqual(
    updated.geometry,
  );
  await seekVideoRecordingFrame(page, manifest, 4);
  await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });
  return {
    drawStartMs,
    drawEndMs,
    evidence: {
      task_id: taskId,
      media: {
        width: manifest.metadata.width,
        height: manifest.metadata.height,
        fps: manifest.metadata.fps,
        frame_count: manifest.metadata.frame_count,
      },
      annotation: updated,
      interpolation: {
        frame_index: 4,
        bbox: interpolated,
        persisted_keyframes: [0, 8],
        visible_dashed_geometry: true,
      },
      reload_verified: true,
    },
  };
}
