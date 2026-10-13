/** Two real tracks at F0; Tab selects the bus reference at F1 and a drag adds only its keyframe. */
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
  preselectClass = true,
) {
  const stage = page.getByTestId("video-konva-stage");
  if (preselectClass) await selectVideoRecordingClass(page, stage, anchor.label);
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
  const leftBus = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f0", 0);
  const frontTruck = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
  const nextFrame = recordingAnchor(catalog, "video_demo", "tracking", "left_bus_f1", 1);
  expect(nextFrame.label).toBe(leftBus.label);
  await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
  await trackButton.click();
  const drawStartMs = Date.now();
  const bus = await drawTrack(page, leftBus, taskId, onCreated);
  // The same-frame track tool creates another object. Use its required class picker;
  // a numeric shortcut here would relabel the selected bus instead of the new truck.
  await expect(page.getByTestId("video-sticky-track-hint")).toContainText("本帧已有关键帧");
  const truck = await drawTrack(page, frontTruck, taskId, onCreated, false);
  expect(bus.id).not.toBe(truck.id);
  expect(bus.geometry.track_id).not.toBe(truck.geometry.track_id);
  const row = (trackId: string) =>
    page.getByTestId("video-track-row").filter({ hasText: trackId.slice(0, 8) });
  await expect(page.getByTestId("video-track-row")).toHaveCount(2);
  await expect(row(truck.geometry.track_id)).toHaveAttribute("aria-selected", "true");
  await waitForVideoRecordingShape(page, manifest, bus.geometry.keyframes[0].bbox, {
    dashed: false,
  });
  await waitForVideoRecordingShape(page, manifest, truck.geometry.keyframes[0].bbox, {
    dashed: false,
  });
  await holdVideoRecording(page, 900);

  await page.keyboard.press("ArrowRight");
  await waitForVideoRecordingFrame(page, manifest, 1);
  const hint = page.getByTestId("video-sticky-track-hint");
  await expect(hint).toContainText(frontTruck.label);
  await expect(hint).toContainText("画框延展到本帧");
  await waitForVideoRecordingShape(page, manifest, truck.geometry.keyframes[0].bbox, {
    ghost: true,
  });
  await holdVideoRecording(page, 900);

  // Switch through the real track-navigation hotkey, without clicking a row that would seek back to F0.
  await page.keyboard.press("Tab");
  await expect(row(bus.geometry.track_id)).toHaveAttribute("aria-selected", "true");
  await expect(row(truck.geometry.track_id)).toHaveAttribute("aria-selected", "false");
  await expect(hint).toContainText(leftBus.label);
  await expect(hint).toContainText("画框延展到本帧");
  await waitForVideoRecordingFrame(page, manifest, 1);
  await waitForVideoRecordingShape(page, manifest, bus.geometry.keyframes[0].bbox, { ghost: true });
  await collapseVideoSelectionCard(page);
  await holdVideoRecording(page, 650);

  const [dragFrom, dragTo] = nextFrame.polyline;
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
  const updatedBox = updatedBus.geometry.keyframes[1].bbox;
  const target = anchorVideoBbox(nextFrame.bbox);
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
    "The manual correction must remain aligned with the F1 bus anchor",
  ).toBeGreaterThanOrEqual(0.75);
  await expect(page.getByTestId("class-picker-popover")).toHaveCount(0);
  await expect(row(bus.geometry.track_id)).toContainText("2 关键帧");
  await expect(row(truck.geometry.track_id)).toContainText("1 关键帧");
  await expect(row(truck.geometry.track_id)).toHaveAttribute("aria-selected", "true");
  await expect(hint).toContainText(frontTruck.label);
  await waitForVideoRecordingShape(page, manifest, updatedBox, { dashed: false });
  await waitForVideoRecordingShape(page, manifest, truck.geometry.keyframes[0].bbox, {
    ghost: true,
  });
  await collapseVideoSelectionCard(page);
  await setVideoRecordingTimeline(page, true);
  await holdVideoRecording(page, 2_000);
  const drawEndMs = Date.now();
  const verifyStored = async () => {
    const saved = await readSavedVideoTracks(page, taskId);
    expect(saved.map((annotation) => annotation.id).sort()).toEqual([bus.id, truck.id].sort());
    expect(saved.find((annotation) => annotation.id === bus.id)?.geometry).toEqual(
      updatedBus.geometry,
    );
    expect(saved.find((annotation) => annotation.id === truck.id)?.geometry).toEqual(
      truck.geometry,
    );
  };
  await verifyStored();
  await page.reload();
  await expect(page.getByTestId("video-track-row")).toHaveCount(2);
  await verifyStored();
  await seekVideoRecordingFrame(page, manifest, 1);
  await waitForVideoRecordingShape(page, manifest, updatedBox, { dashed: false });
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
      initial_tracks: [bus, truck],
      tab_selected_annotation_id: bus.id,
      updated_track: updatedBus,
      unchanged_track: truck,
      continued_frame: 1,
      target_iou: targetIou,
      next_selected_annotation_id: truck.id,
      reload_verified: true,
    },
  };
}
