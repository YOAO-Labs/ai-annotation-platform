/**
 * 流程录制：视频时序追踪工作台(video-track)概览 —— 逐帧前进 + 播放。
 *
 * 输出：outputs/flows/video-track.gif → docs-site/.../workbench/video-track-overview.gif
 *
 * 数据来自 screenshot catalog 的 video_demo（固定公开行车视频，H.264，72 帧）。
 * 本 flow 先在 F0 建立整车轨迹并手动延展到 F8，再演示双向逐帧、播放暂停与刷新持久化。
 * 调用者记录创建结果并在 finally 删除，仅修改隔离截图数据。
 *
 * 返回 { drawStartMs, drawEndMs }：供 finalize 裁掉开头(导航/解析/就绪等待)。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
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
} from "./_video-keyframe-recording";
export async function runVideoTrack(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (annotationId: string) => void,
): Promise<DrawWindow> {
  const { taskId, manifest, stage } = await openVideoKeyframeRecording(page, catalog);
  const timeline = page.getByTestId("video-timeline-shell");
  const source = page.getByTestId("video-konva-source");
  await expect(timeline).toBeVisible({ timeout: 15_000 });
  await expect(stage).toBeVisible();
  await expect
    .poll(() => source.evaluate((video: HTMLVideoElement) => video.readyState))
    .toBeGreaterThanOrEqual(2);

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
  assertVideoBboxNear(initial.geometry.keyframes[0]!.bbox, anchorVideoBbox(firstAnchor.bbox));
  const row = page
    .getByTestId("video-track-row")
    .filter({ hasText: initial.geometry.track_id.slice(0, 8) });
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  await expect(row).toHaveAttribute("aria-selected", "true");
  await collapseVideoSelectionCard(page);
  await waitForVideoRecordingShape(page, manifest, initial.geometry.keyframes[0]!.bbox, {
    dashed: false,
  });
  await holdVideoRecording(page, 1_000);

  // Save a real second keyframe from the catalog anchor before demonstrating frame navigation.
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
  const interpolated = expectedVideoInterpolation(updated, 4);
  await expect(page.getByTestId("class-picker-popover")).toHaveCount(0);
  await expect(row).toContainText("2 关键帧");
  await waitForVideoRecordingShape(page, manifest, updated.geometry.keyframes[1]!.bbox, {
    dashed: false,
  });
  await collapseVideoSelectionCard(page);
  await page.getByTestId("video-tool-btn-select").click();
  await holdVideoRecording(page, 1_000);

  // Frame controls, media time and the saved interpolation all move together.
  await setVideoRecordingTimeline(page, true);
  const slider = timeline.getByRole("slider", { name: "视频帧时间轴", exact: true });
  await expect(slider).toHaveCount(1);
  const currentFrame = async () => {
    const value = await stage.getAttribute("data-video-frame-index");
    if (value === null || !/^\d+$/.test(value)) {
      throw new Error(`[video-track] 当前视频帧无效: ${value}`);
    }
    return Number(value);
  };

  await page.getByRole("button", { name: "回到首帧", exact: true }).click();
  await waitForVideoRecordingFrame(page, manifest, 0);
  await expect.poll(() => source.evaluate((video: HTMLVideoElement) => video.paused)).toBe(true);
  const startTimelineValue = Number(await slider.inputValue());
  const startTimeText = await page.getByTestId("video-time-readout").textContent();

  for (let i = 0; i < 8; i++) {
    const before = await currentFrame();
    await page.getByRole("button", { name: "下一帧", exact: true }).click();
    await expect.poll(currentFrame).toBeGreaterThan(before);
    const frame = await currentFrame();
    await waitForVideoRecordingFrame(page, manifest, frame);
    if (frame === 4)
      await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });
    if (frame === 8)
      await waitForVideoRecordingShape(page, manifest, updated.geometry.keyframes[1]!.bbox, {
        dashed: false,
      });
    await page.waitForTimeout(350);
  }
  await expect
    .poll(async () => Number(await slider.inputValue()))
    .toBeGreaterThan(startTimelineValue);
  await expect(page.getByTestId("video-time-readout")).not.toHaveText(startTimeText ?? "");
  await page.waitForTimeout(1_200);

  for (let i = 0; i < 3; i++) {
    const before = await currentFrame();
    await page.getByRole("button", { name: "上一帧", exact: true }).click();
    await expect.poll(currentFrame).toBeLessThan(before);
    const frame = await currentFrame();
    await waitForVideoRecordingFrame(page, manifest, frame);
    if (frame === 5) {
      await waitForVideoRecordingShape(page, manifest, expectedVideoInterpolation(updated, 5), {
        dashed: true,
      });
    }
    await page.waitForTimeout(400);
  }
  await page.waitForTimeout(1_000);

  // Check the real media element and frame progression, not only the button icon.
  const playToggle = page.getByRole("button", { name: "播放 / 暂停", exact: true });
  const beforePlayback = await currentFrame();
  await playToggle.click();
  await expect.poll(() => source.evaluate((video: HTMLVideoElement) => video.paused)).toBe(false);
  await expect.poll(currentFrame).toBeGreaterThan(beforePlayback);
  await page.waitForTimeout(1_200);
  await playToggle.click();
  await expect.poll(() => source.evaluate((video: HTMLVideoElement) => video.paused)).toBe(true);
  await page.waitForTimeout(250);
  const pausedFrame = await currentFrame();
  await page.waitForTimeout(1_500);
  expect(await currentFrame()).toBe(pausedFrame);

  for (let i = 0; i < 6; i++) {
    const before = await currentFrame();
    await page.getByRole("button", { name: "上一帧", exact: true }).click();
    await expect.poll(currentFrame).toBeLessThan(before);
    await waitForVideoRecordingFrame(page, manifest, await currentFrame());
    await page.waitForTimeout(350);
  }
  await expect(source).toHaveJSProperty("paused", true);
  await expect(timeline).toBeVisible();
  await page.waitForTimeout(1_000);

  await page.getByRole("button", { name: "回到首帧", exact: true }).click();
  await waitForVideoRecordingFrame(page, manifest, 0);
  await seekVideoRecordingFrame(page, manifest, 4);
  await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });

  const verifyStored = async () => {
    const saved = await readSavedVideoTracks(page, taskId);
    expect(saved.map((annotation) => annotation.id)).toEqual([initial.id]);
    expect(saved[0]!.geometry).toEqual(updated.geometry);
  };
  await verifyStored();
  await page.reload();
  await expect(timeline).toBeVisible({ timeout: 20_000 });
  await expect(stage).toBeVisible();
  await expect
    .poll(() => source.evaluate((video: HTMLVideoElement) => video.readyState))
    .toBeGreaterThanOrEqual(2);
  if (!(await source.evaluate((video: HTMLVideoElement) => video.paused))) {
    await playToggle.click();
  }
  await expect(source).toHaveJSProperty("paused", true);
  await page.getByRole("button", { name: "回到首帧", exact: true }).click();
  await waitForVideoRecordingFrame(page, manifest, 0);
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  const reloadedRow = page
    .getByTestId("video-track-row")
    .filter({ hasText: updated.geometry.track_id.slice(0, 8) });
  await expect(reloadedRow).toContainText("2 关键帧");
  await reloadedRow.click();
  await expect(reloadedRow).toHaveAttribute("aria-selected", "true");
  await setVideoRecordingTimeline(page, true);
  await seekVideoRecordingFrame(page, manifest, 4);
  await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });
  const reloaded = await readSavedVideoTracks(page, taskId);
  expect(reloaded.map((annotation) => annotation.id)).toEqual([initial.id]);
  expect(inspectVideoTrack(reloaded[0], taskId, firstAnchor.label, [0, 8]).geometry).toEqual(
    updated.geometry,
  );
  await expect(stage).toHaveAttribute("data-video-frame-index", "4");
  await expect(timeline).toBeVisible();
  await holdVideoRecording(page, 2_000);
  await waitForVideoRecordingShape(page, manifest, interpolated, { dashed: true });
  const drawEndMs = Date.now();
  return { drawStartMs, drawEndMs };
}
