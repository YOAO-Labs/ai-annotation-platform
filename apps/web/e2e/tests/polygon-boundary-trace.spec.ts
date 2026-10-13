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
  version: number;
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

const test = base.extend<{ boundaryCase: Case }>({
  boundaryCase: async ({ page, request, seed, browser }, provideFixture, testInfo) => {
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
        await testInfo.attach("polygon-boundary-trace-evidence", {
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

const controls = (page: Page) => page.getByTestId("polygon-boundary-trace");
const count = (page: Page, expected: number) =>
  expect(stage(page)).toHaveAttribute("data-polygon-draft-count", String(expected));
const concave: Point[] = [
  [0.1, 0.1],
  [0.9, 0.1],
  [0.9, 0.8],
  [0.6, 0.8],
  [0.6, 0.4],
  [0.1, 0.4],
];
const square: Point[] = [
  [0.2, 0.2],
  [0.8, 0.2],
  [0.8, 0.8],
  [0.2, 0.8],
];
async function createSource(request: APIRequestContext, fixture: Case, geometry: unknown) {
  return json<Annotation>(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
      data: {
        annotation_type: "polygon",
        class_name: "car",
        geometry,
        tool_unit_id: "region",
        attributes: { verified: false, count: 0 },
      },
    }),
  );
}
async function clickPoint(page: Page, point: Point, offset: Point = [0, 0]): Promise<Point> {
  const b = await bounds(page);
  const x = Math.round(b.x + b.width * point[0] + offset[0]);
  const y = Math.round(b.y + b.height * point[1] + offset[1]);
  await page.mouse.click(x, y);
  return [(x - b.x) / b.width, (y - b.y) / b.height];
}
async function preview(page: Page, start: Point, end: Point) {
  await controls(page).getByRole("button", { name: "沿已有边界", exact: true }).click();
  await clickPoint(page, start);
  await expect(controls(page)).toContainText("在同一条边界上点击终点");
  await clickPoint(page, end);
  await expect(controls(page).getByRole("button", { name: "确认追加", exact: true })).toBeVisible();
}
function expectPoints(actual: Point[], expected: Point[]) {
  expect(actual).toHaveLength(expected.length);
  actual.forEach((point, i) =>
    point.forEach((value, axis) => expect(value).toBeCloseTo(expected[i][axis], 7)),
  );
}

test("H3-1 concave source defaults to shorter arc, switches direction, appends and persists", async ({
  page,
  request,
  boundaryCase: fixture,
}, testInfo) => {
  const source = await createSource(request, fixture, { type: "polygon", points: concave });
  await open(page, fixture);
  const first = await clickPoint(page, [0.94, 0.64]);
  await count(page, 1);
  await preview(page, concave[1], concave[4]);
  await expect(controls(page).getByRole("button", { name: /^顺时针/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(controls(page).getByRole("button", { name: /^顺时针/ })).toContainText("较短");
  await controls(page)
    .getByRole("button", { name: /^逆时针/ })
    .click();
  await expect(controls(page).getByRole("button", { name: /^逆时针/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await count(page, 1);
  await testInfo.attach("concave-selected-preview", {
    contentType: "image/png",
    body: await page.screenshot(),
  });
  await controls(page).getByRole("button", { name: "确认追加", exact: true }).click();
  await count(page, 5);
  await page.keyboard.press("Enter");
  const saved = await save(page, fixture);
  expectPoints(saved.geometry.points, [first, concave[1], concave[0], concave[5], concave[4]]);
  await reloadAndRead(page, request, fixture, [source, saved]);
  expect(fixture.writes.filter((write) => write.method === "POST")).toHaveLength(1);
  fixture.evidence.push({
    case: "H3-1",
    source,
    expected: [first, concave[1], concave[0], concave[5], concave[4]],
    saved,
  });
});

test("H3-2 wraps closure, snaps near vertices, deduplicates joins and cancels preview without clearing the draft", async ({
  page,
  request,
  boundaryCase: fixture,
}) => {
  const source = await createSource(request, fixture, { type: "polygon", points: square });
  await open(page, fixture);
  // Ordinary snapping chooses the nearest edge/vertex. Approach from outside
  // the corner so pixel rounding cannot make an interior edge projection nearer.
  await clickPoint(page, square[3], [-2, 2]);
  await count(page, 1);
  await controls(page).getByRole("button", { name: "沿已有边界", exact: true }).click();
  await clickPoint(page, square[3], [2, -2]);
  await clickPoint(page, square[1], [-2, 2]);
  await controls(page)
    .getByRole("button", { name: /^顺时针/ })
    .click();
  await controls(page).getByRole("button", { name: "确认追加", exact: true }).click();
  await count(page, 3);
  await page.keyboard.press("Backspace");
  await count(page, 2);
  await preview(page, square[0], square[2]);
  await page.keyboard.press("Escape");
  await expect(
    controls(page).getByRole("button", { name: "沿已有边界", exact: true }),
  ).toBeVisible();
  await count(page, 2);
  await expect(page.getByTestId("tool-btn-polygon")).toHaveAttribute("aria-pressed", "true");
  await clickPoint(page, square[2], [2, 2]);
  await page.keyboard.press("Enter");
  const saved = await save(page, fixture);
  expectPoints(saved.geometry.points, [square[3], square[0], square[2]]);
  await reloadAndRead(page, request, fixture, [source, saved]);
  fixture.evidence.push({ case: "H3-2", source, saved, sourceUnchanged: true, countAfterUndo: 2 });
});

test("H3-3 rejects source changes and deletion from an independent authenticated test session", async ({
  page,
  request,
  playwright,
  boundaryCase: fixture,
}) => {
  const source = await createSource(request, fixture, { type: "polygon", points: square });
  const second = await playwright.request.newContext({
    baseURL: API_BASE,
    extraHTTPHeaders: fixture.headers,
  });
  try {
    await open(page, fixture);
    const draft = await clickPoint(page, [0.9, 0.9]);
    await preview(page, square[0], square[2]);
    const changedGeometry = { type: "polygon", points: [[0.2, 0.25], ...square.slice(1)] };
    const changed = await json<Annotation>(
      await second.patch(`/api/v1/tasks/${fixture.taskId}/annotations/${source.id}`, {
        data: { geometry: changedGeometry },
        headers: { "If-Match": String(source.version) },
      }),
    );
    expect(changed.version).toBeGreaterThan(source.version);
    await controls(page).getByRole("button", { name: "确认追加", exact: true }).click();
    await expect(controls(page).getByRole("alert")).toContainText("来源已修改");
    await count(page, 1);
    await expect(controls(page).getByRole("button", { name: "确认追加", exact: true })).toHaveCount(
      0,
    );
    await page.reload();
    await expect(stage(page)).toHaveAttribute("data-image-ready", "true");
    await page.getByTestId("tool-btn-polygon").click();
    await clickPoint(page, draft);
    await preview(page, square[1], square[2]);
    expect(
      (
        await second.delete(`/api/v1/tasks/${fixture.taskId}/annotations/${source.id}`, {
          headers: { "If-Match": String(changed.version) },
        })
      ).status(),
    ).toBe(204);
    await controls(page).getByRole("button", { name: "确认追加", exact: true }).click();
    await expect(controls(page).getByRole("alert")).toContainText("来源已修改");
    await count(page, 1);
    expect(fixture.writes).toEqual([]);
    await reloadAndRead(page, request, fixture, []);
    fixture.evidence.push({
      case: "H3-3",
      source,
      changed,
      independentSessionDeleted: source.id,
      foregroundWrites: fixture.writes,
    });
  } finally {
    await second.dispose();
  }
});

test("H3-4 rejects holes, multiple exteriors and identical endpoints without degrading source geometry", async ({
  page,
  request,
  boundaryCase: fixture,
}) => {
  const outer: Point[] = [
    [0.12, 0.3],
    [0.4, 0.3],
    [0.4, 0.7],
    [0.12, 0.7],
  ];
  const hole: Point[] = [
    [0.2, 0.4],
    [0.3, 0.4],
    [0.3, 0.6],
    [0.2, 0.6],
  ];
  const part1: Point[] = [
    [0.6, 0.3],
    [0.75, 0.3],
    [0.75, 0.5],
    [0.6, 0.5],
  ];
  const part2: Point[] = [
    [0.8, 0.6],
    [0.94, 0.6],
    [0.94, 0.8],
    [0.8, 0.8],
  ];
  const simple: Point[] = [
    [0.45, 0.75],
    [0.55, 0.75],
    [0.55, 0.9],
    [0.45, 0.9],
  ];
  const sources = [
    await createSource(request, fixture, { type: "polygon", points: outer, holes: [hole] }),
    await createSource(request, fixture, {
      type: "multi_polygon",
      polygons: [
        { type: "polygon", points: part1 },
        { type: "polygon", points: part2 },
      ],
    }),
    await createSource(request, fixture, { type: "polygon", points: simple }),
  ];
  await open(page, fixture);
  await controls(page).getByRole("button", { name: "沿已有边界", exact: true }).click();
  for (const point of [outer[0], hole[0], part1[0], part2[0]]) {
    await clickPoint(page, point);
    await expect(controls(page).getByRole("alert")).toContainText("带孔和多外环");
    await count(page, 0);
  }
  await clickPoint(page, simple[0]);
  await clickPoint(page, simple[0]);
  await expect(controls(page).getByRole("alert")).toContainText("起点与终点不能重合");
  await page.keyboard.press("Enter");
  await count(page, 0);
  await expect(picker(page)).toHaveCount(0);
  await controls(page).getByRole("button", { name: "取消追踪", exact: true }).click();
  expect(fixture.writes).toEqual([]);
  await reloadAndRead(page, request, fixture, sources);
  fixture.evidence.push({
    case: "H3-4",
    sourceGeometriesPreserved: sources.map((source) => source.geometry),
    foregroundWrites: fixture.writes,
  });
});
