/** One saved bus track carries its selected F0 ghost into an aligned F1 keyframe. */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotRecordingAnchor, ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  mediaBbox,
  mediaPoint,
  movePointerAtRefreshRate,
  recordingAnchor,
  renderedMediaBounds,
  selectVideoRecordingClass,
  commitPendingAnnotationClass,
} from "./_canvas";
import {
  anchorVideoBbox,
  assertVideoBboxNear,
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

async function drawTrack(
  page: Page,
  anchor: ScreenshotRecordingAnchor,
  taskId: string,
  onCreated: VideoTrackCreated,
) {
  const stage = page.getByTestId("video-konva-stage");
  await selectVideoRecordingClass(page, stage, anchor.label);
  const { start, end } = mediaBbox(await renderedMediaBounds(stage), anchor.bbox);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, start, end, 650);
  await page.mouse.up();
  const created = inspectVideoTrack(
    await commitPendingAnnotationClass(page, {
      label: anchor.label,
      taskId,
      onCreated: (id, annotation) => registerVideoTrack(id, annotation, onCreated),
    }),
    taskId,
    anchor.label,
    [0],
  );
  assertVideoBboxNear(created.geometry.keyframes[0].bbox, anchorVideoBbox(anchor.bbox));
  await collapseVideoSelectionCard(page);
  await holdVideoRecording(page, 650);
  return created;
}

export async function runVideoTrackCarryover(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: VideoTrackCreated,
): Promise<VideoKeyframeRecordingWindow> {
  const { taskId, manifest, stage } = await openVideoKeyframeRecording(page, catalog);
  const trackButton = page.getByTestId("video-tool-btn-track");
  const busStart = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0);
  const busNext = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f1", 1);
  if (busStart.label !== "bus" || busNext.label !== busStart.label) {
    throw new Error("[video-track-carryover] 续写锚点必须是同一辆公交车");
  }
  await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
  await trackButton.click();
  const drawStartMs = Date.now();

  // One annotation owns both frames: the F1 ghost must be the same bus that was saved at F0.
  const bus = await drawTrack(page, busStart, taskId, onCreated);
  const row = page
    .getByTestId("video-track-row")
    .filter({ hasText: bus.geometry.track_id.slice(0, 8) });
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  await expect(row).toHaveAttribute("aria-selected", "true");
  await waitForVideoRecordingShape(page, manifest, bus.geometry.keyframes[0].bbox, {
    dashed: false,
  });
  await holdVideoRecording(page, 900);

  await page.keyboard.press("ArrowRight");
  await waitForVideoRecordingFrame(page, manifest, 1);
  await expect(row).toHaveAttribute("aria-selected", "true");
  const hint = page.getByTestId("video-sticky-track-hint");
  await expect(hint).toContainText(busStart.label);
  await expect(hint).toContainText("画框延展到本帧");
  await waitForVideoRecordingShape(page, manifest, bus.geometry.keyframes[0].bbox, {
    ghost: true,
  });
  await holdVideoRecording(page, 1_200);

  const [dragFrom, dragTo] = busNext.polyline;
  if (!dragFrom || !dragTo)
    throw new Error("[video-track-carryover] left_bus_f1 缺少续写拖动路径锚点");
  const bounds = await renderedMediaBounds(stage);
  const start = mediaPoint(bounds, dragFrom);
  const end = mediaPoint(bounds, dragTo);
  const expected = {
    ...bus.geometry.keyframes[0].bbox,
    x: bus.geometry.keyframes[0].bbox.x + dragTo[0] - dragFrom[0],
    y: bus.geometry.keyframes[0].bbox.y + dragTo[1] - dragFrom[1],
  };
  const pending = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname === `/api/v1/tasks/${taskId}/annotations/${bus.id}`,
    { timeout: 20_000 },
  );
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, start, end, 700);
  await page.mouse.up();
  const response = await pending;
  expect(response.ok(), `F1 carryover save HTTP ${response.status()}`).toBeTruthy();
  const updatedBus = verifyVideoKeyframeUpdate(bus, await response.json(), 1, expected);
  expect(updatedBus.id).toBe(bus.id);
  expect(updatedBus.geometry.track_id).toBe(bus.geometry.track_id);
  const updatedBox = updatedBus.geometry.keyframes[1].bbox;
  const target = anchorVideoBbox(busNext.bbox);
  const intersection =
    Math.max(
      0,
      Math.min(updatedBox.x + updatedBox.w, target.x + target.w) - Math.max(updatedBox.x, target.x),
    ) *
    Math.max(
      0,
      Math.min(updatedBox.y + updatedBox.h, target.y + target.h) - Math.max(updatedBox.y, target.y),
    );
  const targetIou =
    intersection / (updatedBox.w * updatedBox.h + target.w * target.h - intersection);
  expect(
    targetIou,
    "The continued keyframe must remain aligned with the same bus at F1",
  ).toBeGreaterThanOrEqual(0.75);
  await expect(page.getByTestId("class-picker-popover")).toHaveCount(0);
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  await expect(row).toContainText("2 关键帧");
  await expect(row).toHaveAttribute("aria-selected", "true");
  await expect(hint).toContainText(busStart.label);
  await waitForVideoRecordingShape(page, manifest, updatedBox, { dashed: false });
  await collapseVideoSelectionCard(page);
  await setVideoRecordingTimeline(page, true);
  await holdVideoRecording(page, 1_500);

  const verifyStored = async () => {
    const saved = await readSavedVideoTracks(page, taskId);
    expect(saved.map((annotation) => annotation.id)).toEqual([bus.id]);
    expect(saved[0]!.geometry).toEqual(updatedBus.geometry);
  };
  await verifyStored();
  await page.reload();
  await expect(stage).toBeVisible();
  const source = page.getByTestId("video-konva-source");
  await expect
    .poll(() => source.evaluate((video: HTMLVideoElement) => video.readyState))
    .toBeGreaterThanOrEqual(2);
  if (!(await source.evaluate((video: HTMLVideoElement) => video.paused))) {
    await page.getByRole("button", { name: "播放 / 暂停", exact: true }).click();
  }
  await expect(source).toHaveJSProperty("paused", true);
  await setVideoRecordingTimeline(page, true);
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  const reloadedRow = page
    .getByTestId("video-track-row")
    .filter({ hasText: bus.geometry.track_id.slice(0, 8) });
  await expect(reloadedRow).toContainText("2 关键帧");
  await reloadedRow.click();
  await expect(reloadedRow).toHaveAttribute("aria-selected", "true");
  await seekVideoRecordingFrame(page, manifest, 1);
  await waitForVideoRecordingShape(page, manifest, updatedBox, { dashed: false });
  const reloaded = await readSavedVideoTracks(page, taskId);
  expect(reloaded.map((annotation) => annotation.id)).toEqual([bus.id]);
  expect(inspectVideoTrack(reloaded[0]!, taskId, busStart.label, [0, 1]).geometry).toEqual(
    updatedBus.geometry,
  );
  await expect(stage).toHaveAttribute("data-video-frame-index", "1");
  await holdVideoRecording(page, 2_000);
  const drawEndMs = Date.now();
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
      initial_track: bus,
      selected_track_annotation_id: bus.id,
      updated_track: updatedBus,
      continued_frame: 1,
      same_annotation_identity: updatedBus.id === bus.id,
      same_track_identity: updatedBus.geometry.track_id === bus.geometry.track_id,
      target_iou: targetIou,
      reload_verified: true,
    },
  };
}
