import {
  imageWorkbenchAborts,
  isExpectedRequestAbort,
  type RequestErrorSignal,
} from "../helpers/request-errors";
import type { APIRequestContext, APIResponse, Page, Response } from "@playwright/test";
import { expect, test as base, type SeedData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type Point = [number, number];
interface Annotation {
  id: string;
  geometry: { type: string; points: Point[] };
  class_name: string;
  attributes: Record<string, unknown>;
}
interface Case {
  data: SeedData;
  taskId: string;
  headers: Record<string, string>;
  writes: Array<{ method: string; path: string; body: unknown }>;
  evidence: unknown[];
}
const stage = (page: Page) => page.getByTestId("workbench-stage");
const picker = (page: Page) => page.getByTestId("class-picker-popover");
const pathOf = (url: string) => new URL(url).pathname;
async function json<T>(response: APIResponse | Response): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

const test = base.extend<{ polygonCase: Case }>({
  polygonCase: async ({ page, request, seed, browser }, provideFixture, testInfo) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
    const project = await json<{ tool_bindings: Record<string, unknown> }>(
      await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, { headers }),
    );
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
        headers,
        data: {
          ai_enabled: false,
          ai_interactive_enabled: false,
          ml_backend_id: null,
          tool_bindings: {
            ...project.tool_bindings,
            region: {
              enabled: true,
              classes: [{ name: "car" }, { name: "person" }],
              attribute_schema: {
                fields: [
                  {
                    key: "verified",
                    label: "已核验",
                    type: "boolean",
                    default: false,
                    required: true,
                  },
                  { key: "count", label: "数量", type: "number", default: 0, required: true },
                ],
              },
            },
          },
        },
      }),
    );
    expect(
      (
        await request.delete(
          `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
          { headers },
        )
      ).status(),
    ).toBe(204);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const fixture: Case = { data, taskId, headers, writes: [], evidence: [] };
    await seed.injectToken(page, data.annotator_email);
    const errors: RequestErrorSignal[] = [];
    page.on("pageerror", (error) => errors.push({ kind: "page", message: error.message }));
    page.on("console", (message) => {
      if (message.type() === "error")
        errors.push({
          kind: "console",
          message: message.text(),
          path: message.location().url ? pathOf(message.location().url) : undefined,
        });
    });
    page.on("response", (response) => {
      if (response.status() >= 400 && pathOf(response.url()).startsWith("/api/v1/"))
        errors.push({
          kind: "http",
          path: pathOf(response.url()),
          message: String(response.status()),
          method: response.request().method(),
        });
    });
    page.on("requestfailed", (request) => {
      if (pathOf(request.url()).startsWith("/api/v1/"))
        errors.push({
          kind: "request",
          path: pathOf(request.url()),
          method: request.method(),
          message: request.failure()?.errorText ?? "unknown",
        });
    });
    page.on("request", (request) => {
      const path = pathOf(request.url());
      if (
        request.method() !== "GET" &&
        /^\/api\/v1\/tasks\/[^/]+\/(annotations|ai-mask-candidates\/accept)(\/|$)/.test(path)
      )
        fixture.writes.push({ method: request.method(), path, body: request.postDataJSON() });
    });
    let passed = false;
    try {
      await provideFixture(fixture);
      // Reload can report keepalive session telemetry as aborted after the API accepts it.
      const expectedAborts = errors.filter((error) =>
        isExpectedRequestAbort(error, imageWorkbenchAborts),
      );
      const unexpected = errors.filter((error) => !expectedAborts.includes(error));
      fixture.evidence.push({ expectedAborts, unexpectedErrors: unexpected });
      expect(unexpected).toEqual([]);
      passed = testInfo.status === testInfo.expectedStatus;
    } finally {
      try {
        if (!page.isClosed()) {
          await testInfo.attach(passed ? "verified-workbench" : "failure-before-cleanup", {
            contentType: "image/png",
            body: await page.screenshot({ fullPage: true }),
          });
          if (!passed)
            await testInfo.attach("failure-aria-before-cleanup", {
              contentType: "text/plain",
              body: await page.locator("body").ariaSnapshot(),
            });
        }
        await testInfo.attach("polygon-auto-points-evidence", {
          contentType: "application/json",
          body: JSON.stringify(
            {
              projectId: data.project_id,
              taskId,
              browser: browser.version(),
              viewport: page.viewportSize(),
              dpr: page.isClosed() ? null : await page.evaluate(() => devicePixelRatio),
              writes: fixture.writes,
              errors,
              evidence: fixture.evidence,
            },
            null,
            2,
          ),
        });
      } finally {
        page.removeAllListeners("pageerror");
        page.removeAllListeners("console");
        page.removeAllListeners("response");
        page.removeAllListeners("requestfailed");
        page.removeAllListeners("request");
        await page.unrouteAll({ behavior: "ignoreErrors" });
        if (!page.isClosed()) await page.goto("about:blank");
        await seed.owned();
      }
    }
  },
});

async function open(page: Page, fixture: Case) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/projects/${fixture.data.project_id}/annotate?task=${fixture.taskId}`);
  await page.getByRole("button", { name: "布局", exact: true }).click();
  await page.getByRole("button", { name: "标准标注布局", exact: true }).click();
  await expect(stage(page)).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await page.getByTestId("tool-btn-polygon").click();
}
async function bounds(page: Page) {
  return stage(page).evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return {
      x: rect.x + Number(node.getAttribute("data-media-x")),
      y: rect.y + Number(node.getAttribute("data-media-y")),
      width: Number(node.getAttribute("data-media-width")),
      height: Number(node.getAttribute("data-media-height")),
    };
  });
}

async function save(page: Page, fixture: Case) {
  await expect(picker(page)).toBeVisible();
  const response = page.waitForResponse(
    (r) =>
      pathOf(r.url()) === `/api/v1/tasks/${fixture.taskId}/annotations` &&
      r.request().method() === "POST",
  );
  await picker(page).locator("span").filter({ hasText: /^car$/ }).click();
  const annotation = await json<Annotation>(await response);
  await expect(picker(page)).toBeHidden();
  await expect(stage(page)).toHaveAttribute("data-pending-drawing", "false");
  return annotation;
}
async function reloadAndRead(
  page: Page,
  request: APIRequestContext,
  fixture: Case,
  expected: Annotation[],
) {
  await page.reload();
  await expect(stage(page)).toHaveAttribute("data-image-ready", "true");
  const actual = await json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
    }),
  );
  expect(actual).toHaveLength(expected.length);
  for (const item of expected)
    expect(actual.find((candidate) => candidate.id === item.id)).toMatchObject({ ...item });
  fixture.evidence.push({ persistedAnnotations: actual });
}

async function pointCount(page: Page) {
  return Number(await stage(page).getAttribute("data-polygon-draft-count"));
}
async function startScreen(page: Page): Promise<Point> {
  const b = await bounds(page);
  return [Math.round(b.x + b.width * 0.25), Math.round(b.y + b.height * 0.25)];
}
async function autoPath(page: Page, start: Point, deltas: Point[], steps = 1) {
  await page.mouse.move(...start);
  await page.keyboard.down("Alt"); // Disable snapping to previously saved comparison contours.
  await page.keyboard.down("Shift");
  try {
    await page.mouse.down();
    for (const [dx, dy] of deltas) await page.mouse.move(start[0] + dx, start[1] + dy, { steps });
    await page.mouse.up();
  } finally {
    await page.keyboard.up("Shift");
    await page.keyboard.up("Alt");
  }
}
function assertScreenPath(
  annotation: Annotation,
  b: Awaited<ReturnType<typeof bounds>>,
  start: Point,
) {
  const screen = annotation.geometry.points.map(([x, y]) => [
    b.x + x * b.width - start[0],
    b.y + y * b.height - start[1],
  ]);
  expect(screen).toHaveLength(52);
  // The intended path is 160 px right, 83 px down, 160 px left (403 px total).
  for (let i = 0; i < screen.length; i++) {
    const distance = i === screen.length - 1 ? 403 : i * 8;
    const expected =
      distance <= 160
        ? [distance, 0]
        : distance <= 243
          ? [160, distance - 160]
          : [403 - distance, 83];
    expect(screen[i][0]).toBeCloseTo(expected[0], 2);
    expect(screen[i][1]).toBeCloseTo(expected[1], 2);
  }
  return screen;
}

test.describe("H2 polygon auto points", () => {
  test.setTimeout(120_000);
  test.use({ actionTimeout: 10_000 });

  test("H2-1 缩放与输入速度不改变累计8CSSpx采样，终点保留且刷新一致", async ({
    page,
    request,
    polygonCase: fixture,
  }) => {
    await open(page, fixture);
    const saved: Annotation[] = [];
    for (const zoom of [0, 1]) {
      if (zoom) {
        const b = await bounds(page);
        await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
        const previous = await stage(page).getAttribute("data-media-width");
        await page.keyboard.down("Control");
        await page.mouse.wheel(0, -200);
        await page.keyboard.up("Control");
        await expect(stage(page)).not.toHaveAttribute("data-media-width", previous!);
      }
      for (const steps of [1, 40]) {
        await page.keyboard.press("Escape"); // Clear the saved object's editable selection first.
        await page.getByTestId("tool-btn-polygon").click();
        const b = await bounds(page);
        const start = await startScreen(page);
        await autoPath(
          page,
          start,
          [
            [160, 0],
            [160, 83],
            [0, 83],
          ],
          steps,
        );
        await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "52");
        await page.keyboard.press("Enter");
        const annotation = await save(page, fixture);
        const screen = assertScreenPath(annotation, b, start);
        saved.push(annotation);
        fixture.evidence.push({ zoom, steps, media: b, start, screenPoints: screen });
      }
    }
    await reloadAndRead(page, request, fixture, saved);
  });

  test("H2-2 松开Shift继续单点，Backspace撤一点，连续创建自动保存后继续下一对象", async ({
    page,
    request,
    polygonCase: fixture,
  }) => {
    await open(page, fixture);
    const start = await startScreen(page);
    await autoPath(page, start, [
      [96, 0],
      [96, 80],
    ]);
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "23");
    await page.mouse.click(start[0], start[1] + 80);
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "24");
    await page.keyboard.press("Backspace");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "23");
    await page.mouse.click(start[0], start[1] + 96);
    await page.keyboard.press("Enter");
    const saved = [await save(page, fixture)];
    expect(saved[0].geometry.points).toHaveLength(24);
    const controls = page.getByTestId("continuous-creation-controls");
    await controls.getByRole("switch", { name: "连续创建", exact: true }).click();
    await controls.getByRole("combobox", { name: "创建工具单元" }).selectOption("region");
    await controls.getByRole("button", { name: "car", exact: true }).click();
    for (const y of [120, 240]) {
      const response = page.waitForResponse(
        (r) =>
          pathOf(r.url()) === `/api/v1/tasks/${fixture.taskId}/annotations` &&
          r.request().method() === "POST",
      );
      await autoPath(
        page,
        [start[0], start[1] + y],
        [
          [96, 0],
          [96, 80],
          [0, 80],
        ],
      );
      await page.keyboard.press("Enter");
      const annotation = await json<Annotation>(await response);
      expect(annotation.geometry.type).toBe("polygon");
      expect(annotation.geometry.points).toHaveLength(35);
      expect(annotation.class_name).toBe("car");
      expect(annotation.attributes).toEqual({ verified: false, count: 0 });
      await expect(picker(page)).toBeHidden();
      await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "0");
      saved.push(annotation);
    }
    await reloadAndRead(page, request, fixture, saved);
  });

  test("H2-3 零移动重复点不增长，Space平移不加点，Esc与切工具不会恢复旧采样", async ({
    page,
    polygonCase: fixture,
  }) => {
    await open(page, fixture);
    const start = await startScreen(page);
    await autoPath(page, start, [
      [0, 0],
      [0, 0],
    ]);
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "1");
    await autoPath(page, start, [[0, 0]]);
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "1");
    await page.keyboard.down("Space");
    await page.mouse.move(start[0] + 50, start[1] + 50);
    await page.mouse.down();
    await page.mouse.move(start[0] + 80, start[1] + 65, { steps: 10 });
    await page.mouse.up();
    await page.keyboard.up("Space");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "1");
    await page.keyboard.press("Escape");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "0");
    const next = await startScreen(page);
    await page.mouse.move(...next);
    await page.keyboard.down("Shift");
    await page.mouse.down();
    await page.mouse.move(next[0] + 160, next[1]);
    await page.keyboard.press("Escape");
    await page.mouse.move(next[0] + 240, next[1]);
    await page.mouse.up();
    await page.keyboard.up("Shift");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "0");
    await autoPath(page, next, [
      [80, 0],
      [80, 80],
    ]);
    await page.getByTestId("tool-btn-box").click();
    await page.getByTestId("tool-btn-polygon").click();
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "0");
    expect(fixture.writes).toEqual([]);
    fixture.evidence.push({
      zeroMovementCount: 1,
      afterPanCount: 1,
      afterEscapeAndToolSwitch: await pointCount(page),
      writes: 0,
    });
  });

  test("H2-5 普通双击仍闭合并保存，Shift拖动中Enter包含最终端点", async ({
    page,
    request,
    polygonCase: fixture,
  }) => {
    await open(page, fixture);
    const start = await startScreen(page);
    await page.mouse.click(...start);
    await page.mouse.click(start[0] + 120, start[1]);
    await page.mouse.dblclick(start[0] + 120, start[1] + 90);
    const saved = [await save(page, fixture)];
    expect(saved[0].geometry.type).toBe("polygon");
    await page.keyboard.press("Escape");
    await page.getByTestId("tool-btn-polygon").click();
    await page.mouse.move(start[0], start[1] + 150);
    await page.keyboard.down("Shift");
    await page.mouse.down();
    await page.mouse.move(start[0] + 120, start[1] + 150);
    await page.mouse.move(start[0] + 120, start[1] + 241);
    await page.keyboard.press("Enter");
    await page.mouse.up();
    await page.keyboard.up("Shift");
    saved.push(await save(page, fixture));
    expect(saved[1].geometry.points).toHaveLength(28);
    const b = await bounds(page);
    const last = saved[1].geometry.points.at(-1)!;
    expect(last[0] * b.width + b.x).toBeCloseTo(start[0] + 120, 3);
    expect(last[1] * b.height + b.y).toBeCloseTo(start[1] + 241, 3);
    await reloadAndRead(page, request, fixture, saved);
  });

  test("H2-4 真实长路径在20,000点暂停并保留草稿，可撤一点再取消，未自动保存", async ({
    page,
    polygonCase: fixture,
  }, testInfo) => {
    test.setTimeout(180_000);
    await open(page, fixture);
    const start = await startScreen(page);
    await page.mouse.move(...start);
    await page.keyboard.down("Alt");
    await page.keyboard.down("Shift");
    await page.mouse.down();
    // A bounded repeated screen path exercises the interaction budget; it is intentionally not submitted as geometry.
    for (let i = 0; i < 500; i++) {
      await page.mouse.move(start[0] + (i % 2 === 0 ? 320 : 0), start[1] + (i % 4 < 2 ? 0 : 8));
      if (i % 50 === 49 && (await pointCount(page)) >= 20_000) break;
    }
    await page.mouse.up();
    await page.keyboard.up("Shift");
    await page.keyboard.up("Alt");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "20000");
    await expect(page.getByTestId("polygon-auto-points-status")).toContainText("自动落点已暂停");
    await testInfo.attach("polygon-budget-preserved", {
      contentType: "image/png",
      body: await page.screenshot({ fullPage: true }),
    });
    await autoPath(page, start, [
      [320, 0],
      [0, 0],
    ]);
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "20000");
    expect(fixture.writes).toEqual([]);
    await page.keyboard.press("Backspace");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "19999");
    await expect(page.getByTestId("polygon-auto-points-status")).not.toContainText("已暂停");
    fixture.evidence.push({
      budget: 20_000,
      retainedAfterFurtherDrag: 20_000,
      afterUndo: await pointCount(page),
      writes: fixture.writes,
    });
    await page.keyboard.press("Escape");
    await expect(stage(page)).toHaveAttribute("data-polygon-draft-count", "0");
    await expect(page.getByTestId("polygon-auto-points-status")).toBeHidden();
    expect(fixture.writes).toEqual([]);
  });
});
