/**
 * 流程录制：从空白创建视频 Mask 轨迹，再在后续帧物化新关键帧。
 *
 * Mask 轨迹属于轨迹工具组，不使用单帧 Mask 的 M 快捷键。流程会落库，
 * 创建响应立即登记到调用者，由 finally / afterAll 精确清理。
 */
import { expect, type Page, type Request, type Response } from "@playwright/test";
import type Konva from "konva";
import type {
  AnnotationResponse,
  TaskVideoManifestResponse,
  VideoTrackMaskGeometry,
} from "../../../src/types";
import { validateCocoRle } from "../../../src/pages/Workbench/stage/shared/geometry/maskRle";
import { waitForRecordingPanels } from "./_workbench-layout";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  commitPendingAnnotationClass,
  mediaPoint,
  movePointerPathAtRefreshRate,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";
import {
  collapseVideoSelectionCard,
  waitForVideoRecordingFrame,
  seekVideoRecordingFrame,
} from "./_video-keyframe-recording";

async function stroke(page: Page, points: Array<{ x: number; y: number }>, durationMs: number) {
  const first = points[0];
  if (!first || points.length < 2) {
    throw new Error("[video-mask-track-edit] 笔刷路径至少需要两个点");
  }
  await page.mouse.move(first.x, first.y);
  await page.mouse.down();
  await movePointerPathAtRefreshRate(page, points, durationMs);
  await page.mouse.up();
  await page.waitForTimeout(250);
}

type MaskTrack = Record<string, unknown> & {
  id: string;
  version: number;
  geometry: VideoTrackMaskGeometry;
};

function maskTrack(value: Record<string, unknown>): MaskTrack {
  expect(value.geometry).toMatchObject({ type: "video_track_mask" });
  expect(Number.isInteger(value.version)).toBe(true);
  expect(typeof value.id).toBe("string");
  const track = value as MaskTrack;
  expect(track.geometry.track_id).toBeTruthy();
  return track;
}

async function readJson<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (url) => {
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok) throw new Error(`Video Mask readback: HTTP ${response.status}`);
    return response.json();
  }, path);
}

async function maskArea(page: Page, id: string, frame: number) {
  const rle = validateCocoRle(
    await readJson(page, `/api/v1/annotations/${id}/mask-content/${frame}`),
  );
  const area = rle.counts.reduce((sum, count, index) => sum + (index % 2 ? count : 0), 0);
  expect(area, "The edited truck Mask must retain foreground").toBeGreaterThan(100);
  return { area, size: rle.size };
}

async function waitForVisibleMask(page: Page, annotationId: string, area: number) {
  // A saved row alone does not prove the raster layer has decoded the new keyframe.
  await page.waitForFunction(
    ({ id, expectedArea }) => {
      const runtime = (window as unknown as { Konva?: typeof Konva }).Konva;
      const group = runtime?.stages
        .flatMap((stage) => stage.find(".raster-mask-annotation"))
        .find((node) => node.id() === id) as Konva.Group | undefined;
      const layer = group?.getLayer() as (Konva.Layer & { _waitingForDraw?: boolean }) | undefined;
      const image = group?.findOne(".raster-mask-fill") as Konva.Image | undefined;
      const bitmap = image?.image() as
        | (CanvasImageSource & { width: number; height: number })
        | undefined;
      if (!group?.isVisible() || !bitmap?.width || !bitmap.height || layer?._waitingForDraw)
        return false;
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const context = canvas.getContext("2d");
      if (!context) return false;
      context.drawImage(bitmap, 0, 0);
      const pixels = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
      let foreground = 0;
      for (let index = 3; index < pixels.length; index += 4) if (pixels[index] > 0) foreground++;
      return foreground === expectedArea;
    },
    { id: annotationId, expectedArea: area },
    { timeout: 20_000 },
  );
}

export async function runVideoMaskTrackEdit(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (annotationId: string) => void,
): Promise<DrawWindow & { evidence: Record<string, unknown> }> {
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  const failedMaskRequests: string[] = [];
  const temporaryMaskRequests: string[] = [];
  const observeRequest = (request: Request) => {
    if (/\/annotations\/tmp_[^/]+\/mask-content/.test(request.url()))
      temporaryMaskRequests.push(request.url());
  };
  const observeResponse = (response: Response) => {
    if (response.url().includes("/mask-content") && !response.ok())
      failedMaskRequests.push(`${response.status()} ${response.url()}`);
  };
  page.on("request", observeRequest);
  page.on("response", observeResponse);
  try {
    await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
    const stage = page.getByTestId("video-konva-stage");
    const source = page.getByTestId("video-konva-source");
    const timeline = page.getByTestId("video-timeline-shell");
    await expect(timeline).toBeVisible({ timeout: 20_000 });
    await expect(stage).toBeVisible();
    await expect
      .poll(() => source.evaluate((video: HTMLVideoElement) => video.readyState))
      .toBeGreaterThanOrEqual(2);
    for (const title of ["类别面板", "标注详情"]) {
      await page
        .getByRole("tab")
        .filter({ has: page.getByRole("button", { name: `隐藏${title}`, exact: true }) })
        .click();
    }
    await waitForRecordingPanels(page, ["canvas", "class-palette", "inspector"]);
    await page.getByRole("button", { name: "展开时间轴详情", exact: true }).click();
    const overlay = page.getByTestId("video-playback-overlay");
    await expect(overlay).toHaveAttribute("data-state", "expanded");
    await page.getByRole("button", { name: "回到首帧", exact: true }).click();
    await expect(stage).toHaveAttribute("data-video-frame-index", "0");
    await expect(source).toHaveJSProperty("paused", true);
    const manifest = await readJson<TaskVideoManifestResponse>(
      page,
      `/api/v1/tasks/${task.id}/video/manifest`,
    );
    expect(manifest.task_id).toBe(task.id);
    await waitForVideoRecordingFrame(page, manifest, 0);
    const dimensions = await source.evaluate((video: HTMLVideoElement) => [
      video.videoHeight,
      video.videoWidth,
    ]);
    expect(dimensions).toEqual([manifest.metadata.height, manifest.metadata.width]);
    const initialAnchor = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f0", 0);
    const editAnchor = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f5", 5);
    if (!initialAnchor.brush_strokes.length || !editAnchor.brush_strokes.length)
      throw new Error("Video truck Mask brush anchors are missing");
    const before = await readJson<AnnotationResponse[]>(
      page,
      `/api/v1/tasks/${task.id}/annotations`,
    );
    const toolbar = page.getByTestId("mask-toolbar");
    await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
    await page.getByTestId("video-tool-btn-mask-track").click();
    // ContextToolbar：Mask 工具先挂胶囊，需展开常用工具并点「更多设置」才挂载完整工具条。
    await page.getByTestId("mask-tool-capsule").waitFor({ state: "visible", timeout: 5000 });
    await page.getByTestId("mask-settings-trigger").click();
    await page.getByLabel("更多 Mask 工具").click();
    await expect(toolbar).toBeVisible();
    const drawStartMs = Date.now();
    await page.waitForTimeout(1_200);
    const box = await renderedMediaBounds(stage);
    const initialPath = initialAnchor.brush_strokes.flatMap((path, index) => {
      const points = path.map((point) => mediaPoint(box, point));
      return index % 2 === 0 ? points : points.reverse();
    });
    await stroke(page, initialPath, 1_800);
    await page.waitForTimeout(650);
    // 画布绘制会让 Radix 工具条收起；Enter 是 Mask 的主动作快捷键（保存）。
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeVisible();
    await page.waitForTimeout(1_000);
    const created = maskTrack(
      await commitPendingAnnotationClass(page, {
        label: initialAnchor.label,
        taskId: task.id,
        onCreated,
      }),
    );
    await expect(toolbar).toBeHidden();
    expect(created.geometry.keyframes).toHaveLength(1);
    expect(created.geometry.keyframes[0]).toMatchObject({ frame_index: 0, source: "manual" });
    const originalMask = created.geometry.keyframes[0].mask;
    const initial = await maskArea(page, created.id, 0);
    expect(initial.size).toEqual(dimensions);
    await waitForVisibleMask(page, created.id, initial.area);
    const row = page.getByTestId(`video-mask-track-${created.id}`);
    await expect(row).toContainText("1 关键帧");
    // The expanded selection card covers this row. Finish its exit before clicking the row,
    // so browser actionability retries cannot scroll the workspace behind the fixed card.
    await collapseVideoSelectionCard(page);
    await expect(page.getByLabel("展开选中信息卡(可拖动)", { exact: true })).toBeVisible();
    await row.click();
    await expect(stage).toBeInViewport({ ratio: 1 });
    await page.mouse.move(box.x + 6, box.y + 6);
    await page.waitForTimeout(1_500);
    for (let frame = 1; frame <= 5; frame++) {
      await page.getByRole("button", { name: "下一帧", exact: true }).click();
      await waitForVideoRecordingFrame(page, manifest, frame);
      await page.waitForTimeout(220);
    }
    await page.getByLabel("展开选中信息卡(可拖动)", { exact: true }).click();
    await expect(page.getByText(/编辑并保存当前帧 Mask/)).toBeVisible();
    // 「保持」帧不再以像素层渲染 Mask（画布只显示关键帧标签/红条），进入编辑后才可解码核对。
    await page.waitForTimeout(1_000);
    await page.getByTitle("编辑当前帧 Mask").click();
    // 先收起浮卡：它的点击属于外部交互，会关掉随后打开的 Radix 工具条。
    await collapseVideoSelectionCard(page);
    await stage.scrollIntoViewIfNeeded();
    // ContextToolbar：进入编辑只挂胶囊，完整工具条需展开常用工具并点「更多设置」。
    await page.getByTestId("mask-tool-capsule").waitFor({ state: "visible", timeout: 5000 });
    await page.getByTestId("mask-settings-trigger").click();
    await page.getByLabel("更多 Mask 工具").click();
    await expect(toolbar).toBeVisible();
    await toolbar.getByRole("radio", { name: "橡皮", exact: true }).click();
    const editBounds = await renderedMediaBounds(stage);
    for (const path of editAnchor.brush_strokes)
      await stroke(
        page,
        path.map((point) => mediaPoint(editBounds, point)),
        600,
      );
    await page.waitForTimeout(800);
    const updateResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        new URL(response.url()).pathname ===
          `/api/v1/tasks/${task.id}/video/tracks/${created.id}/mask-keyframes/5`,
      { timeout: 30_000 },
    );
    await page.keyboard.press("Enter");
    const response = await updateResponse;
    expect(response.ok(), `Save F5 Mask: HTTP ${response.status()}`).toBeTruthy();
    expect(response.request().headers()["if-match"]).toBe(`W/"${created.version}"`);
    const updated = maskTrack(await response.json());
    expect(updated.id).toBe(created.id);
    expect(updated.geometry.track_id).toBe(created.geometry.track_id);
    expect(updated.version).toBeGreaterThan(created.version);
    expect(updated.geometry.keyframes.map((keyframe) => keyframe.frame_index)).toEqual([0, 5]);
    expect(updated.geometry.keyframes[0].mask).toEqual(originalMask);
    expect(updated.geometry.keyframes[1]).toMatchObject({ frame_index: 5, source: "manual" });
    expect(updated.geometry.keyframes[1].mask.sha256).not.toBe(originalMask.sha256);
    await expect(toolbar).toBeHidden();
    const edited = await maskArea(page, created.id, 5);
    expect(edited.size).toEqual(dimensions);
    expect(edited.area, "Erasing a held Mask must reduce its foreground").toBeLessThan(
      initial.area,
    );
    await waitForVisibleMask(page, created.id, edited.area);
    await expect(row).toContainText("2 关键帧");
    await page.getByLabel("展开选中信息卡(可拖动)", { exact: true }).click();
    await page.locator('button[title="上一可见关键帧"]').click();
    await waitForVideoRecordingFrame(page, manifest, 0);
    await waitForVisibleMask(page, created.id, initial.area);
    await page.mouse.move(editBounds.x + 6, editBounds.y + 6);
    await page.waitForTimeout(1_500);
    await page.locator('button[title="下一可见关键帧"]').click();
    await waitForVideoRecordingFrame(page, manifest, 5);
    await waitForVisibleMask(page, created.id, edited.area);
    await page.getByRole("button", { name: "收起浮窗", exact: true }).click();
    await stage.scrollIntoViewIfNeeded();
    await expect(stage).toBeInViewport({ ratio: 1 });
    await expect(row).toContainText("2 关键帧");
    const finalBounds = await renderedMediaBounds(stage);
    await page.mouse.move(finalBounds.x + 6, finalBounds.y + 6);
    await expect(overlay).toHaveCSS("opacity", "1");
    await expect(overlay).toHaveAttribute("data-state", "expanded");
    await page.waitForTimeout(2_500);
    const drawEndMs = Date.now();
    await page.reload();
    await expect(stage).toBeVisible({ timeout: 20_000 });
    await expect
      .poll(() => source.evaluate((video: HTMLVideoElement) => video.readyState))
      .toBeGreaterThanOrEqual(2);
    const after = await readJson<AnnotationResponse[]>(
      page,
      `/api/v1/tasks/${task.id}/annotations`,
    );
    expect(after.map((item) => item.id).sort()).toEqual(
      [...before.map((item) => item.id), created.id].sort(),
    );
    expect(after.find((item) => item.id === created.id)).toMatchObject({
      version: updated.version,
      geometry: updated.geometry,
    });
    await seekVideoRecordingFrame(page, manifest, 5);
    await waitForVisibleMask(page, created.id, edited.area);
    const reloaded = await maskArea(page, created.id, 5);
    expect(reloaded).toEqual(edited);
    expect(temporaryMaskRequests).toEqual([]);
    expect(failedMaskRequests).toEqual([]);
    return {
      drawStartMs,
      drawEndMs,
      evidence: {
        annotation_id: created.id,
        track_id: created.geometry.track_id,
        source_size: dimensions,
        initial: { frame_index: 0, sha256: originalMask.sha256, area: initial.area },
        edited: {
          frame_index: 5,
          sha256: updated.geometry.keyframes[1].mask.sha256,
          area: edited.area,
        },
        source_version: created.version,
        result_version: updated.version,
        reload_verified: true,
      },
    };
  } finally {
    page.off("request", observeRequest);
    page.off("response", observeResponse);
  }
}
