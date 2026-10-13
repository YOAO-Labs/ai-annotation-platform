import {
  isExpectedRequestAbort,
  videoWorkbenchAborts,
  type AbortAllowRule,
  type RequestErrorSignal,
} from "../helpers/request-errors";
import { invalidSeekTarget, rangeValueForFrame } from "../helpers/video-timeline-seek";
import { layoutCommand } from "../helpers/workbench-layout";
import type { APIRequestContext, APIResponse, Browser, Page, Route } from "@playwright/test";
import { randomUUID } from "node:crypto";

import { expect, test as base, type SeedData } from "../fixtures/seed";
import {
  expectVideoContextFramePixels,
  sampleVideoContextFrameMarkers,
  type FrameExpectations,
} from "../fixtures/video-frame-pixels";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const MAIN_FIXTURE = "h264-issue-context";
type Point = [number, number];
interface Issue {
  id: string;
  task_id: string;
  anchor_type: "pixel" | "task";
  annotation_id: string | null;
  anchor_position: { x: number; y: number; frame?: number; video_context?: VideoContext } | null;
  body: string;
}
interface Viewport {
  center_x: number;
  center_y: number;
  zoom: number;
}
interface TimelineWindow {
  from: number;
  to: number;
}
interface VideoContext {
  schema_version: number;
  track_id?: string;
  annotation_version?: number;
  frame_range?: { from_frame: number; to_frame: number };
  viewport?: Viewport;
  timeline_window?: TimelineWindow;
}
interface Track {
  id: string;
  version: number;
  geometry: {
    type: "video_track_bbox";
    track_id: string;
    keyframes: Array<{
      frame_index: number;
      source: "manual";
      bbox: { x: number; y: number; w: number; h: number };
    }>;
    outside: Array<{ from: number; to: number }>;
  };
}
interface IssueCase {
  data: SeedData;
  taskId: string;
  token: string;
  fixtureName: string;
  expectations: FrameExpectations;
  evidence: unknown[];
  writes: unknown[];
  mediaLatency: boolean;
  actorChanged: boolean;
  navigatedTaskIds: string[];
  releaseMedia: Array<() => void>;
  expectedHttpErrors: Array<{ path: string; status: number; bodyIncludes: string }>;
}

const stage = (page: Page) => page.getByTestId("video-konva-stage");
const navigation = (page: Page) => page.getByTestId("issue-frame-navigation");
const modal = (page: Page) => page.getByRole("dialog").filter({ hasText: "标记问题 (Issue)" });
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
const pathOf = (url: string) => new URL(url).pathname;
const isFixtureMedia = (url: URL, fixture: string) =>
  url.pathname.includes(`/e2e/video/webcodecs/${fixture}/`) &&
  /\/(?:source|chunk-\d+)\.mp4$/.test(url.pathname);

/** Fixture-specific cancellations beyond the shared workbench policy. */
function fixtureAbortRules(fixture: IssueCase): AbortAllowRule[] {
  const rules: AbortAllowRule[] = [];
  if (fixture.mediaLatency) {
    rules.push(
      {
        method: "DELETE",
        path: /^\/api\/v1\/tasks\/[0-9a-f-]{36}\/lock$/,
        reason: "navigation may cancel the lock release while media is still decoding",
      },
      {
        method: "GET",
        path: new RegExp(
          `/e2e/video/webcodecs/${fixture.fixtureName}/(?:source|chunk-\\d+)\\.mp4$`,
        ),
        reason: "retiring the task aborts in-flight fixture media fetches",
      },
    );
  }
  for (const id of fixture.navigatedTaskIds)
    rules.push({
      method: "DELETE",
      path: `/api/v1/tasks/${id}/lock`,
      reason: "navigation away from a visited task may cancel its lock release",
    });
  if (fixture.actorChanged)
    rules.push(
      {
        method: "GET",
        path: "/api/v1/projects",
        reason: "actor switch retires the previous session's project list query",
      },
      {
        method: "GET",
        path: "/api/v1/audit-logs",
        reason: "actor switch retires the previous session's audit-log query",
      },
    );
  return rules;
}

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function expectSeededChunks(
  request: APIRequestContext,
  token: string,
  video: { task_id: string; chunk_size_frames: number },
) {
  const base = `${API_BASE}/api/v1/tasks/${video.task_id}/video`;
  const manifest = await json<{ frame_count: number; chunk_size_frames: number }>(
    await request.get(`${base}/manifest-v2`, { headers: auth(token) }),
  );
  expect(video.chunk_size_frames).toBe(manifest.chunk_size_frames);
  const { chunks } = await json<{
    chunks: Array<{ chunk_id: number; start_frame: number; end_frame: number; status: string }>;
  }>(
    await request.get(`${base}/chunks`, {
      headers: auth(token),
      params: { from_frame: 0, to_frame: manifest.frame_count - 1 },
    }),
  );
  expect(chunks).toHaveLength(Math.ceil(manifest.frame_count / manifest.chunk_size_frames));
  for (const [index, chunk] of chunks.entries())
    expect(chunk).toMatchObject({
      chunk_id: index,
      start_frame: index * manifest.chunk_size_frames,
      end_frame: Math.min((index + 1) * manifest.chunk_size_frames, manifest.frame_count) - 1,
      status: "ready",
    });
}

async function graphicsEvidence(browser: Browser, page: Page) {
  const browserInfo = {
    browser: browser.version(),
    userAgent: page.isClosed() ? "page closed" : await page.evaluate(() => navigator.userAgent),
  };
  try {
    const session = await browser.newBrowserCDPSession();
    try {
      const info = await session.send("SystemInfo.getInfo");
      return {
        ...browserInfo,
        graphicsDevices: info.gpu.devices,
        graphicsFeatures: info.gpu.featureStatus,
        // Adapter metadata is evidence about rendering, not proof of hardware video decoding.
        hardwareVideoDecoderQualified: false,
      };
    } finally {
      await session.detach();
    }
  } catch (error) {
    return {
      ...browserInfo,
      graphicsMetadataUnavailable: String(error),
      hardwareVideoDecoderQualified: false,
    };
  }
}

const test = base.extend<{ issueCase: IssueCase; videoFixture: string }>({
  videoFixture: [MAIN_FIXTURE, { option: true }],
  issueCase: async ({ page, request, seed, browser, videoFixture }, provideFixture, testInfo) => {
    const data = await seed.owned();
    const video = await seed.videoWebCodecs(data.project_id, { fixture: videoFixture });
    const token = await seed.accessToken(data.admin_email);
    if (videoFixture === MAIN_FIXTURE) await expectSeededChunks(request, token, video);
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
        headers: auth(token),
        data: {
          ai_enabled: false,
          ai_interactive_enabled: false,
          ml_backend_id: null,
          video_sampling: { mode: "step", frame_step: 5 },
          tool_bindings: {
            bbox: {
              enabled: true,
              classes: [{ name: "car", color: "#22c55e" }],
              attribute_schema: { fields: [] },
              video_modes: { box: true, track: true },
            },
            region: {
              enabled: true,
              classes: [{ name: "car", color: "#22c55e" }],
              attribute_schema: { fields: [] },
              video_modes: { box: true, track: true },
            },
          },
        },
      }),
    );
    expect(
      (
        await request.delete(
          `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
          { headers: auth(token) },
        )
      ).status(),
    ).toBe(204);
    await seed.injectToken(page, data.admin_email);
    await page.evaluate(() => localStorage.setItem("video.experimental.webcodecs", "1"));
    await page.setViewportSize({ width: 1440, height: 1000 });
    const fixture: IssueCase = {
      data,
      taskId: video.task_id,
      token,
      fixtureName: videoFixture,
      expectations: video.frame_expectations as unknown as FrameExpectations,
      evidence: [],
      writes: [],
      mediaLatency: false,
      actorChanged: false,
      navigatedTaskIds: [],
      releaseMedia: [],
      expectedHttpErrors: [],
    };
    const errors: RequestErrorSignal[] = [];
    const pending: Promise<void>[] = [];
    const relevant = (url: string) =>
      pathOf(url).startsWith("/api/v1/") || isFixtureMedia(new URL(url), videoFixture);
    page.on("pageerror", (error) => errors.push({ kind: "page", message: error.message }));
    page.on("console", (message) => {
      if (message.type() === "error")
        errors.push({
          kind: "console",
          message: message.text(),
          path: message.location().url ? pathOf(message.location().url) : undefined,
        });
    });
    page.on("requestfailed", (outgoing) => {
      if (relevant(outgoing.url()))
        errors.push({
          kind: "request",
          method: outgoing.method(),
          path: pathOf(outgoing.url()),
          message: outgoing.failure()?.errorText ?? "unknown request failure",
        });
    });
    page.on("response", (response) => {
      if (response.status() < 400 || !relevant(response.url())) return;
      // Read the body immediately: Playwright Response handles can expire on reload.
      pending.push(
        (async () => {
          const entry: RequestErrorSignal = {
            kind: "http",
            method: response.request().method(),
            path: pathOf(response.url()),
            status: response.status(),
            message: response.request().method(),
          };
          try {
            entry.body = await response.text();
          } catch (error) {
            entry.body = String(error);
          }
          errors.push(entry);
        })(),
      );
    });
    page.on("request", (outgoing) => {
      if (outgoing.method() === "POST" && pathOf(outgoing.url()) === "/api/v1/feedbacks")
        fixture.writes.push(outgoing.postDataJSON());
    });
    let verified = false;
    try {
      await provideFixture(fixture);
      await Promise.all(pending);
      // Feedback/annotation writes are never allowlisted. Navigation may cancel lock cleanup.
      const rules = [...videoWorkbenchAborts, ...fixtureAbortRules(fixture)];
      const expectedAborts = errors.filter((error) => isExpectedRequestAbort(error, rules));
      const expectedHttp = errors.filter(
        (error) =>
          error.kind === "http" &&
          fixture.expectedHttpErrors.some(
            (expected) =>
              error.path === expected.path &&
              error.status === expected.status &&
              error.body?.includes(expected.bodyIncludes),
          ),
      );
      const expectedConsole = errors.filter(
        (error) =>
          error.kind === "console" &&
          expectedHttp.some(
            (response) =>
              response.path === error.path &&
              error.message.includes(`status of ${response.status}`),
          ),
      );
      const unexpected = errors.filter(
        (error) =>
          !expectedAborts.includes(error) &&
          !expectedHttp.includes(error) &&
          !expectedConsole.includes(error),
      );
      fixture.evidence.push({
        expectedFaults: [...expectedAborts, ...expectedHttp, ...expectedConsole],
        unexpectedErrors: unexpected,
      });
      expect(unexpected).toEqual([]);
      verified = true;
    } finally {
      try {
        if ((!verified || testInfo.status !== testInfo.expectedStatus) && !page.isClosed()) {
          const captures = await Promise.allSettled([
            page.screenshot({ fullPage: true, timeout: 3_000 }),
            page.locator("body").ariaSnapshot({ timeout: 3_000 }),
          ]);
          const [screenshot, aria] = captures;
          if (screenshot.status === "fulfilled")
            await testInfo.attach("failure-before-cleanup", {
              contentType: "image/png",
              body: screenshot.value,
            });
          else fixture.evidence.push({ failureScreenshotUnavailable: String(screenshot.reason) });
          if (aria.status === "fulfilled")
            await testInfo.attach("failure-aria-before-cleanup", {
              contentType: "text/plain",
              body: aria.value,
            });
          else fixture.evidence.push({ failureAriaUnavailable: String(aria.reason) });
        }
        await Promise.all(pending);
        await testInfo.attach("video-issue-context-evidence", {
          contentType: "application/json",
          body: JSON.stringify(
            {
              projectId: data.project_id,
              taskId: fixture.taskId,
              fixture: videoFixture,
              graphics: await graphicsEvidence(browser, page),
              feedbackWrites: fixture.writes,
              errors,
              evidence: fixture.evidence,
            },
            null,
            2,
          ),
        });
      } finally {
        page.removeAllListeners("response");
        page.removeAllListeners("requestfailed");
        page.removeAllListeners("request");
        page.removeAllListeners("pageerror");
        page.removeAllListeners("console");
        for (const release of fixture.releaseMedia) release();
        try {
          await page.unrouteAll({ behavior: "ignoreErrors" });
          if (!page.isClosed()) await page.goto("about:blank");
        } finally {
          await seed.owned();
        }
      }
    }
  },
});

async function open(page: Page, fixture: IssueCase) {
  // A cold media response may intentionally be held; DOMContentLoaded does not await it.
  await page.goto(`/projects/${fixture.data.project_id}/annotate?task=${fixture.taskId}`, {
    waitUntil: "domcontentloaded",
  });
  await expect(stage(page)).toBeVisible({ timeout: 25_000 });
  await page.getByRole("button", { name: "布局", exact: true }).click();
  await page.getByRole("button", { name: "标准标注布局", exact: true }).click();
}

async function key(page: Page, value: string) {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press(value);
}

async function expectPaintSettled(page: Page) {
  // 时间轴点击触发的精确取帧是异步完成的：负载高时会迟到，并盖掉随后键盘
  // 步进已到达的帧号（实测把 F120 拽回 F105）。等绘制帧号追上选中帧号再继
  // 续；原生 <video> 回退不经精确管线，无需等待。
  await expect
    .poll(
      () =>
        stage(page).evaluate((node) => {
          if (node.getAttribute("data-video-frame-source") !== "webcodecs") return true;
          return (
            node.getAttribute("data-video-painted-frame-index") ===
            node.getAttribute("data-video-frame-index")
          );
        }),
      { timeout: 30_000 },
    )
    .toBe(true);
}

async function seekNavigationSnapshot(page: Page, frame: number, value: number, phase: string) {
  // 只读快照（计划 D）：不改变事件传播、焦点或播放器状态，仅在导航失败时作为
  // 附件帮助区分「目标未写入 / 事件未处理 / 呈现未完成」。收集自身异常降级为
  // 字段占位，绝不覆盖原始断言错误。
  const info = test.info();
  const snapshot: Record<string, unknown> = {
    phase,
    test: info.title,
    retry: info.retry,
    repeatEachIndex: info.repeatEachIndex,
    workerIndex: info.workerIndex,
    shard: info.config.shard,
    targetFrame: frame,
    requestedRangeValue: value,
  };
  const capture = async (name: string, read: () => Promise<unknown>) => {
    try {
      snapshot[name] = await read();
    } catch (error) {
      snapshot[name] = `unavailable: ${String(error)}`;
    }
  };
  await capture("selectedFrame", () => stage(page).getAttribute("data-video-frame-index"));
  await capture("paintedFrame", () => stage(page).getAttribute("data-video-painted-frame-index"));
  await capture("frameSource", () => stage(page).getAttribute("data-video-frame-source"));
  await capture("preciseState", () => stage(page).getAttribute("data-video-precise-state"));
  await capture("timelineWindow", () => readTimeline(page));
  await capture("rangeInput", () =>
    page.getByLabel("视频帧时间轴", { exact: true }).evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return {
        value: (node as HTMLInputElement).value,
        pointerEvents: getComputedStyle(node).pointerEvents,
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      };
    }),
  );
  return snapshot;
}

async function seek(page: Page, frame: number) {
  const invalid = invalidSeekTarget(frame);
  if (invalid) throw new Error(invalid);
  await test.step(`定位视频时间轴到 F${frame}`, async () => {
    await key(page, "k");
    // 建立包含目标的有效窗口；缩放复位后等窗口状态实际更新再复用，
    // 不沿用更新之前的窗口数据。零跨度（单帧素材）显式拒绝。
    const ensureWindow = async (forceReset = false) => {
      let current = await readTimeline(page);
      if (!forceReset && frame >= current.from && frame <= current.to) return current;
      await expandTimeline(page);
      await page.getByTestId("video-timeline-zoom-reset").click();
      await expect
        .poll(
          async () => {
            current = await readTimeline(page);
            return frame >= current.from && frame <= current.to;
          },
          { timeout: 10_000 },
        )
        .toBe(true);
      return current;
    };
    let window = await ensureWindow();
    if (window.to - window.from <= 0)
      throw new Error(`时间轴窗口 [${window.from}, ${window.to}] 退化；本辅助函数只服务多帧素材`);
    // range 0..10000 的归一化粒度必须能精确还原目标帧（与 pctToFrame 反向映射
    // 一致）；本套件 180 帧素材在任意合法窗口下逐帧可表达，长素材不可表达时
    // 回全窗口复核，仍不可表达则立即失败，不静默接受近似帧。
    let mapping = rangeValueForFrame(frame, window);
    if (!mapping.expressible) {
      window = await ensureWindow(true);
      mapping = rangeValueForFrame(frame, window);
    }
    if (!mapping.expressible)
      throw new Error(
        `目标帧 ${frame} 在时间轴窗口 [${window.from}, ${window.to}] 无法经 range 归一化` +
          `精确表达（value=${mapping.value} 反解为 ${mapping.representedFrame}）；请缩小时间轴窗口后重试`,
      );
    const input = page.getByLabel("视频帧时间轴", { exact: true });
    await input.scrollIntoViewIfNeeded();
    let phase = "request-target";
    try {
      await test.step(`经 range 输入通路写入 value=${mapping.value}`, async () => {
        // 应用既有输入路径：range onChange → onSeek。不写内部状态，也不依赖
        // 播放浮层可交互（fill 无命中检查，浮层隐藏时裸坐标点击会被静默吞掉）。
        await input.fill(String(mapping.value));
        await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(frame));
      });
      phase = "paint-settled";
      // 先断言选择帧到达目标（上一步），再等绘制帧追上；最后复核选择帧未被
      // 迟到结果覆盖——应用层 Issue #114 单调栅栏保留为冗余防护。
      await test.step("确认呈现完成且选择帧未被迟到结果覆盖", async () => {
        await expectPaintSettled(page);
        await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(frame));
      });
    } catch (error) {
      await test.info().attach("seek-navigation-state", {
        contentType: "application/json",
        body: JSON.stringify(
          await seekNavigationSnapshot(page, frame, mapping.value, phase),
          null,
          2,
        ),
      });
      throw error;
    }
  });
}

async function revealFab(page: Page) {
  const size = page.viewportSize()!;
  await page.mouse.move(size.width - 12, size.height - 12);
}

async function arm(page: Page) {
  await revealFab(page);
  await page.getByTestId("issue-pin-fab").click();
  await expect(page.getByTestId("issue-pin-fab")).toHaveAttribute("data-armed", "true");
}

async function clickPoint(page: Page, point: Point) {
  const bounds = await videoMediaBounds(page);
  await page.evaluate(() => {
    const target = window as unknown as { __g2PointerReceipt?: { x: number; y: number } };
    delete target.__g2PointerReceipt;
    document.addEventListener(
      "click",
      (event) => {
        target.__g2PointerReceipt = { x: event.clientX, y: event.clientY };
      },
      { capture: true, once: true },
    );
  });
  await page.mouse.click(bounds.x + bounds.width * point[0], bounds.y + bounds.height * point[1]);
  const receipt = await page.evaluate(() => {
    const target = window as unknown as { __g2PointerReceipt?: { x: number; y: number } };
    const value = target.__g2PointerReceipt;
    delete target.__g2PointerReceipt;
    return value;
  });
  expect(receipt).toBeDefined();
  const normalized: Point = [
    (receipt!.x - bounds.x) / bounds.width,
    (receipt!.y - bounds.y) / bounds.height,
  ];
  // Browser input is quantized to screen pixels; preserve the actual clicked
  // point. The read-only location summary rounds only its display.
  expect(Math.abs(normalized[0] - point[0])).toBeLessThanOrEqual(1.1 / bounds.width);
  expect(Math.abs(normalized[1] - point[1])).toBeLessThanOrEqual(1.1 / bounds.height);
  return { x: normalized[0], y: normalized[1] };
}

async function videoMediaBounds(page: Page) {
  return stage(page).evaluate((element) => {
    const content = element.querySelector<HTMLElement>(".konvajs-content");
    if (!content) throw new Error("Konva content bounds unavailable");
    const media = {
      x: Number(element.getAttribute("data-media-x")),
      y: Number(element.getAttribute("data-media-y")),
      width: Number(element.getAttribute("data-media-width")),
      height: Number(element.getAttribute("data-media-height")),
    };
    if (!Object.values(media).every(Number.isFinite) || media.width <= 0 || media.height <= 0)
      throw new Error("Video media transform unavailable");
    // Konva maps browser input through DOM scale, including fractional CSS
    // dimensions whose clientWidth/clientHeight are rounded to integers.
    const contentBounds = content.getBoundingClientRect();
    const scaleX = contentBounds.width / content.clientWidth;
    const scaleY = contentBounds.height / content.clientHeight;
    return {
      x: contentBounds.left + media.x * scaleX,
      y: contentBounds.top + media.y * scaleY,
      width: media.width * scaleX,
      height: media.height * scaleY,
    };
  });
}

async function expectReady(page: Page, fixture: IssueCase, frame: number) {
  await expect(navigation(page)).toHaveAttribute("data-status", "ready", { timeout: 12_000 });
  await expect(navigation(page)).toHaveAttribute("data-frame-index", String(frame));
  await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(frame));
  // No capability skip: both WebCodecs and native fallback must display the real target pixels.
  await expectVideoContextFramePixels(page, fixture.expectations, frame);
  fixture.evidence.push({
    frame,
    pixels: await sampleVideoContextFrameMarkers(page, fixture.expectations),
    presentation: await stage(page).evaluate((node) => ({
      source: node.getAttribute("data-video-frame-source"),
      preciseState: node.getAttribute("data-video-precise-state"),
      paintedFrame: node.getAttribute("data-video-painted-frame-index"),
    })),
    diagnostics: await page.evaluate(() => {
      const store = (
        window as unknown as {
          __videoWorkbenchDiagnostics?: {
            activeTaskId?: string;
            byTask?: Record<string, { preciseFrame?: unknown }>;
          };
        }
      ).__videoWorkbenchDiagnostics;
      return store?.activeTaskId
        ? (store.byTask?.[store.activeTaskId]?.preciseFrame ?? null)
        : null;
    }),
  });
}

async function dropReady(page: Page, fixture: IssueCase, frame: number, point: Point = [0.5, 0.5]) {
  await arm(page);
  const anchor = await clickPoint(page, point);
  await expectReady(page, fixture, frame);
  await expect(modal(page)).toBeVisible();
  await expect(page.getByTestId("issue-create-frame")).toHaveText(`源帧 F ${frame}`);
  await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", true);
  return anchor;
}

async function listIssues(request: APIRequestContext, fixture: IssueCase, taskId = fixture.taskId) {
  const payload = await json<{ items: Issue[] }>(
    await request.get(`${API_BASE}/api/v1/feedbacks`, {
      headers: auth(fixture.token),
      params: { project_id: fixture.data.project_id, task_id: taskId, kind: "issue" },
    }),
  );
  return payload.items;
}

async function saveIssue(page: Page, fixture: IssueCase, body = `G2 context ${randomUUID()}`) {
  await modal(page).getByPlaceholder("描述问题位置 / 现象 / 期望行为").fill(body);
  const response = page.waitForResponse(
    (candidate) =>
      pathOf(candidate.url()) === "/api/v1/feedbacks" && candidate.request().method() === "POST",
  );
  await modal(page).getByRole("button", { name: "提交", exact: true }).click();
  const saved = await response;
  expect(saved.ok(), await saved.text()).toBe(true);
  const result = (await saved.json()) as Issue;
  expect(result.body).toBe(body);
  expect(result.task_id).toBe(fixture.taskId);
  await expect(modal(page)).toBeHidden();
  fixture.evidence.push({ savedIssue: result });
  return result;
}

async function createIssue(
  request: APIRequestContext,
  fixture: IssueCase,
  frame: number,
  point: Point = [0.5, 0.5],
  options: { taskId?: string; context?: VideoContext; annotationId?: string } = {},
) {
  return json<Issue>(
    await request.post(`${API_BASE}/api/v1/feedbacks`, {
      headers: auth(fixture.token),
      data: {
        kind: "issue",
        anchor_type: "pixel",
        project_id: fixture.data.project_id,
        task_id: options.taskId ?? fixture.taskId,
        ...(options.annotationId ? { annotation_id: options.annotationId } : {}),
        anchor_position: {
          x: point[0],
          y: point[1],
          frame,
          ...(options.context ? { video_context: options.context } : {}),
        },
        severity: "warn",
        body: `G2 persisted navigation F${frame} ${randomUUID()}`,
      },
    }),
  );
}

async function openIssues(page: Page) {
  await revealFab(page);
  await page.getByTestId("issue-fab").click();
  await expect(page.getByRole("tab", { name: /^问题/ })).toHaveAttribute("aria-selected", "true");
}

async function holdMedia(page: Page, fixture: IssueCase) {
  fixture.mediaLatency = true;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  fixture.releaseMedia.push(release);
  const fetched: Array<{ path: string; status: number; bytes: number }> = [];
  const deliveries: Promise<void>[] = [];
  const handler = async (route: Route) => {
    const delivery = (async () => {
      const response = await route.fetch();
      const bytes = (await response.body()).byteLength;
      fetched.push({ path: pathOf(route.request().url()), status: response.status(), bytes });
      expect(response.ok()).toBe(true);
      expect(bytes).toBeGreaterThan(100);
      await gate;
      // Only latency is injected; status, headers and bytes are the actual storage response.
      await route.fulfill({ response });
    })();
    deliveries.push(delivery);
    await delivery;
  };
  const matcher = (url: URL) => isFixtureMedia(url, fixture.fixtureName);
  await page.route(matcher, handler);
  return {
    release,
    async fetched() {
      await expect.poll(() => fetched.length).toBeGreaterThan(0);
      return fetched;
    },
    async finish() {
      release();
      await Promise.all(deliveries);
      await page.unroute(matcher, handler);
      fixture.evidence.push({
        faultInjection: "delay actual MP4 delivery after route.fetch",
        mediaResponses: fetched,
      });
    },
  };
}

async function observeNavigation(page: Page) {
  await page.evaluate(() => {
    const entries: Array<{ status: string; frame: string | null }> = [];
    const record = () => {
      const node = document.querySelector('[data-testid="issue-frame-navigation"]');
      const entry = {
        status: node?.getAttribute("data-status") ?? "idle",
        frame: node?.getAttribute("data-frame-index") ?? null,
      };
      if (JSON.stringify(entry) !== JSON.stringify(entries.at(-1))) entries.push(entry);
    };
    const observer = new MutationObserver(record);
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["data-status", "data-frame-index"],
    });
    // This is observational telemetry only; it never calls application state or dispatches events.
    Object.defineProperty(window, "__g2NavigationObservation", {
      value: { entries, observer },
      configurable: true,
    });
    record();
  });
}

async function finishNavigationObservation(page: Page) {
  return page.evaluate(() => {
    const observation = (
      window as unknown as {
        __g2NavigationObservation: {
          entries: Array<{ status: string; frame: string | null }>;
          observer: MutationObserver;
        };
      }
    ).__g2NavigationObservation;
    observation.observer.disconnect();
    return observation.entries;
  });
}

async function readViewport(page: Page): Promise<Viewport> {
  await expect(stage(page)).toHaveAttribute("data-video-view-ready", "true");
  return stage(page).evaluate((node) => ({
    center_x: Number(node.getAttribute("data-video-view-center-x")),
    center_y: Number(node.getAttribute("data-video-view-center-y")),
    zoom: Number(node.getAttribute("data-video-view-zoom")),
  }));
}

async function readTimeline(page: Page): Promise<TimelineWindow> {
  const overlay = page.getByTestId("video-playback-overlay");
  await expect(overlay).toHaveAttribute("data-timeline-from", /\d/);
  return overlay.evaluate((node) => ({
    from: Number(node.getAttribute("data-timeline-from")),
    to: Number(node.getAttribute("data-timeline-to")),
  }));
}

async function expandTimeline(page: Page) {
  if (!(await page.getByTestId("video-timeline-details").isVisible()))
    await page.getByTestId("video-timeline-toggle").click();
  await expect(page.getByTestId("video-timeline-details")).toBeVisible();
}

async function capturedContext(page: Page): Promise<VideoContext> {
  return {
    schema_version: 1,
    viewport: await readViewport(page),
    timeline_window: await readTimeline(page),
  };
}

async function expectViewport(page: Page, expected: Viewport) {
  await expect(async () => {
    const actual = await readViewport(page);
    expect(actual.center_x).toBeCloseTo(expected.center_x, 5);
    expect(actual.center_y).toBeCloseTo(expected.center_y, 5);
    expect(actual.zoom).toBeCloseTo(expected.zoom, 5);
  }).toPass({ timeout: 5000 });
  // Independent geometry check: the same view must be applied to the rendered media transform.
  const rendered = await stage(page).evaluate((node) => {
    const content = node.querySelector<HTMLElement>(".konvajs-content")!;
    const rect = content.getBoundingClientRect();
    const width = Number(node.getAttribute("data-media-width"));
    const height = Number(node.getAttribute("data-media-height"));
    return {
      center_x: (rect.width / 2 - Number(node.getAttribute("data-media-x"))) / width,
      center_y: (rect.height / 2 - Number(node.getAttribute("data-media-y"))) / height,
      zoom: width / (160 * Math.min(rect.width / 160, rect.height / 120)),
    };
  });
  expect(rendered.center_x).toBeCloseTo(expected.center_x, 5);
  expect(rendered.center_y).toBeCloseTo(expected.center_y, 5);
  expect(rendered.zoom).toBeCloseTo(expected.zoom, 5);
}

async function expectContext(page: Page, context: VideoContext) {
  if (context.viewport) await expectViewport(page, context.viewport);
  if (context.timeline_window) {
    await expect(async () => {
      const window = await readTimeline(page);
      expect(window.from).toBeCloseTo(context.timeline_window!.from, 5);
      expect(window.to).toBeCloseTo(context.timeline_window!.to, 5);
    }).toPass({ timeout: 5000 });
  }
}

async function changeView(page: Page, steps = 2) {
  const before = await readViewport(page);
  const box = (await stage(page).locator(".konvajs-content").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height * 0.4);
  await page.keyboard.down("Control");
  try {
    for (let i = 0; i < steps; i += 1) {
      const zoom = (await readViewport(page)).zoom;
      await page.mouse.wheel(0, -120);
      await expect.poll(async () => (await readViewport(page)).zoom).toBeGreaterThan(zoom);
    }
  } finally {
    await page.keyboard.up("Control");
  }
  await page.mouse.move(box.x + box.width / 2, box.y + box.height * 0.35);
  await page.mouse.down({ button: "right" });
  await page.mouse.move(box.x + box.width / 2 + 24, box.y + box.height * 0.35 - 12, { steps: 6 });
  await page.mouse.up({ button: "right" });
  expect((await readViewport(page)).zoom).toBeGreaterThan(before.zoom);
  await expandTimeline(page);
  await page.getByTestId("video-timeline-zoom-in").click();
  expect((await readTimeline(page)).to - (await readTimeline(page)).from).toBeLessThan(179);
}

function trackRow(page: Page, id: string) {
  return page.getByTestId("video-track-row").and(page.locator(`[data-annotation-id="${id}"]`));
}

async function createTrack(
  request: APIRequestContext,
  fixture: IssueCase,
  name: string,
  outside = false,
) {
  return json<Track>(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: auth(fixture.token),
      data: {
        annotation_type: "video_track_bbox",
        tool_unit_id: "bbox",
        class_name: "car",
        geometry: {
          type: "video_track_bbox",
          track_id: name,
          keyframes: [0, 179].map((frame_index) => ({
            frame_index,
            source: "manual",
            bbox: { x: 0.72, y: 0.2, w: 0.14, h: 0.18 },
          })),
          outside: outside ? [{ from: 120, to: 140 }] : [],
        },
      },
    }),
  );
}

async function annotations(request: APIRequestContext, fixture: IssueCase) {
  return json<Track[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: auth(fixture.token),
    }),
  );
}

async function createContextIssue(
  page: Page,
  fixture: IssueCase,
  frame: number,
  range?: [number, number],
) {
  await seek(page, frame);
  const context = await capturedContext(page);
  const point = await dropReady(page, fixture, frame, [0.47, 0.53]);
  if (range) {
    await page.getByTestId("issue-frame-range-enabled").check();
    await page.getByTestId("issue-frame-range-from").fill(String(range[0]));
    await page.getByTestId("issue-frame-range-to").fill(String(range[1]));
    context.frame_range = { from_frame: range[0], to_frame: range[1] };
  }
  const issue = await saveIssue(page, fixture);
  expect(issue.anchor_position).toMatchObject({ frame, video_context: context });
  expect(issue.anchor_position!.x).toBeCloseTo(point.x, 12);
  expect(issue.anchor_position!.y).toBeCloseTo(point.y, 12);
  return issue;
}

async function collapseObjectCard(page: Page) {
  const collapse = page
    .locator("section[data-floating-panel]")
    .filter({
      has: page.getByText("选中对象", { exact: true }),
    })
    .getByRole("button", { name: "收起浮窗", exact: true });
  if (await collapse.isVisible()) {
    await collapse.click();
    await expect(collapse).toBeHidden();
  }
}

async function locate(page: Page, fixture: IssueCase, issue: Issue, objectChanged = false) {
  if (issue.task_id !== fixture.taskId)
    fixture.navigatedTaskIds.push(fixture.taskId, issue.task_id);
  await collapseObjectCard(page);
  const before = await readViewport(page);
  await openIssues(page);
  await page.getByTestId(`discussion-issue-locate-${issue.id}`).click();
  await expect(page).toHaveURL(new RegExp(`task=${issue.task_id}(?:&|$)`));
  await expectReady(page, fixture, issue.anchor_position!.frame!);
  const context = issue.anchor_position!.video_context;
  if (context?.schema_version === 1) {
    if (objectChanged) {
      await expectViewport(page, {
        ...before,
        center_x: issue.anchor_position!.x,
        center_y: issue.anchor_position!.y,
      });
      await expectContext(page, { schema_version: 1, timeline_window: context.timeline_window });
    } else await expectContext(page, context);
  }
}

async function createPolygonDraft(page: Page) {
  const polygon = page.getByTestId("video-tool-btn-polygon");
  if (await polygon.isVisible()) await polygon.click();
  else {
    await page.getByTestId("tool-dock-more").click();
    await page.getByTestId("tool-overflow-item-polygon").click();
  }
  await expect(polygon).toHaveAttribute("aria-pressed", "true");
  for (const point of [
    [0.4, 0.4],
    [0.55, 0.4],
  ] as Point[]) {
    await clickPoint(page, point);
    // Separate actual clicks beyond Konva's double-click interval.
    await page.waitForTimeout(450);
  }
  await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "2");
}

test.describe("video Issue persisted context", () => {
  test.setTimeout(180_000);
  test.use({ actionTimeout: 10_000 });

  for (const allowSwitch of [false, true])
    test(`浏览器返回讨论链接前确认视频草稿：${allowSwitch ? "允许后打开目标" : "拒绝后原任务帧和笔触保持不变"}`, async ({
      page,
      request,
      seed,
      issueCase: fixture,
    }) => {
      const issue = await createIssue(request, fixture, 33, [0.4, 0.5]);
      const other = await seed.videoWebCodecs(fixture.data.project_id, { fixture: MAIN_FIXTURE });
      const otherTask = await json<{ display_id: string }>(
        await request.get(`${API_BASE}/api/v1/tasks/${other.task_id}`, {
          headers: auth(fixture.token),
        }),
      );
      await page.goto(
        `/projects/${fixture.data.project_id}/annotate?task=${fixture.taskId}&discussion=issues&issue=${issue.id}`,
      );
      await expect(stage(page)).toBeVisible({ timeout: 25_000 });
      await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
        "data-issue-id",
        issue.id,
      );
      await layoutCommand(page, "标准标注布局");
      fixture.navigatedTaskIds.push(fixture.taskId, other.task_id);
      await page
        .getByRole("tabpanel", { name: "任务队列", exact: true })
        .getByText(otherTask.display_id, { exact: true })
        .click();
      await expect(page).toHaveURL(new RegExp(`task=${other.task_id}(?:&|$)`));
      expect(new URL(page.url()).searchParams.has("discussion")).toBe(false);
      const previousUrl = page.url();
      await seek(page, 3);
      await createPolygonDraft(page);
      await page.goBack();
      const dialog = page.getByRole("alertdialog").filter({ hasText: "继续绘制" });
      await expect(dialog).toBeVisible();
      await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
      await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "2");
      if (allowSwitch) {
        await dialog.getByRole("button", { name: "丢弃并切换", exact: true }).click();
        await expect(page).toHaveURL(new RegExp(`task=${fixture.taskId}(?:&|$)`));
        await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
          "data-issue-id",
          issue.id,
        );
        await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "0");
        expect(await annotations(request, fixture)).toEqual([]);
        return;
      }
      await dialog.getByRole("button", { name: "继续绘制", exact: true }).click();
      await expect(page).toHaveURL(previousUrl);
      await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
      await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "2");
      await expect(page.getByTestId("discussion-issue-detail")).toHaveCount(0);
      expect(await annotations(request, fixture)).toEqual([]);
    });

  test("讨论输入区 Enter 发送留言但保留视频多边形草稿", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    await open(page, fixture);
    await seek(page, 33);
    await createPolygonDraft(page);
    await clickPoint(page, [0.53, 0.56]);
    await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "3");
    await page.getByRole("tab", { name: /^评论/ }).click();
    const input = page.getByRole("textbox", { name: "留言", exact: true });
    await input.fill("先确认问题，保留正在绘制的多边形");
    const sent = page.waitForResponse(
      (response) =>
        pathOf(response.url()) === "/api/v1/feedbacks" && response.request().method() === "POST",
    );
    await input.press("Enter");
    const response = await sent;
    expect(response.ok(), await response.text()).toBe(true);
    expect(await response.json()).toMatchObject({
      kind: "comment",
      task_id: fixture.taskId,
      body: "先确认问题，保留正在绘制的多边形",
    });
    await expect(input).toBeEmpty();
    await expect(page.getByTestId("class-picker-popover")).toHaveCount(0);
    await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "3");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "33");
    expect(await annotations(request, fixture)).toEqual([]);
  });

  test("切换视频任务再重开问题页签，不恢复上一任务的旧详情", async ({
    page,
    request,
    seed,
    issueCase: fixture,
  }) => {
    const originalIssue = await createIssue(request, fixture, 33, [0.4, 0.5]);
    const other = await seed.videoWebCodecs(fixture.data.project_id, { fixture: MAIN_FIXTURE });
    const otherTask = await json<{ display_id: string }>(
      await request.get(`${API_BASE}/api/v1/tasks/${other.task_id}`, {
        headers: auth(fixture.token),
      }),
    );
    await open(page, fixture);
    await seek(page, 3);
    await openIssues(page);
    await page.getByTestId(`discussion-issue-open-${originalIssue.id}`).click();
    await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
      "data-issue-id",
      originalIssue.id,
    );
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
    fixture.navigatedTaskIds.push(fixture.taskId, other.task_id);
    await page
      .getByRole("tabpanel", { name: "任务队列", exact: true })
      .getByText(otherTask.display_id, { exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`task=${other.task_id}(?:&|$)`));
    await expect(page.getByTestId("issue-open-count")).toHaveText("待处理 0");
    await page.getByRole("tab", { name: "历史", exact: true }).click();
    // Use the tab itself: the FAB intentionally starts a fresh list request
    // and would clear the stale activation, masking this remount regression.
    await page.getByRole("tab", { name: /^问题/ }).click();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    await expect(page.getByTestId("issue-open-count")).toHaveText("待处理 0");
    await expect(page.getByTestId("discussion-issue-detail")).toHaveCount(0);

    // An explicit project-scope activation remains available in the new task,
    // and opening it still does not move the canvas or switch the task.
    await page.getByTestId("issue-list-scope").selectOption("project");
    await page.getByTestId(`discussion-issue-open-${originalIssue.id}`).click();
    await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
      "data-issue-id",
      originalIssue.id,
    );
    await expect(page).toHaveURL(new RegExp(`task=${other.task_id}(?:&|$)`));
  });

  test("G2-1 十个单帧/范围Issue通过真实表单持久化，F120–F160保持单条记录，刷新逐项恢复", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    await open(page, fixture);
    const issues: Issue[] = [];
    for (const frame of [3, 17, 33, 63, 93, 120, 129, 143, 159, 173])
      issues.push(
        await createContextIssue(page, fixture, frame, frame === 120 ? [120, 160] : undefined),
      );
    expect(fixture.writes).toHaveLength(10);
    const persisted = await listIssues(request, fixture);
    expect(persisted).toHaveLength(10);
    for (const issue of issues)
      expect(persisted.find((entry) => entry.id === issue.id)).toEqual(issue);
    expect(await annotations(request, fixture)).toEqual([]);
    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await expectPaintSettled(page);
    await openIssues(page);
    const range = issues.find((issue) => issue.anchor_position?.frame === 120)!;
    await expect(page.getByTestId(`discussion-issue-card-${range.id}`)).toContainText("F120–F160");
    await expect(page.locator('[data-testid^="discussion-issue-card-"]')).toHaveCount(10);
    for (const issue of issues) await locate(page, fixture, issue);
    expect(await listIssues(request, fixture)).toHaveLength(10);
    expect(fixture.writes).toHaveLength(10);
    await test.info().attach("ten-context-Issues", {
      contentType: "image/png",
      body: await page.screenshot({ fullPage: true }),
    });
  });

  test("G2-2 outside原始对象、归一化缩放平移和时间窗跨刷新/resize恢复，稍后自动聚焦不覆盖Issue视图", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const original = await createTrack(request, fixture, "g2-outside-target", true);
    const other = await createTrack(request, fixture, "g2-other-object");
    await open(page, fixture);
    await page
      .getByTestId("ai-inspector-panel")
      .getByRole("button", { name: "全部", exact: true })
      .click();
    await trackRow(page, original.id).click();
    await changeView(page);
    const issue = await createContextIssue(page, fixture, 120, [120, 160]);
    const context = issue.anchor_position!.video_context!;
    expect(issue.annotation_id).toBe(original.id);
    expect(context).toMatchObject({
      track_id: original.geometry.track_id,
      annotation_version: original.version,
    });
    await expect(page.getByTestId("video-track-context-state")).toContainText("outside");
    await collapseObjectCard(page);
    await trackRow(page, other.id).click();
    await changeView(page, 1);
    await locate(page, fixture, issue);
    await expect(trackRow(page, original.id)).toHaveAttribute("aria-selected", "true");
    await expect(trackRow(page, other.id)).toHaveAttribute("aria-selected", "false");
    await expect(page.getByTestId("video-track-context-state")).toContainText("outside");

    await page.setViewportSize({ width: 1180, height: 820 });
    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await expectPaintSettled(page);
    await page
      .getByTestId("ai-inspector-panel")
      .getByRole("button", { name: "全部", exact: true })
      .click();
    await locate(page, fixture, issue);
    await expect(trackRow(page, original.id)).toHaveAttribute("aria-selected", "true");
    // A second native seek finishes after selection/render effects and would reveal a late auto-focus overwrite.
    await seek(page, 121);
    await expectVideoContextFramePixels(page, fixture.expectations, 121);
    await expectContext(page, context);
    await locate(page, fixture, issue);
    expect((await listIssues(request, fixture)).find((entry) => entry.id === issue.id)).toEqual(
      issue,
    );
    expect(await annotations(request, fixture)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: original.id, version: original.version }),
        expect.objectContaining({ id: other.id, version: other.version }),
      ]),
    );
    fixture.evidence.push({
      savedContext: context,
      restoredViewport: await readViewport(page),
      restoredTimeline: await readTimeline(page),
      viewportSize: page.viewportSize(),
    });
  });

  test("G2-3 对象改版和删除后仍恢复原帧/像素区域并明确提示对象已变化", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const original = await createTrack(request, fixture, "g2-versioned-object");
    await open(page, fixture);
    await trackRow(page, original.id).click();
    await changeView(page);
    const issue = await createContextIssue(page, fixture, 129);
    expect(issue.annotation_id).toBe(original.id);
    expect(issue.anchor_position!.video_context!.annotation_version).toBe(original.version);
    const updated = await json<Track>(
      await request.patch(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${original.id}`, {
        headers: { ...auth(fixture.token), "If-Match": `W/"${original.version}"` },
        data: {
          geometry: {
            ...original.geometry,
            keyframes: original.geometry.keyframes.map((keyframe) => ({
              ...keyframe,
              bbox: { ...keyframe.bbox, x: 0.1 },
            })),
          },
        },
      }),
    );
    expect(updated.version).toBeGreaterThan(original.version);
    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await expectPaintSettled(page);
    await locate(page, fixture, issue, true);
    await expect(navigation(page)).toContainText("对象已变化");
    expect((await listIssues(request, fixture)).find((entry) => entry.id === issue.id)).toEqual(
      issue,
    );
    const removed = await request.delete(
      `${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${original.id}`,
      { headers: { ...auth(fixture.token), "If-Match": `W/"${updated.version}"` } },
    );
    expect(removed.status(), await removed.text()).toBe(204);
    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await expectPaintSettled(page);
    await locate(page, fixture, issue, true);
    await expect(navigation(page)).toContainText("对象已变化");
    await expect(trackRow(page, original.id)).toHaveCount(0);
    expect((await listIssues(request, fixture)).find((entry) => entry.id === issue.id)).toEqual(
      issue,
    );
    expect(await annotations(request, fixture)).toEqual([]);
    expect(fixture.writes).toHaveLength(1);
  });

  test("G2-4 旧锚点和未知版本历史上下文可读并降级，公开API拒绝非法/越界V1上下文", async ({
    page,
    request,
    seed,
    issueCase: fixture,
  }) => {
    const legacy = await createIssue(request, fixture, 33, [0.46, 0.54]);
    const future = await createIssue(request, fixture, 143, [0.49, 0.51]);
    const historical = await seed.videoIssueContextHistory(future.id);
    expect(historical.anchor_position).toMatchObject({
      x: 0.49,
      y: 0.51,
      frame: 143,
      video_context: { schema_version: 999 },
    });
    const cases = [
      { schema_version: 999 },
      { schema_version: 1, viewport: { center_x: 0.5, center_y: 0.5, zoom: 0 } },
      { schema_version: 1, viewport: { center_x: 0.5, zoom: 2 } },
      { schema_version: 1, frame_range: { from_frame: 120, to_frame: 180 } },
      { schema_version: 1, frame_range: { from_frame: 121, to_frame: 160 } },
      { schema_version: 1, frame_range: { from_frame: true, to_frame: 160 } },
      { schema_version: 1, timeline_window: { from: 0, to: 180 } },
      { schema_version: 1, annotation_version: 1 },
    ];
    for (const context of cases) {
      const response = await request.post(`${API_BASE}/api/v1/feedbacks`, {
        headers: auth(fixture.token),
        data: {
          kind: "issue",
          anchor_type: "pixel",
          project_id: fixture.data.project_id,
          task_id: fixture.taskId,
          anchor_position: { x: 0.5, y: 0.5, frame: 120, video_context: context },
          body: "G2 rejected schema fixture",
        },
      });
      expect(response.status(), await response.text()).toBe(422);
      fixture.evidence.push({
        rejectedContext: context,
        status: response.status(),
        detail: await response.json(),
      });
    }
    const persisted = await listIssues(request, fixture);
    expect(persisted).toHaveLength(2);
    const futureRead = persisted.find((issue) => issue.id === future.id)!;
    expect(futureRead.anchor_position).toEqual(historical.anchor_position);
    expect(legacy.anchor_position).not.toHaveProperty("video_context");
    await open(page, fixture);
    await changeView(page);
    const currentView = await readViewport(page);
    await locate(page, fixture, legacy);
    await expectViewport(page, currentView);
    await locate(page, fixture, futureRead);
    await expectViewport(page, currentView);
    await expect(navigation(page)).not.toContainText("对象已变化");
    expect(await annotations(request, fixture)).toEqual([]);
    expect(fixture.writes).toEqual([]);
  });

  test("G2-5 跨任务Issue先处理未完成多边形，拒绝时保留源帧、草稿和视图，续画保存后可定位", async ({
    page,
    request,
    seed,
    issueCase: fixture,
  }) => {
    const other = await seed.videoWebCodecs(fixture.data.project_id, { fixture: MAIN_FIXTURE });
    await expectSeededChunks(request, fixture.token, other);
    const target = await createIssue(request, fixture, 143, [0.47, 0.53], {
      taskId: other.task_id,
      context: {
        schema_version: 1,
        viewport: { center_x: 0.5, center_y: 0.5, zoom: 1.2 },
        timeline_window: { from: 110, to: 170 },
      },
    });
    await open(page, fixture);
    await seek(page, 3);
    await changeView(page, 2);
    const origin = await capturedContext(page);
    await openIssues(page);
    await page.getByTestId("issue-list-scope").selectOption("project");
    await createPolygonDraft(page);
    const originFrame = Number(await stage(page).getAttribute("data-video-frame-index"));
    await page.getByTestId(`discussion-issue-locate-${target.id}`).click();
    const dialog = page.getByRole("alertdialog").filter({ hasText: "继续绘制" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "继续绘制", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator("body")).toHaveCSS("pointer-events", "auto");
    await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "2");
    await expect(page).toHaveURL(new RegExp(`task=${fixture.taskId}(?:&|$)`));
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(originFrame));
    await expectContext(page, origin);
    await expectVideoContextFramePixels(page, fixture.expectations, originFrame);
    expect(await annotations(request, fixture)).toEqual([]);
    await clickPoint(page, [0.53, 0.56]);
    await expect(stage(page)).toHaveAttribute("data-video-draft-point-count", "3");
    await page.waitForTimeout(450);
    await key(page, "Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeVisible();
    const committed = page.waitForResponse(
      (response) =>
        pathOf(response.url()) === `/api/v1/tasks/${fixture.taskId}/annotations` &&
        response.request().method() === "POST",
    );
    await page
      .getByTestId("class-picker-popover")
      .locator("span")
      .filter({ hasText: /^car$/ })
      .click();
    const response = await committed;
    expect(response.ok(), await response.text()).toBe(true);
    const polygon = (await response.json()) as {
      geometry: { frame_index: number; points: number[][] };
    };
    expect(polygon.geometry.frame_index).toBe(originFrame);
    expect(polygon.geometry.points).toHaveLength(3);
    await locate(page, fixture, target);
    fixture.evidence.push({
      originContext: origin,
      continuedPolygon: polygon,
      targetIssue: target,
    });
  });

  test("G2-5 故障注入：真实MP4延迟期间跨任务最后点击胜出，超时重试恢复该Issue上下文", async ({
    page,
    request,
    seed,
    issueCase: fixture,
  }) => {
    const other = await seed.videoWebCodecs(fixture.data.project_id, { fixture: MAIN_FIXTURE });
    await expectSeededChunks(request, fixture.token, other);
    const contextA: VideoContext = {
      schema_version: 1,
      viewport: { center_x: 0.48, center_y: 0.51, zoom: 1.1 },
      timeline_window: { from: 10, to: 70 },
    };
    const contextB: VideoContext = {
      schema_version: 1,
      viewport: { center_x: 0.52, center_y: 0.49, zoom: 1.2 },
      timeline_window: { from: 110, to: 170 },
    };
    const issueA = await createIssue(request, fixture, 33, [0.47, 0.53], { context: contextA });
    const issueB = await createIssue(request, fixture, 143, [0.47, 0.53], {
      taskId: other.task_id,
      context: contextB,
    });
    const held = await holdMedia(page, fixture);
    await open(page, fixture);
    await held.fetched();
    await openIssues(page);
    await page.getByTestId("issue-list-scope").selectOption("project");
    await observeNavigation(page);
    await page.getByTestId(`discussion-issue-locate-${issueB.id}`).click();
    await expect(page).toHaveURL(new RegExp(`task=${other.task_id}(?:&|$)`));
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await page.getByTestId(`discussion-issue-locate-${issueA.id}`).click();
    await expect(page).toHaveURL(new RegExp(`task=${fixture.taskId}(?:&|$)`));
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "33");
    await held.finish();
    await expectReady(page, fixture, 33);
    await expectContext(page, contextA);
    const transitions = await finishNavigationObservation(page);
    expect(
      transitions.filter((entry) => entry.status === "ready").map((entry) => entry.frame),
    ).toEqual(["33"]);
    fixture.evidence.push({ latestCrossTaskNavigation: transitions });

    const timeoutMedia = await holdMedia(page, fixture);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await timeoutMedia.fetched();
    await openIssues(page);
    await page.getByTestId("issue-list-scope").selectOption("project");
    await page.getByTestId(`discussion-issue-locate-${issueB.id}`).click();
    await expect(page).toHaveURL(new RegExp(`task=${other.task_id}(?:&|$)`));
    await expect(navigation(page)).toHaveAttribute("data-status", "timeout", { timeout: 12_000 });
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "143");
    await expect(modal(page)).toBeHidden();
    await timeoutMedia.finish();
    await page.getByTestId("issue-frame-retry").click();
    await expectReady(page, fixture, 143);
    await expectContext(page, contextB);
    expect((await listIssues(request, fixture))[0]).toEqual(issueA);
    expect((await listIssues(request, fixture, other.task_id))[0]).toEqual(issueB);
    expect(fixture.writes).toEqual([]);
  });

  test("G2-5 真实标注员权限撤销后旧项目Issue卡片被404拒绝，当前任务现场不丢失且刷新列表不泄露", async ({
    page,
    request,
    seed,
    issueCase: fixture,
  }) => {
    const users = await json<Array<{ id: string; email: string }>>(
      await request.get(`${API_BASE}/api/v1/users`, { headers: auth(fixture.token) }),
    );
    const annotator = users.find((user) => user.email === fixture.data.annotator_email)!;
    // Authority now comes from project membership: invite a second employee who
    // accepts an annotator responsibility in this project through the real
    // invitation → registration path, then hand the batch to them. The actor is
    // namespace-scoped (`takeover-<namespace>@e2e.test`) so the exact owned
    // cleanup removes it instead of leaking a shared global account.
    const takeoverEmail = fixture.data.admin_email.replace(/^admin-/, "takeover-");
    const invitation = await json<{ token: string }>(
      await request.post(`${API_BASE}/api/v1/users/invite`, {
        headers: auth(fixture.token),
        data: {
          email: takeoverEmail,
          role: "employee",
          project_id: fixture.data.project_id,
          project_member_role: "annotator",
        },
      }),
    );
    const takeover = await json<{ user: { id: string } }>(
      await request.post(`${API_BASE}/api/v1/auth/register`, {
        data: {
          token: invitation.token,
          name: "E2E Takeover",
          password: "Takeover-123",
        },
      }),
    );
    const batch = await json<{ id: string }>(
      await request.post(`${API_BASE}/api/v1/projects/${fixture.data.project_id}/batches`, {
        headers: auth(fixture.token),
        data: { name: "G2 revocable video batch", annotator_id: annotator.id },
      }),
    );
    const target = await seed.videoWebCodecs(fixture.data.project_id, {
      fixture: MAIN_FIXTURE,
      batchId: batch.id,
    });
    await expectSeededChunks(request, fixture.token, target);
    for (const status of ["active", "annotating"])
      await json(
        await request.post(
          `${API_BASE}/api/v1/projects/${fixture.data.project_id}/batches/${batch.id}/transition`,
          { headers: auth(fixture.token), data: { target_status: status } },
        ),
      );
    const issue = await createIssue(request, fixture, 143, [0.47, 0.53], {
      taskId: target.task_id,
      context: {
        schema_version: 1,
        viewport: { center_x: 0.5, center_y: 0.5, zoom: 1.6 },
        timeline_window: { from: 110, to: 170 },
      },
    });
    fixture.actorChanged = true;
    await seed.injectToken(page, fixture.data.annotator_email);
    await open(page, fixture);
    await seek(page, 33);
    await changeView(page, 2);
    const originFrame = Number(await stage(page).getAttribute("data-video-frame-index"));
    const origin = await capturedContext(page);
    await openIssues(page);
    await page.getByTestId("issue-list-scope").selectOption("project");
    const card = page.getByTestId(`discussion-issue-card-${issue.id}`);
    await expect(card).toBeVisible();
    await json(
      await request.patch(
        `${API_BASE}/api/v1/projects/${fixture.data.project_id}/batches/${batch.id}`,
        { headers: auth(fixture.token), data: { annotator_id: takeover.user.id } },
      ),
    );
    fixture.expectedHttpErrors.push({
      path: `/api/v1/tasks/${target.task_id}`,
      status: 404,
      bodyIncludes: "Task not found",
    });
    await page.getByTestId(`discussion-issue-locate-${issue.id}`).click();
    await expect(navigation(page)).toContainText("无法访问问题所在任务");
    await expect(page).toHaveURL(new RegExp(`task=${fixture.taskId}(?:&|$)`));
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(originFrame));
    await expectContext(page, origin);
    await expectVideoContextFramePixels(page, fixture.expectations, originFrame);
    const annotatorToken = await seed.accessToken(fixture.data.annotator_email);
    const visible = await json<{ items: Issue[] }>(
      await request.get(`${API_BASE}/api/v1/feedbacks`, {
        headers: auth(annotatorToken),
        params: { project_id: fixture.data.project_id, kind: "issue" },
      }),
    );
    expect(visible.items.some((entry) => entry.id === issue.id)).toBe(false);
    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await expectPaintSettled(page);
    await openIssues(page);
    await page.getByTestId("issue-list-scope").selectOption("project");
    await expect(card).toHaveCount(0);
    expect((await listIssues(request, fixture, target.task_id))[0]).toEqual(issue);
    expect(fixture.writes).toEqual([]);
    fixture.evidence.push({
      revokedBatch: batch.id,
      inaccessibleTask: target.task_id,
      preservedContext: origin,
      visibleProjectIssueIds: visible.items.map((entry) => entry.id),
    });
  });
});
