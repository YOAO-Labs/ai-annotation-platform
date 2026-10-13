import { panelCommand } from "../fixtures/workbench-panel-actions";
import type { APIRequestContext, APIResponse, Locator, Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";

import { expect, test as base, type SeedData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type TrackKind = "bbox" | "polygon" | "polyline" | "mask";
type Source = "manual" | "prediction" | "interpolated";
type FrameState = "keyframe" | "interpolated" | "held" | "outside" | "unavailable";

interface Keyframe {
  frame_index: number;
  source?: Source;
  occluded?: boolean;
  bbox?: { x: number; y: number; w: number; h: number };
  points?: number[][];
  mask?: { object_key: string; [key: string]: unknown };
}

interface Track {
  id: string;
  version: number;
  source: string;
  class_name: string;
  is_locked: boolean;
  geometry: {
    type: string;
    track_id: string;
    keyframes: Keyframe[];
    outside: Array<{ from: number; to: number; source?: string }>;
  };
}

interface TrackCase {
  data: SeedData;
  taskId: string;
  token: string;
  tracks: Record<TrackKind, Track>;
}

const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
const bar = (page: Page) => page.getByTestId("video-track-context-bar");
const writeActions = (page: Page) =>
  bar(page).getByRole("button", { name: /补关键帧|标记 outside|恢复显示|延展轨迹/ });

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function readTrack(request: APIRequestContext, fixture: TrackCase, id: string) {
  const tracks = await json<Track[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: auth(fixture.token),
    }),
  );
  const track = tracks.find((annotation) => annotation.id === id);
  expect(track, `persisted annotation ${id}`).toBeDefined();
  return track!;
}

async function patchTrack(
  request: APIRequestContext,
  fixture: TrackCase,
  track: Track,
  data: Record<string, unknown>,
) {
  return json<Track>(
    await request.patch(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${track.id}`, {
      headers: { ...auth(fixture.token), "If-Match": `W/"${track.version}"` },
      data,
    }),
  );
}

function requestPath(rawUrl: string): string {
  try {
    return new URL(rawUrl).pathname;
  } catch {
    return "console";
  }
}

const test = base.extend<{ trackCase: TrackCase }>({
  trackCase: async ({ page, request, seed }, provideFixture, testInfo) => {
    const data = await seed.owned();
    const { task_id: taskId } = await seed.videoTask(data.project_id);
    const token = await seed.accessToken(data.admin_email);
    await seed.configureRasterMask(data.project_id, true);
    // The deterministic RLE seed needs the project's original backend pool identity.
    const candidate = await seed.nativeMaskCandidate(taskId, { variant: "multimask_donut" });
    // Disable the seed's unreachable SAM registration through real configuration.
    // This suite exercises stored geometry and does not perform model inference.
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
        headers: auth(token),
        data: {
          ai_enabled: false,
          ai_interactive_enabled: false,
          ml_backend_id: null,
          tool_bindings: Object.fromEntries(
            ["bbox", "region", "polyline"].map((unit) => [
              unit,
              {
                enabled: true,
                classes: [{ name: "car", color: "#22c55e" }],
                attribute_schema: { fields: [] },
                video_modes: { box: true, track: true },
              },
            ]),
          ),
        },
      }),
    );
    const disabled = await request.delete(
      `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
      { headers: auth(token) },
    );
    expect(disabled.status()).toBe(204);
    const mask = await json<NonNullable<Keyframe["mask"]>>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
        headers: auth(token),
        data: candidate.rles.at(-1) ?? candidate.rle,
      }),
    );
    const frames: Record<TrackKind, Keyframe[]> = {
      bbox: [
        { frame_index: 0, bbox: { x: 0.125, y: 0.125, w: 0.125, h: 0.25 }, source: "manual" },
        { frame_index: 10, bbox: { x: 0.375, y: 0.125, w: 0.125, h: 0.25 }, source: "prediction" },
      ],
      polygon: [
        {
          frame_index: 0,
          points: [
            [0.125, 0.5],
            [0.25, 0.5],
            [0.25, 0.625],
          ],
          source: "prediction",
        },
        {
          frame_index: 10,
          points: [
            [0.25, 0.5],
            [0.375, 0.5],
            [0.375, 0.625],
          ],
          source: "manual",
        },
      ],
      polyline: [
        {
          frame_index: 0,
          points: [
            [0.625, 0.5],
            [0.75, 0.625],
          ],
          source: "interpolated",
        },
        {
          frame_index: 10,
          points: [
            [0.75, 0.5],
            [0.875, 0.625],
          ],
          source: "manual",
        },
      ],
      mask: [
        { frame_index: 0, mask, source: "manual", occluded: false },
        { frame_index: 10, mask, source: "prediction", occluded: false },
      ],
    };
    const tracks = {} as Record<TrackKind, Track>;
    for (const kind of ["bbox", "polygon", "polyline", "mask"] as const) {
      tracks[kind] = await json<Track>(
        await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
          headers: auth(token),
          data: {
            annotation_type: `video_track_${kind}`,
            tool_unit_id: kind === "polygon" || kind === "mask" ? "region" : kind,
            class_name: "car",
            geometry: {
              type: `video_track_${kind}`,
              track_id: `f1-${kind}`,
              keyframes: frames[kind],
              outside: [],
            },
          },
        }),
      );
    }
    await seed.setPetEnabled(data.admin_email, true, token);
    await seed.injectToken(page, data.admin_email);
    await page.setViewportSize({ width: 1366, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") {
        errors.push(`${requestPath(message.location().url)}: ${message.text()}`);
      }
    });
    page.on("response", (response) => {
      if (response.status() >= 400) {
        errors.push(
          `${response.request().method()} ${requestPath(response.url())}: ${response.status()}`,
        );
      }
    });
    try {
      await provideFixture({ data, taskId, token, tracks });
      expect(errors).toEqual([]);
    } finally {
      // Non-secret ownership evidence lets the isolated runner clean this case exactly.
      await writeFile(
        testInfo.outputPath("video-track-context-metadata.json"),
        JSON.stringify(
          {
            projectId: data.project_id,
            taskIds: [taskId],
            annotationIds: Object.values(tracks).map((track) => track.id),
            maskObjectKeys: [mask.object_key],
          },
          null,
          2,
        ),
      );
    }
  },
});

async function open(page: Page, fixture: TrackCase, mode: "annotate" | "review" = "annotate") {
  await page.goto(`/projects/${fixture.data.project_id}/${mode}?task=${fixture.taskId}`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 25_000 });
  await expect(bar(page)).toBeVisible();
}

async function frame(page: Page) {
  const text = await page.getByTestId("video-track-context-frame").innerText();
  const match = /F(\d+)/.exec(text);
  expect(match, `source frame readout: ${text}`).not.toBeNull();
  return Number(match![1]);
}

async function blur(page: Page) {
  // Focus only; never mutate application stores or playback state from evaluate.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
}

async function seek(page: Page, target: number) {
  await blur(page);
  await page.keyboard.press("k");
  let current = await frame(page);
  while (current !== target) {
    const next = current + (target > current ? 1 : -1);
    await page.keyboard.press(target > current ? "ArrowRight" : "ArrowLeft");
    await expect.poll(() => frame(page)).toBe(next);
    current = next;
  }
}

function row(page: Page, track: Track): Locator {
  if (track.geometry.type === "video_track_bbox") {
    return page
      .getByTestId("video-track-row")
      .filter({ hasText: track.geometry.track_id.slice(0, 8) });
  }
  return page.getByTestId(
    track.geometry.type === "video_track_mask"
      ? `video-mask-track-${track.id}`
      : `box-list-item-${track.id}`,
  );
}

async function select(page: Page, track: Track) {
  await seek(page, 0);
  await row(page, track).click();
  await expect(bar(page)).toContainText(track.geometry.track_id.slice(0, 8));
  await expect(bar(page)).toContainText("car");
  const collapse = page.getByRole("button", { name: "收起浮窗", exact: true });
  if (await collapse.isVisible()) await collapse.click();
}

async function state(page: Page, expected: FrameState, index: number) {
  await expect(page.getByTestId("video-track-context-state")).toHaveAttribute(
    "data-state",
    expected,
  );
  await expect(page.getByTestId("video-track-context-frame")).toContainText(`F${index}`);
}

async function annotationWrite(page: Page, fixture: TrackCase, track: Track, action: string) {
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().endsWith(`/api/v1/tasks/${fixture.taskId}/annotations/${track.id}`),
  );
  await bar(page).getByRole("button", { name: action, exact: true }).click();
  expect((await saved).status()).toBe(200);
}

test.describe("当前视频轨迹条：真实几何与持久化", () => {
  test.setTimeout(100_000);
  test.skip(
    process.env.PLAYWRIGHT_RASTER_MASK_MATRIX !== "native",
    "requires native Mask API writes",
  );

  test("F1-1 四种轨迹的关键帧、插值与最近 Mask 保持沿用真实时间轴", async ({ page, trackCase }) => {
    await open(page, trackCase);
    await expect(bar(page)).toContainText("选择轨迹");
    const sources = { bbox: "人工", polygon: "AI预测", polyline: "插值记录" };
    for (const kind of ["bbox", "polygon", "polyline"] as const) {
      const track = trackCase.tracks[kind];
      await select(page, track);
      await state(page, "keyframe", 0);
      await expect(page.getByTestId("video-track-context-source")).toContainText(sources[kind]);
      await expect(page.getByTestId("video-timeline-track-keyframe")).toHaveCount(2);
      await expect(
        bar(page).getByRole("button", { name: "上一关键帧", exact: true }),
      ).toBeDisabled();
      const next = bar(page).getByRole("button", { name: "下一关键帧", exact: true });
      await expect(next).toContainText("F10");
      await next.click();
      await state(page, "keyframe", 10);
      await bar(page).getByRole("button", { name: "上一关键帧", exact: true }).click();
      await state(page, "keyframe", 0);
      await seek(page, 5);
      await state(page, "interpolated", 5);
      await expect(page.getByTestId("video-track-context-source")).toContainText("来源未知");
      await expect(
        bar(page).getByRole("button", { name: "上一关键帧", exact: true }),
      ).toContainText("F0");
      await expect(next).toContainText("F10");
    }
    await select(page, trackCase.tracks.mask);
    await state(page, "keyframe", 0);
    await seek(page, 5);
    await state(page, "held", 5);
    await expect(page.getByTestId("video-track-context-source")).toContainText("保持自 F0");
    await seek(page, 8);
    await state(page, "held", 8);
    await expect(page.getByTestId("video-track-context-source")).toContainText("保持自 F10");
    await expect(page.getByTestId("video-track-context-source")).toContainText("AI预测");
    await bar(page).getByRole("button", { name: "下一关键帧", exact: true }).click();
    await state(page, "keyframe", 10);
    await expect(bar(page).getByRole("button", { name: "下一关键帧", exact: true })).toBeDisabled();
  });

  test("F1-2 outside 与遮挡保留身份，缺关键帧来源不继承整条 AI 来源", async ({
    page,
    request,
    seed,
    trackCase,
  }) => {
    const prediction = await seed.injectPrediction({
      taskId: trackCase.taskId,
      projectId: trackCase.data.project_id,
      label: "car",
      polygon: [
        [0.1, 0.1],
        [0.2, 0.1],
        [0.2, 0.2],
      ],
    });
    const original = await json<Track>(
      await request.post(`${API_BASE}/api/v1/tasks/${trackCase.taskId}/annotations`, {
        headers: auth(trackCase.token),
        data: {
          annotation_type: "video_track_bbox",
          tool_unit_id: "bbox",
          class_name: "car",
          parent_prediction_id: prediction.prediction_id,
          geometry: { ...trackCase.tracks.bbox.geometry, track_id: "unknown" },
        },
      }),
    );
    const keyframes = original.geometry.keyframes.map((keyframe, index) => {
      if (index !== 0) return { ...keyframe, source: "manual" as const };
      const { source: _source, ...withoutSource } = keyframe;
      return { ...withoutSource, occluded: true };
    });
    const unknown = await patchTrack(request, trackCase, original, {
      geometry: {
        ...original.geometry,
        keyframes,
        outside: [{ from: 4, to: 6, source: "manual" }],
      },
    });
    const persisted = await readTrack(request, trackCase, unknown.id);
    expect(persisted.source).toBe("prediction_based");
    expect(persisted.geometry.keyframes[0]).not.toHaveProperty("source");
    await open(page, trackCase);
    await select(page, unknown);
    const color = await bar(page).locator("circle").first().getAttribute("fill");
    expect(color).toBeTruthy();
    await state(page, "keyframe", 0);
    await expect(page.getByTestId("video-track-context-source")).toHaveText("来源未知");
    await expect(bar(page)).toContainText("遮挡");
    await seek(page, 5);
    await state(page, "outside", 5);
    await expect(bar(page)).toContainText("unknown");
    await expect(bar(page)).toContainText("car");
    await expect(bar(page).locator("circle").first()).toHaveAttribute("fill", color!);
    await seek(page, 8);
    await state(page, "unavailable", 8);
    await expect(bar(page)).toContainText("unknown");
    await seek(page, 10);
    await state(page, "keyframe", 10);
    await expect(page.getByTestId("video-track-context-source")).toHaveText("人工");
    await expect(bar(page).getByText("遮挡", { exact: true })).toHaveCount(0);
    await seek(page, 12);
    await state(page, "unavailable", 12);
    expect(await readTrack(request, trackCase, unknown.id)).toEqual(persisted);
  });

  test("F1-3 补关键帧与 Mask outside 可刷新读回，宽窄布局和时间轴折叠不重建轨迹条", async ({
    page,
    request,
    trackCase,
  }, testInfo) => {
    await open(page, trackCase);
    for (const kind of ["bbox", "polygon", "polyline"] as const) {
      const track = trackCase.tracks[kind];
      await select(page, track);
      await seek(page, 5);
      await state(page, "interpolated", 5);
      await annotationWrite(page, trackCase, track, "补关键帧");
      await state(page, "keyframe", 5);
      const saved = await readTrack(request, trackCase, track.id);
      expect(saved.geometry.keyframes.map((keyframe) => keyframe.frame_index)).toEqual([0, 5, 10]);
      expect(saved.geometry.keyframes[1].source).toBe("manual");
      if (kind === "bbox") expect(saved.geometry.keyframes[1].bbox?.x).toBeCloseTo(0.25);
      else
        expect(saved.geometry.keyframes[1].points!.length).toBeGreaterThanOrEqual(
          kind === "polygon" ? 3 : 2,
        );
      if (kind === "bbox") {
        await annotationWrite(page, trackCase, track, "标记 outside");
        await state(page, "outside", 5);
        await expect(bar(page)).toContainText(track.geometry.track_id);
        const outside = await readTrack(request, trackCase, track.id);
        expect(outside.geometry.outside.some((range) => range.from <= 5 && range.to >= 5)).toBe(
          true,
        );
        await annotationWrite(page, trackCase, track, "恢复显示");
        await state(page, "keyframe", 5);
      }
      await page.reload();
      await expect(page.getByTestId("video-konva-stage")).toBeVisible();
      await select(page, saved);
      await seek(page, 5);
      await state(page, "keyframe", 5);
      expect((await readTrack(request, trackCase, track.id)).geometry).toEqual(saved.geometry);
    }
    const mask = trackCase.tracks.mask;
    await select(page, mask);
    await seek(page, 5);
    for (const [action, expected] of [
      ["标记 outside", "outside"],
      ["恢复显示", "held"],
    ] as const) {
      const saved = page.waitForResponse(
        (response) =>
          response.request().method() === "PATCH" &&
          response.url().endsWith(`/video/tracks/${mask.id}/mask-keyframes/5`),
      );
      await bar(page).getByRole("button", { name: action, exact: true }).click();
      expect((await saved).status()).toBe(200);
      await state(page, expected, 5);
      const stored = await readTrack(request, trackCase, mask.id);
      expect(stored.geometry.outside.some((range) => range.from <= 5 && range.to >= 5)).toBe(
        expected === "outside",
      );
      expect(stored.geometry.keyframes).toEqual(mask.geometry.keyframes);
    }
    await page.reload();
    await expect(page.getByTestId("video-konva-stage")).toBeVisible();
    await select(page, mask);
    await seek(page, 5);
    await state(page, "held", 5);
    const identity = await bar(page).elementHandle();
    const toggle = page.getByTestId("video-timeline-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("video-timeline-track-keyframe")).toHaveCount(2);
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await panelCommand(page, "标注详情", "隐藏面板");
    await expect(page.locator('[data-workbench-panel="inspector"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await expect(
      page.locator('[data-workbench-panel="canvas"]').getByTestId("video-track-context-bar"),
    ).toBeVisible();
    expect(await bar(page).evaluate((node, original) => node === original, identity)).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath("video-track-context-wide.png"),
      animations: "disabled",
    });
    await page.setViewportSize({ width: 900, height: 740 });
    await state(page, "held", 5);
    await expect(bar(page)).toBeInViewport();
    expect(await bar(page).evaluate((node, original) => node === original, identity)).toBe(true);
    const bounds = await bar(page)
      .getByRole("group", { name: "当前视频轨迹", exact: true })
      .boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(900);
    await page.screenshot({
      path: testInfo.outputPath("video-track-context-narrow.png"),
      animations: "disabled",
    });
    await identity?.dispose();
  });

  test("F1-4 标注锁、会话锁与已完成审核限制写动作，K 仍暂停播放", async ({
    page,
    request,
    seed,
    trackCase,
  }) => {
    let bbox = await patchTrack(request, trackCase, trackCase.tracks.bbox, { is_locked: true });
    await open(page, trackCase);
    await select(page, bbox);
    await seek(page, 5);
    const writes: string[] = [];
    page.on("request", (outgoing) => {
      if (
        ["POST", "PATCH", "PUT", "DELETE"].includes(outgoing.method()) &&
        /\/api\/v1\/(?:annotations(?:\/|$)|tasks\/[^/]+\/(?:annotations(?:\/|$)|video\/tracks\/))/.test(
          requestPath(outgoing.url()),
        )
      ) {
        writes.push(`${outgoing.method()} ${requestPath(outgoing.url())}`);
      }
    });
    await expect(bar(page)).toContainText("已锁定");
    await expect(writeActions(page)).toHaveCount(0);
    await blur(page);
    await page.keyboard.press("o");
    await page.keyboard.press("q");
    expect(await readTrack(request, trackCase, bbox.id)).toEqual(bbox);
    expect(writes).toEqual([]);

    bbox = await patchTrack(request, trackCase, bbox, { is_locked: false });
    await page.reload();
    await expect(page.getByTestId("video-konva-stage")).toBeVisible();
    await select(page, bbox);
    await blur(page);
    await page.keyboard.press("l");
    await expect(bar(page)).toContainText("已锁定");
    await seek(page, 5);
    await expect(writeActions(page)).toHaveCount(0);
    await blur(page);
    await page.keyboard.press("o");
    await page.keyboard.press("q");
    expect(await readTrack(request, trackCase, bbox.id)).toEqual(bbox);
    expect(writes).toEqual([]);
    await page.keyboard.press("l");
    await expect(bar(page).getByText("已锁定", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "播放 / 暂停", exact: true }).click();
    await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", false);
    await expect.poll(() => frame(page)).toBeGreaterThan(5);
    await blur(page);
    await page.keyboard.press("k");
    await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", true);
    expect(await readTrack(request, trackCase, bbox.id)).toEqual(bbox);
    expect(writes).toEqual([]);

    // Leave the annotator view before switching accounts so its lease is released
    // under the original identity, as a normal user navigation would do.
    await page.goto(`/projects/${trackCase.data.project_id}`);
    const unlocked = await request.delete(`${API_BASE}/api/v1/tasks/${trackCase.taskId}/lock`, {
      headers: auth(trackCase.token),
    });
    expect(unlocked.status()).toBe(204);
    await seed.advanceTask({
      taskId: trackCase.taskId,
      toStatus: "completed",
      reviewerEmail: trackCase.data.reviewer_email,
    });
    await seed.injectToken(page, trackCase.data.reviewer_email);
    await open(page, trackCase, "review");
    await select(page, bbox);
    await seek(page, 5);
    await expect(bar(page)).toContainText("只读");
    await expect(writeActions(page)).toHaveCount(0);
    await bar(page).getByRole("button", { name: "下一关键帧", exact: true }).click();
    await state(page, "keyframe", 10);
    await blur(page);
    await page.keyboard.press("o");
    await page.keyboard.press("q");
    await page.keyboard.press("k");
    expect(await readTrack(request, trackCase, bbox.id)).toEqual(bbox);
    expect(writes).toEqual([]);
  });
});
