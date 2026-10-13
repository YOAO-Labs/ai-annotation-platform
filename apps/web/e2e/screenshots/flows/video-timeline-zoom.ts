/** Record pointer-anchored zoom and pan while preserving the selected track and frame. */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import {
  commitPendingAnnotationClass,
  mediaBbox,
  movePointerAtRefreshRate,
  recordingAnchor,
  renderedMediaBounds,
  selectVideoRecordingClass,
} from "./_canvas";
import {
  assertVideoTimelineVisible,
  currentVideoFrame,
  openVideoTimeline,
  parkVideoPointer,
  readVideoWindow,
} from "./_video-timeline";
import { collapseVideoSelectionCard } from "./_video-keyframe-recording";

export interface VideoTimelineZoomRecordingWindow extends DrawWindow {
  evidence: {
    selectedAnnotationId: string;
    currentFrame: number;
    maxFrame: number;
    zooms: Array<{ pointerRatio: number; anchorFrame: number; from: number; to: number }>;
    pannedWindow: { from: number; to: number };
    resetWindow: { from: number; to: number };
    selectionPreserved: boolean;
  };
}

export async function runVideoTimelineZoom(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (annotationId: string) => void,
): Promise<VideoTimelineZoomRecordingWindow> {
  const maxFrame = await openVideoTimeline(page, catalog);
  const stage = page.getByTestId("video-konva-stage");
  const rows = page.getByTestId("video-track-row");
  await expect(rows).toHaveCount(0);
  await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
  await page.getByTestId("video-tool-btn-track").click();
  const anchor = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
  await selectVideoRecordingClass(page, stage, anchor.label);
  const rect = mediaBbox(await renderedMediaBounds(stage), anchor.bbox);
  await page.mouse.move(rect.start.x, rect.start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, rect.start, rect.end, 700);
  await page.mouse.up();
  const created = await commitPendingAnnotationClass(page, {
    label: anchor.label,
    taskId: catalog.projects.video_demo.tasks.tracking.id,
    onCreated,
  });
  await expect(rows).toHaveCount(1);
  // 新建轨迹会同时打开选中浮窗，浮窗覆盖右栏轨道行会让点击无限等待。
  await collapseVideoSelectionCard(page);
  await rows.click();
  await expect(rows).toHaveAttribute("aria-selected", "true");
  await page.getByTestId("video-tool-btn-select").click();
  const selectionText = await rows.innerText();
  const frame = await currentVideoFrame(page);
  const assertUnchanged = async () => {
    expect(await currentVideoFrame(page)).toBe(frame);
    await expect(rows).toHaveCount(1);
    await expect(rows).toHaveAttribute("aria-selected", "true");
    await expect(rows).toHaveText(selectionText, { useInnerText: true });
    await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", true);
    await assertVideoTimelineVisible(page);
  };
  await parkVideoPointer(page);
  await assertUnchanged();
  const drawStartMs = Date.now();
  await page.waitForTimeout(1500);
  const timeline = page.getByTestId("video-timeline-shell");
  const zooms: VideoTimelineZoomRecordingWindow["evidence"]["zooms"] = [];
  const zoomAt = async (ratio: number) => {
    const before = await readVideoWindow(page, maxFrame);
    const box = await timeline.boundingBox();
    if (!box) throw new Error("Video timeline is not visible");
    await page.mouse.move(box.x + box.width * ratio, box.y + box.height * 0.55);
    await page.keyboard.down("Control");
    try {
      await page.mouse.wheel(0, -80);
    } finally {
      await page.keyboard.up("Control");
    }
    await expect
      .poll(async () => {
        const after = await readVideoWindow(page, maxFrame);
        return after.to - after.from;
      })
      .toBeLessThan(before.to - before.from - 1);
    const after = await readVideoWindow(page, maxFrame);
    const anchorBefore = before.from + ratio * (before.to - before.from);
    const anchorAfter = after.from + ratio * (after.to - after.from);
    expect(
      Math.abs(anchorAfter - anchorBefore),
      "Zoom keeps the frame beneath the pointer fixed",
    ).toBeLessThan(0.1);
    zooms.push({ pointerRatio: ratio, anchorFrame: anchorBefore, ...after });
    await parkVideoPointer(page);
    await assertUnchanged();
    await page.waitForTimeout(1600);
  };
  await zoomAt(0.64);
  await zoomAt(0.36);
  const beforePan = await readVideoWindow(page, maxFrame);
  const box = await timeline.boundingBox();
  if (!box) throw new Error("Video timeline is not visible");
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.55);
  await page.mouse.wheel(0, box.width * 0.05);
  await expect
    .poll(async () => (await readVideoWindow(page, maxFrame)).from)
    .toBeGreaterThan(beforePan.from + 1);
  const afterPan = await readVideoWindow(page, maxFrame);
  expect(afterPan.to - afterPan.from).toBeCloseTo(beforePan.to - beforePan.from, 5);
  await parkVideoPointer(page);
  await assertUnchanged();
  await page.waitForTimeout(1800);
  await page.getByRole("button", { name: "适配全部帧", exact: true }).click();
  await expect(page.getByTestId("video-timeline-navigator-window")).toHaveAttribute(
    "data-full-window",
    "true",
  );
  expect(await readVideoWindow(page, maxFrame)).toEqual({ from: 0, to: maxFrame });
  await parkVideoPointer(page);
  await assertUnchanged();
  await page.waitForTimeout(2300);
  await assertUnchanged();
  return {
    drawStartMs,
    drawEndMs: Date.now(),
    evidence: {
      selectedAnnotationId: String(created.id),
      currentFrame: frame,
      maxFrame,
      zooms,
      pannedWindow: afterPan,
      resetWindow: await readVideoWindow(page, maxFrame),
      selectionPreserved: true,
    },
  };
}
