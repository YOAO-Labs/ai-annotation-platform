import {
  isExpectedRequestAbort,
  videoWorkbenchAborts,
  type AbortAllowRule,
  type RequestErrorSignal,
} from "../helpers/request-errors";
import type {
  APIRequestContext,
  APIResponse,
  Browser,
  Page,
  Request,
  Route,
} from "@playwright/test";
import { randomUUID } from "node:crypto";

import { expect, test as base, type SeedData } from "../fixtures/seed";
import {
  expectVideoFramePixels,
  sampleFrameMarkers,
  type FrameExpectations,
} from "../fixtures/video-frame-pixels";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const MAIN_FIXTURE = "h264-main-bframes-gop30";
type Point = [number, number];
interface Issue {
  id: string;
  task_id: string;
  anchor_type: "pixel" | "task";
  anchor_position: { x: number; y: number; frame?: number } | null;
  body: string;
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
  releaseMedia: Array<() => void>;
}

const stage = (page: Page) => page.getByTestId("video-konva-stage");
const navigation = (page: Page) => page.getByTestId("issue-frame-navigation");
const modal = (page: Page) => page.getByRole("dialog").filter({ hasText: "标记问题 (Issue)" });
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
const pathOf = (url: string) => new URL(url).pathname;
const isFixtureMedia = (url: URL, fixture: string) =>
  url.pathname.endsWith(`/e2e/video/webcodecs/${fixture}/source.mp4`);

/** Fixture-specific cancellations beyond the shared workbench policy. */
function fixtureAbortRules(fixture: IssueCase): AbortAllowRule[] {
  if (!fixture.mediaLatency) return [];
  return [
    {
      method: "DELETE",
      path: /^\/api\/v1\/tasks\/[0-9a-f-]{36}\/lock$/,
      reason: "navigation may cancel the lock release while media is still decoding",
    },
    {
      method: "GET",
      path: new RegExp(`/e2e/video/webcodecs/${fixture.fixtureName}/source\\.mp4$`),
      reason: "retiring the task aborts in-flight fixture media fetches",
    },
  ];
}

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
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
      releaseMedia: [],
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
      const unexpected = errors.filter((error) => !expectedAborts.includes(error));
      fixture.evidence.push({ expectedFaults: expectedAborts, unexpectedErrors: unexpected });
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
        await testInfo.attach("video-issue-frame-evidence", {
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

async function seek(page: Page, frame: number) {
  await key(page, "k");
  let current = Number(await stage(page).getAttribute("data-video-frame-index"));
  while (current !== frame) {
    const forward = frame > current;
    current += forward ? 1 : -1;
    // Shift in the Workbench hotkey owner means one source frame, even with step=5.
    await key(page, forward ? "Shift+ArrowRight" : "Shift+ArrowLeft");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(current));
  }
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
    const target = window as unknown as { __g1PointerReceipt?: { x: number; y: number } };
    delete target.__g1PointerReceipt;
    document.addEventListener(
      "click",
      (event) => {
        target.__g1PointerReceipt = { x: event.clientX, y: event.clientY };
      },
      { capture: true, once: true },
    );
  });
  await page.mouse.click(bounds.x + bounds.width * point[0], bounds.y + bounds.height * point[1]);
  const receipt = await page.evaluate(() => {
    const target = window as unknown as { __g1PointerReceipt?: { x: number; y: number } };
    const value = target.__g1PointerReceipt;
    delete target.__g1PointerReceipt;
    return value;
  });
  expect(receipt).toBeDefined();
  const normalized: Point = [
    (receipt!.x - bounds.x) / bounds.width,
    (receipt!.y - bounds.y) / bounds.height,
  ];
  // The location summary rounds only its display; persistence retains the
  // actual clicked point after browser screen-pixel quantization.
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
  await expectVideoFramePixels(page, fixture.expectations, frame);
  fixture.evidence.push({
    frame,
    pixels: await sampleFrameMarkers(page, fixture.expectations.sample_regions),
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

async function saveIssue(page: Page, fixture: IssueCase, body = `G1 source frame ${randomUUID()}`) {
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
) {
  return json<Issue>(
    await request.post(`${API_BASE}/api/v1/feedbacks`, {
      headers: auth(fixture.token),
      data: {
        kind: "issue",
        anchor_type: "pixel",
        project_id: fixture.data.project_id,
        task_id: fixture.taskId,
        anchor_position: { x: point[0], y: point[1], frame },
        severity: "warn",
        body: `G1 persisted navigation F${frame} ${randomUUID()}`,
      },
    }),
  );
}

async function openIssues(page: Page) {
  await revealFab(page);
  await page.getByTestId("issue-fab").click();
  await expect(page.getByRole("tab", { name: /^问题/ })).toHaveAttribute("aria-selected", "true");
}

async function pinPixels(page: Page, point: Point) {
  return stage(page).evaluate((element, [x, y]) => {
    const media = {
      x: Number(element.getAttribute("data-media-x")),
      y: Number(element.getAttribute("data-media-y")),
      w: Number(element.getAttribute("data-media-width")),
      h: Number(element.getAttribute("data-media-height")),
    };
    return Array.from(element.querySelectorAll<HTMLCanvasElement>(".konvajs-content > canvas"))
      .slice(1)
      .map((canvas) => {
        const scaleX = canvas.width / canvas.clientWidth;
        const scaleY = canvas.height / canvas.clientHeight;
        // The pin has a fixed 8px screen radius; sample clear of its glyph and stroke at any zoom.
        const px = Math.round((media.x + media.w * x + 4) * scaleX);
        const py = Math.round((media.y + media.h * y) * scaleY);
        const rgba = [...canvas.getContext("2d")!.getImageData(px, py, 1, 1).data];
        return rgba;
      });
  }, point);
}

async function expectPin(page: Page, fixture: IssueCase, issue: Issue) {
  const point: Point = [issue.anchor_position!.x, issue.anchor_position!.y];
  await expect
    .poll(async () =>
      (await pinPixels(page, point)).some(
        ([r, g, b, alpha]) => alpha > 240 && r > 140 && g > 70 && b < 150,
      ),
    )
    .toBe(true);
  fixture.evidence.push({ pin: issue.id, point, overlayPixels: await pinPixels(page, point) });
  await page.getByRole("tab", { name: /^评论/ }).click();
  await expect(page.getByRole("tab", { name: /^问题/ })).toHaveAttribute("aria-selected", "false");
  await clickPoint(page, point);
  await expect(page.getByRole("tab", { name: /^问题/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
    "data-issue-id",
    issue.id,
  );
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
    Object.defineProperty(window, "__g1NavigationObservation", {
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
        __g1NavigationObservation: {
          entries: Array<{ status: string; frame: string | null }>;
          observer: MutationObserver;
        };
      }
    ).__g1NavigationObservation;
    observation.observer.disconnect();
    return observation.entries;
  });
}

test.describe("video Issue source-frame ownership", () => {
  test.setTimeout(120_000);

  test("G1-1/3 播放时落点模式暂停，F17离网格锚点刷新后由卡片、时间线和真实图钉定位", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    await open(page, fixture);
    await expect(async () => expectVideoFramePixels(page, fixture.expectations, 0)).toPass({
      timeout: 15_000,
    });
    await key(page, "Space");
    await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", false);
    await arm(page);
    await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", true);
    await page.getByTestId("issue-pin-fab").focus();
    await page.keyboard.press("Space");
    await expect(page.getByTestId("issue-pin-fab")).toHaveAttribute("data-armed", "false");
    await expect(page.getByTestId("video-konva-source")).toHaveJSProperty("paused", true);
    // The independent off-grid step makes the no-snap assertion deterministic under load.
    await seek(page, 17);
    const anchor = await dropReady(page, fixture, 17, [0.375, 0.625]);
    const issue = await saveIssue(page, fixture);
    expect(issue.anchor_position).toMatchObject({ frame: 17 });
    expect(issue.anchor_position!.x).toBeCloseTo(anchor.x, 12);
    expect(issue.anchor_position!.y).toBeCloseTo(anchor.y, 12);
    expect((await listIssues(request, fixture)).find((item) => item.id === issue.id)).toEqual(
      issue,
    );
    expect(
      await json<unknown[]>(
        await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
          headers: auth(fixture.token),
        }),
      ),
    ).toEqual([]);

    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await seek(page, 3);
    await openIssues(page);
    await page.getByTestId(`discussion-issue-open-${issue.id}`).click();
    await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
      "data-issue-id",
      issue.id,
    );
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
    await page.getByTestId(`discussion-issue-locate-${issue.id}`).click();
    await expectReady(page, fixture, 17);
    await expectPin(page, fixture, issue);
    await test.info().attach("persisted-F17-Issue-pin", {
      contentType: "image/png",
      body: await stage(page).screenshot(),
    });
    await seek(page, 3);
    await page.getByTestId("video-issue-marker").and(page.locator('[title="问题 · F 17"]')).click();
    await expectReady(page, fixture, 17);
    await expectPin(page, fixture, issue);
    expect((await listIssues(request, fixture)).find((item) => item.id === issue.id)).toEqual(
      issue,
    );
  });

  test("G1-3 显式任务问题不提供坐标，刷新后没有伪造帧锚点", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const isDiscussionLayoutWrite = (request: Request) =>
      request.method() === "PATCH" &&
      pathOf(request.url()) === "/api/v1/auth/me/preferences" &&
      request.postDataJSON()?.workbench?.layout?.workspace?.contexts?.["annotate:video"]?.snapshot
        ?.layout?.activeGroup === "discussion";
    const finalLayoutWrite = page.waitForRequest(isDiscussionLayoutWrite);
    // A prior successful save must not authorize reload while the final save is in flight.
    await page.route("**/api/v1/auth/me/preferences", async (route) => {
      if (!isDiscussionLayoutWrite(route.request())) return route.continue();
      const response = await route.fetch();
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      await route.fulfill({ response });
    });
    const layoutSaved = page.waitForResponse((response) =>
      isDiscussionLayoutWrite(response.request()),
    );
    await open(page, fixture);
    await seek(page, 3);
    await openIssues(page);
    await page.getByTestId("issue-create-task").click();
    await expect(modal(page)).toBeVisible();
    await expect(modal(page).getByPlaceholder("x (0-1)")).toHaveCount(0);
    await expect(modal(page).getByPlaceholder("y (0-1)")).toHaveCount(0);
    await expect(page.getByTestId("issue-create-frame")).toBeHidden();
    const issue = await saveIssue(page, fixture);
    expect(issue).toMatchObject({ anchor_type: "task", anchor_position: null });
    await finalLayoutWrite;
    // Opening Issues saves a later active group than initialization or the preset change.
    // Consume that response completely before reload can cancel its transport.
    const layoutResponse = await layoutSaved;
    expect(layoutResponse.ok()).toBe(true);
    expect(await layoutResponse.finished()).toBeNull();
    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await seek(page, 8);
    await openIssues(page);
    await page.getByTestId(`discussion-issue-open-${issue.id}`).click();
    await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
      "data-issue-id",
      issue.id,
    );
    await expect(page.getByTestId(`discussion-issue-locate-${issue.id}`)).toHaveCount(0);
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await expect(navigation(page)).toBeHidden();
    await expect(page.getByTestId("video-issue-marker")).toHaveCount(0);
    expect((await listIssues(request, fixture)).find((item) => item.id === issue.id)).toMatchObject(
      { anchor_type: "task", anchor_position: null },
    );
  });

  test("G1-3 延迟实际MP4仍可直接记录任务级Issue，迟到源帧不复活像素创建", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const held = await holdMedia(page, fixture);
    await open(page, fixture);
    await held.fetched();
    await seek(page, 3);
    await arm(page);
    await clickPoint(page, [0.4, 0.6]);
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await expect(modal(page)).toBeHidden();
    await observeNavigation(page);

    await openIssues(page);
    await page.getByTestId("issue-create-task").click();
    await expect(modal(page)).toBeVisible();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
    await expect(navigation(page)).toBeHidden();
    await expect(page.getByTestId("issue-pin-fab")).toHaveAttribute("data-armed", "false");
    await expect(modal(page).getByPlaceholder("x (0-1)")).toHaveCount(0);
    await expect(modal(page).getByPlaceholder("y (0-1)")).toHaveCount(0);
    await expect(modal(page)).toContainText("任务问题不绑定画布位置");
    await expect(page.getByTestId("issue-create-frame")).toBeHidden();
    // No held source response has been delivered: task-level feedback is independent of media.
    const issue = await saveIssue(page, fixture);
    expect(issue).toMatchObject({ anchor_type: "task", anchor_position: null });
    expect(fixture.writes).toHaveLength(1);
    expect(fixture.writes[0]).toMatchObject({ anchor_type: "task", anchor_position: null });
    expect((await listIssues(request, fixture)).find((item) => item.id === issue.id)).toMatchObject(
      { anchor_type: "task", anchor_position: null },
    );

    await held.finish();
    const returnedFrame = Number(await stage(page).getAttribute("data-video-frame-index"));
    await expect(async () =>
      expectVideoFramePixels(page, fixture.expectations, returnedFrame),
    ).toPass({ timeout: 15_000 });
    await expect(navigation(page)).toBeHidden();
    await expect(modal(page)).toBeHidden();
    const transitions = await finishNavigationObservation(page);
    expect(transitions.some((entry) => entry.status === "ready")).toBe(false);
    fixture.evidence.push({
      taskIssueSavedBeforeMediaRelease: issue,
      supersededPixelNavigation: transitions,
      returnedSourceFrame: returnedFrame,
    });

    await page.reload();
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await openIssues(page);
    await expect(page.getByTestId(`discussion-issue-card-${issue.id}`)).toContainText(issue.body);
    await expect(page.getByTestId("video-issue-marker")).toHaveCount(0);
    await expect(navigation(page)).toBeHidden();
    await expect(modal(page)).toBeHidden();
    const persisted = await listIssues(request, fixture);
    expect(persisted).toHaveLength(1);
    expect(persisted[0]).toMatchObject({
      id: issue.id,
      anchor_type: "task",
      anchor_position: null,
    });
    expect(fixture.writes).toHaveLength(1);
  });

  test("G1-4 多边形草稿上聚焦问题标记并按Enter，拒绝跳帧后续画原帧再重试", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const issue = await createIssue(request, fixture, 17);
    await open(page, fixture);
    await seek(page, 3);
    const polygon = page.getByTestId("video-tool-btn-polygon");
    if (await polygon.isVisible()) await polygon.click();
    else {
      await page.getByTestId("tool-dock-more").click();
      await page.getByTestId("tool-overflow-item-polygon").click();
    }
    await expect(polygon).toHaveAttribute("aria-pressed", "true");
    const points: Point[] = [
      [0.3, 0.3],
      [0.6, 0.3],
      [0.55, 0.6],
    ];
    for (const point of points.slice(0, 2)) {
      await clickPoint(page, point);
      await page.waitForTimeout(450);
    }
    const issueMarker = page
      .getByTestId("video-issue-marker")
      .and(page.locator('[title="问题 · F 17"]'));
    await issueMarker.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("alertdialog").filter({ hasText: "切换视频工具" });
    await expect(dialog).toBeVisible();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
    await expect(page.getByTestId("class-picker-popover")).toBeHidden();
    expect(
      await json<unknown[]>(
        await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
          headers: auth(fixture.token),
        }),
      ),
    ).toEqual([]);
    await dialog.getByRole("button", { name: "继续绘制", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(navigation(page)).toHaveAttribute("data-status", "cancelled");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "3");
    await expect(page.getByTestId("video-creation-scope-hint")).toHaveText("仅当前源帧");
    await expectVideoFramePixels(page, fixture.expectations, 3);
    await clickPoint(page, points[2]);
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
    const annotation = (await response.json()) as {
      id: string;
      geometry: { type: string; frame_index: number; points: Point[] };
    };
    expect(annotation.geometry).toMatchObject({ type: "video_polygon", frame_index: 3 });
    expect(annotation.geometry.points).toHaveLength(3);
    points.forEach((point, index) =>
      point.forEach((coordinate, axis) =>
        expect(annotation.geometry.points[index][axis]).toBeCloseTo(coordinate, 2),
      ),
    );
    await page.getByTestId("issue-frame-retry").click();
    await expectReady(page, fixture, 17);
    const stored = await json<Array<{ id: string; geometry: unknown }>>(
      await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
        headers: auth(fixture.token),
      }),
    );
    expect(stored.find((item) => item.id === annotation.id)?.geometry).toEqual(annotation.geometry);
    fixture.evidence.push({
      preservedDraftAnnotation: annotation,
      issueId: issue.id,
      navigationTrigger: "focus timeline Issue marker, native Enter",
      controllerDisabledAdmission:
        "covered by controller unit tests; browser exercises real draft cancellation",
    });
  });

  test("G1-2 延迟实际MP4：准备期间再次点击拖拽不穿透，放行后保留原F3落点", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const annotationsPath = `/api/v1/tasks/${fixture.taskId}/annotations`;
    const annotationWrites: unknown[] = [];
    page.on("request", (outgoing) => {
      if (outgoing.method() === "POST" && pathOf(outgoing.url()) === annotationsPath) {
        annotationWrites.push(outgoing.postDataJSON());
      }
    });
    const held = await holdMedia(page, fixture);
    await open(page, fixture);
    await held.fetched();
    await seek(page, 3);
    await key(page, "b");
    await expect(page.getByTestId("video-tool-btn-box")).toHaveAttribute("aria-pressed", "true");
    await arm(page);
    const anchor = await clickPoint(page, [0.4, 0.6]);
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "3");
    await expect(modal(page)).toBeHidden();
    // The first drop disarms the FAB; the pending catcher must still own later canvas gestures.
    await clickPoint(page, [0.75, 0.3]);
    const bounds = await videoMediaBounds(page);
    await page.mouse.move(bounds.x + bounds.width * 0.2, bounds.y + bounds.height * 0.2);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width * 0.65, bounds.y + bounds.height * 0.45, {
      steps: 6,
    });
    await page.mouse.up();
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await expect(modal(page)).toBeHidden();
    await expect(page.getByTestId("class-picker-popover")).toBeHidden();
    await expect(page.getByTestId("video-creation-scope-hint")).toBeHidden();
    expect(annotationWrites).toEqual([]);
    expect(
      await json<unknown[]>(
        await request.get(`${API_BASE}${annotationsPath}`, { headers: auth(fixture.token) }),
      ),
    ).toEqual([]);
    expect(fixture.writes).toEqual([]);
    held.release();
    await expectReady(page, fixture, 3);
    await expect(page.getByTestId("issue-create-frame")).toHaveText("源帧 F 3");
    const issue = await saveIssue(page, fixture);
    expect(issue.anchor_position).toMatchObject({ frame: 3 });
    expect(issue.anchor_position!.x).toBeCloseTo(anchor.x, 12);
    expect(issue.anchor_position!.y).toBeCloseTo(anchor.y, 12);
    expect((await listIssues(request, fixture)).find((item) => item.id === issue.id)).toEqual(
      issue,
    );
    expect(annotationWrites).toEqual([]);
    fixture.evidence.push({
      pendingCanvasGestures: ["click", "rectangle drag"],
      annotationWrites,
      retainedAnchor: issue.anchor_position,
    });
    await held.finish();
  });

  test("G1-4 延迟实际MP4超过就绪期限：超时保留F3落点，重试后才打开表单", async ({
    page,
    issueCase: fixture,
  }) => {
    const held = await holdMedia(page, fixture);
    await open(page, fixture);
    await held.fetched();
    await seek(page, 3);
    await arm(page);
    const anchor = await clickPoint(page, [0.4, 0.6]);
    await expect(navigation(page)).toHaveAttribute("data-status", "timeout", { timeout: 12_000 });
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "3");
    await expect(modal(page)).toBeHidden();
    expect(fixture.writes).toEqual([]);
    await held.finish();
    await page.getByTestId("issue-frame-retry").click();
    await expectReady(page, fixture, 3);
    await expect(modal(page)).toBeVisible();
    await expect(modal(page)).toContainText(
      `画布位置 x ${anchor.x.toFixed(3)} · y ${anchor.y.toFixed(3)}`,
    );
    await expect(page.getByTestId("issue-create-frame")).toHaveText("源帧 F 3");
  });

  test("G1-4 延迟实际MP4时连续点击只采纳最后锚点，任务A→B→A不复活旧请求", async ({
    page,
    request,
    seed,
    issueCase: fixture,
  }) => {
    const first = await createIssue(request, fixture, 3);
    const last = await createIssue(request, fixture, 17);
    const secondTask = await seed.videoWebCodecs(fixture.data.project_id, {
      fixture: "h264-boundary-gop8",
    });
    const display = async (taskId: string) =>
      (
        await json<{ display_id: string }>(
          await request.get(`${API_BASE}/api/v1/tasks/${taskId}`, { headers: auth(fixture.token) }),
        )
      ).display_id;
    const originalDisplay = await display(fixture.taskId);
    const secondDisplay = await display(secondTask.task_id);
    const held = await holdMedia(page, fixture);
    await open(page, fixture);
    await held.fetched();
    await openIssues(page);
    await observeNavigation(page);
    await page.getByTestId(`discussion-issue-locate-${first.id}`).click();
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await page.getByTestId(`discussion-issue-locate-${last.id}`).click();
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "17");
    await held.finish();
    await expectReady(page, fixture, 17);
    const clickTransitions = await finishNavigationObservation(page);
    expect(
      clickTransitions.filter((entry) => entry.status === "ready").map((entry) => entry.frame),
    ).toEqual(["17"]);
    fixture.evidence.push({ rapidClickTransitions: clickTransitions });

    // Reopen with a cold cache via reload, then preserve the JS owner through real SPA clicks.
    const secondGate = await holdMedia(page, fixture);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(stage(page)).toBeVisible({ timeout: 25_000 });
    await secondGate.fetched();
    await openIssues(page);
    await observeNavigation(page);
    await page.getByTestId(`discussion-issue-locate-${first.id}`).click();
    await expect(navigation(page)).toHaveAttribute("data-status", "preparing");
    await page
      .getByRole("tabpanel", { name: "任务队列", exact: true })
      .getByText(secondDisplay, { exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`task=${secondTask.task_id}`));
    await expect(navigation(page)).toBeHidden();
    await page
      .getByRole("tabpanel", { name: "任务队列", exact: true })
      .getByText(originalDisplay, { exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`task=${fixture.taskId}`));
    await expect(navigation(page)).toBeHidden();
    await secondGate.finish();
    // Allow actual source delivery and paint to finish before checking for a stale UI result.
    const returnedFrame = Number(await stage(page).getAttribute("data-video-frame-index"));
    await expect(async () =>
      expectVideoFramePixels(page, fixture.expectations, returnedFrame),
    ).toPass({ timeout: 15_000 });
    await expect(navigation(page)).toBeHidden();
    await expect(modal(page)).toBeHidden();
    const taskTransitions = await finishNavigationObservation(page);
    expect(taskTransitions.some((entry) => entry.status === "ready")).toBe(false);
    fixture.evidence.push({
      taskAbaTransitions: taskTransitions,
      returnedSourceFrame: returnedFrame,
    });
    await openIssues(page);
    await page.getByTestId(`discussion-issue-locate-${last.id}`).click();
    await expectReady(page, fixture, 17);
    expect(fixture.writes).toEqual([]);
    expect((await listIssues(request, fixture)).map((issue) => issue.id).sort()).toEqual(
      [first.id, last.id].sort(),
    );
  });
});

test.describe("video Issue explicit decoder and native-media faults", () => {
  test.use({ videoFixture: "malformed-samples" });
  test.setTimeout(120_000);

  test("G1-4 损坏解码元数据仍允许有真实像素证明的native fallback定位", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    const issue = await createIssue(request, fixture, 3);
    await open(page, fixture);
    await openIssues(page);
    await page.getByTestId(`discussion-issue-locate-${issue.id}`).click();
    await expectReady(page, fixture, 3);
    await expect(stage(page)).toHaveAttribute("data-video-frame-source", /^(video|native-bitmap)$/);
    fixture.evidence.push({
      faultInjection: "seed malformed decoder sample metadata; native MP4 bytes remain valid",
      nativeFallback: "ready only after exact pixels",
    });
  });

  test("G1-4 故障注入：损坏解码元数据并停止原生帧回执，不报告ready，恢复后重试", async ({
    page,
    request,
    issueCase: fixture,
  }) => {
    await page.addInitScript(() => {
      const prototype = HTMLVideoElement.prototype;
      const original = prototype.requestVideoFrameCallback;
      const state = { stalled: true, events: [] as Array<Record<string, unknown>> };
      const observed = new WeakSet<HTMLVideoElement>();
      const record = (kind: string, video: HTMLVideoElement, mediaTime?: number) => {
        state.events.push({
          kind,
          at: performance.now(),
          stalled: state.stalled,
          currentTime: video.currentTime,
          seeking: video.seeking,
          readyState: video.readyState,
          ...(mediaTime === undefined ? {} : { mediaTime }),
        });
      };
      Object.defineProperty(window, "__g1NativeFrameFault", { value: state, configurable: true });
      if (original) {
        prototype.requestVideoFrameCallback = function (callback) {
          if (!observed.has(this)) {
            observed.add(this);
            for (const name of ["seeking", "seeked", "loadeddata"]) {
              this.addEventListener(name, () => record(name, this));
            }
          }
          record("request", this);
          return original.call(this, (now, metadata) => {
            record(state.stalled ? "swallowed" : "delivered", this, metadata.mediaTime);
            if (!state.stalled) callback(now, metadata);
          });
        };
      }
    });
    const issue = await createIssue(request, fixture, 3);
    await open(page, fixture);
    await openIssues(page);
    await page.getByTestId(`discussion-issue-locate-${issue.id}`).click();
    await expect(navigation(page)).toHaveAttribute("data-status", /^(timeout|unavailable)$/, {
      timeout: 12_000,
    });
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "3");
    await expect(modal(page)).toBeHidden();
    await page.evaluate(() => {
      (
        window as unknown as { __g1NativeFrameFault: { stalled: boolean } }
      ).__g1NativeFrameFault.stalled = false;
    });
    await page.getByTestId("issue-frame-retry").click();
    // The fixture permanently discarded F3's receipt. Restoring the callback function does
    // not replay it, and a paused same-frame seek produces no new native presentation.
    await expect(navigation(page)).toHaveAttribute("data-status", "timeout", { timeout: 12_000 });
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "3");
    // Restore actual native frame output through real user navigation, then retry the same Issue.
    await key(page, "Shift+ArrowLeft");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "2");
    await expect(async () => expectVideoFramePixels(page, fixture.expectations, 2)).toPass({
      timeout: 12_000,
    });
    await page.getByTestId("issue-frame-retry").click();
    try {
      await expectReady(page, fixture, 3);
    } finally {
      fixture.evidence.push({
        nativeFrameFault: await page.evaluate(() => {
          const video = document.querySelector<HTMLVideoElement>(
            '[data-testid="video-konva-source"]',
          );
          return {
            fault: (window as unknown as { __g1NativeFrameFault: unknown }).__g1NativeFrameFault,
            currentTime: video?.currentTime,
            seeking: video?.seeking,
            readyState: video?.readyState,
            paused: video?.paused,
            width: video?.videoWidth,
            height: video?.videoHeight,
          };
        }),
      });
    }
    fixture.evidence.push({
      faultInjection:
        "malformed precise metadata plus browser-native requestVideoFrameCallback delivery stall",
      recovery:
        "restore callbacks; same-frame retry still times out without a receipt; native Shift+ArrowLeft presents F2, then retry the unchanged F3 issue anchor",
      controllerDisabledAdmission: false,
    });
    expect(fixture.writes).toEqual([]);
  });
});
