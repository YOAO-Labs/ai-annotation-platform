/**
 * 流程录制：视频时序追踪工作台(video-track)概览 —— 逐帧前进 + 播放。
 *
 * 输出：outputs/flows/video-track.gif → docs-site/.../workbench/video-track-overview.gif
 *
 * 数据来自 screenshot catalog 的 video_demo（固定公开行车视频，H.264，72 帧）。
 * 本 flow 双向逐帧、播放暂停后，在首帧手动建立车辆轨迹并验证刷新持久化。
 * 调用者记录创建结果并在 finally 删除，仅修改隔离截图数据。
 *
 * 返回 { drawStartMs, drawEndMs }：供 finalize 裁掉开头(导航/解析/就绪等待)。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import {
  mediaBbox,
  renderedMediaBounds,
  recordingAnchor,
  selectVideoRecordingClass,
  movePointerAtRefreshRate,
  commitPendingAnnotationClass,
} from "./_canvas";
import { collapseVideoSelectionCard } from "./_video-keyframe-recording";

export async function runVideoTrack(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (annotationId: string) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.video_demo;
  await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.tracking.id}`);
  await page.waitForLoadState("domcontentloaded");

  // 等时间轴就绪（manifest 加载完成的信号）+ 首帧画面解码。
  const timeline = page.getByTestId("video-timeline-shell");
  const stage = page.getByTestId("video-konva-stage");
  const source = page.getByTestId("video-konva-source");
  await expect(timeline).toBeVisible({ timeout: 15_000 });
  await expect(stage).toBeVisible();
  await expect
    .poll(() => source.evaluate((video: HTMLVideoElement) => video.readyState))
    .toBeGreaterThanOrEqual(2);

  await page.getByRole("tab", { name: "标注详情", exact: true }).click();

  // 选 select(查看)工具：保证后续点击/按键不会误触发画框。
  const selectBtn = page.getByTestId("video-tool-btn-select");
  await selectBtn.click();
  await page.getByRole("button", { name: "展开时间轴详情", exact: true }).click();
  await expect(page.getByTestId("video-playback-overlay")).toHaveAttribute(
    "data-state",
    "expanded",
  );
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
  await expect.poll(currentFrame).toBe(0);
  await expect.poll(() => source.evaluate((video: HTMLVideoElement) => video.paused)).toBe(true);

  const drawStartMs = Date.now();
  await page.waitForTimeout(2_000);
  const startTimelineValue = Number(await slider.inputValue());
  const startTimeText = await page.getByTestId("video-time-readout").textContent();

  // ── 逐帧前进 8 帧（展示帧级控制，画面里车辆逐帧移动）──
  for (let i = 0; i < 8; i++) {
    const before = await currentFrame();
    await page.getByRole("button", { name: "下一帧", exact: true }).click();
    await expect.poll(currentFrame).toBeGreaterThan(before);
    await page.waitForTimeout(350);
  }
  await expect
    .poll(async () => Number(await slider.inputValue()))
    .toBeGreaterThan(startTimelineValue);
  await expect(page.getByTestId("video-time-readout")).not.toHaveText(startTimeText ?? "");
  await page.waitForTimeout(1200);

  for (let i = 0; i < 3; i++) {
    const before = await currentFrame();
    await page.getByRole("button", { name: "上一帧", exact: true }).click();
    await expect.poll(currentFrame).toBeLessThan(before);
    await page.waitForTimeout(400);
  }
  await page.waitForTimeout(1000);

  // Check the real media element and frame progression, not only the button icon.
  const playToggle = page.getByRole("button", { name: "播放 / 暂停", exact: true });
  const beforePlayback = await currentFrame();
  await playToggle.click();
  await expect.poll(() => source.evaluate((video: HTMLVideoElement) => video.paused)).toBe(false);
  await expect.poll(currentFrame).toBeGreaterThan(beforePlayback);
  await page.waitForTimeout(1200);
  await playToggle.click();
  await expect.poll(() => source.evaluate((video: HTMLVideoElement) => video.paused)).toBe(true);
  await page.waitForTimeout(250);
  const pausedFrame = await currentFrame();
  await page.waitForTimeout(1500);
  expect(await currentFrame()).toBe(pausedFrame);

  // 暂停后逐帧回看，展示播放头、帧号和画面可以双向核对。
  for (let i = 0; i < 6; i++) {
    const before = await currentFrame();
    await page.getByRole("button", { name: "上一帧", exact: true }).click();
    await expect.poll(currentFrame).toBeLessThan(before);
    await page.waitForTimeout(350);
  }
  await expect(source).toHaveJSProperty("paused", true);
  await expect(timeline).toBeVisible();
  await page.waitForTimeout(1000);

  await page.getByRole("button", { name: "回到首帧", exact: true }).click();
  await expect.poll(currentFrame).toBe(0);
  await page.getByRole("tab", { name: "标注详情", exact: true }).click();
  await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
  await page.getByTestId("video-tool-btn-track").click();
  const anchor = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
  await selectVideoRecordingClass(page, stage, anchor.label);
  const rect = mediaBbox(await renderedMediaBounds(stage), anchor.bbox);
  await page.mouse.move(rect.start.x, rect.start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, rect.start, rect.end, 900);
  await page.mouse.up();
  const created = await commitPendingAnnotationClass(page, {
    label: anchor.label,
    taskId: project.tasks.tracking.id,
    onCreated,
  });
  expect(typeof created.id).toBe("string");
  const annotationId = created.id as string;
  const track = page.getByTestId("video-track-row");
  await expect(track).toHaveCount(1);
  // 新建轨迹会同时打开选中浮窗，浮窗覆盖右栏轨道行会让点击无限等待。
  await collapseVideoSelectionCard(page);
  await track.click();
  await expect(track).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("video-track-timeline")).toBeVisible();
  await page.mouse.move(0, 0);
  await page.waitForTimeout(3000);
  const drawEndMs = Date.now();
  await page.reload();
  await expect(page.getByTestId("video-track-row")).toHaveCount(1);
  const stored = await page.evaluate(async (taskId) => {
    const response = await fetch(`/api/v1/tasks/${taskId}/annotations`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok) throw new Error(`Video annotation reload: HTTP ${response.status}`);
    return response.json() as Promise<Array<{ id: string; geometry: { type: string } }>>;
  }, project.tasks.tracking.id);
  expect(
    stored.some(
      (annotation) =>
        annotation.id === annotationId && annotation.geometry.type === "video_track_bbox",
    ),
  ).toBeTruthy();
  return { drawStartMs, drawEndMs };
}
