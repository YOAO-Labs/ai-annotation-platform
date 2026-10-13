import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
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
  attributes_meta: Record<string, unknown>;
  parent_annotation_id: string | null;
  tool_unit_id: string;
  z_order: number;
  source: string;
  confidence: number | null;
  is_locked: boolean;
}
interface Case {
  allowedErrors: Set<string>;
  data: SeedData;
  taskId: string;
  headers: Record<string, string>;
  writes: Array<{ method: string; path: string; body: unknown }>;
  evidence: unknown[];
}
const stage = (page: Page) => page.getByTestId("workbench-stage");
const controls = (page: Page) => page.getByTestId("polygon-slice-controls");
const pathOf = (url: string) => new URL(url).pathname;
async function json<T>(response: APIResponse | Response): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

const test = base.extend<{ sliceCase: Case }>({
  sliceCase: async ({ page, request, seed, browser }, provideFixture, testInfo) => {
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
    const fixture: Case = {
      data,
      taskId,
      headers,
      writes: [],
      evidence: [],
      allowedErrors: new Set(),
    };
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
      const unexpected = errors.filter(
        (error) =>
          !expectedAborts.includes(error) &&
          !fixture.allowedErrors.has(`${error.kind}:${error.path}:${error.message}`),
      );
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
        await testInfo.attach("polygon-slice-evidence", {
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

test.describe.configure({ timeout: 90_000 });

async function open(page: Page, fixture: Case) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/projects/${fixture.data.project_id}/annotate?task=${fixture.taskId}`);
  await page.getByRole("button", { name: "布局", exact: true }).click();
  await page.getByRole("button", { name: "标准标注布局", exact: true }).click();
  await expect(stage(page)).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await page.getByTestId("tool-btn-select").click();
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
interface SliceResult {
  operation_id: string;
  slice_operation_id: string;
  source_annotation_id: string;
  created_annotation_id: string;
  result_versions: Record<string, number>;
  active_annotation_ids: string[];
  restore_expires_at: string;
  idempotent_replay: boolean;
}
interface Ledger {
  operations: Array<{
    id: string;
    kind: string;
    idempotency_key: string;
    result_versions: Record<string, number>;
    current_side: string | null;
  }>;
  annotations: Array<{
    id: string;
    version: number;
    is_active: boolean;
    geometry: Annotation["geometry"];
  }>;
}
function ledger(fixture: Case, action = "inspect", operationId?: string): Ledger {
  return JSON.parse(
    execFileSync(
      fileURLToPath(new URL("../../../api/.venv/bin/python", import.meta.url)),
      [
        fileURLToPath(new URL("../fixtures/annotation-slice-ledger.py", import.meta.url)),
        action,
        fixture.taskId,
        ...(operationId ? [operationId] : []),
      ],
      { encoding: "utf8", env: process.env },
    ),
  ) as Ledger;
}
async function seedSource(
  request: APIRequestContext,
  fixture: Case,
  geometry: unknown = { type: "polygon", points: concave },
  parentId?: string,
) {
  return json<Annotation>(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
      data: {
        annotation_type: (geometry as { type: string }).type,
        tool_unit_id: "region",
        class_name: "car",
        geometry,
        attributes: { verified: true, count: 7 },
        ...(parentId ? { parent_annotation_id: parentId } : {}),
      },
    }),
  );
}
async function read(request: APIRequestContext, fixture: Case) {
  return json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
    }),
  );
}
async function clickPoint(page: Page, point: Point, button: "left" | "right" = "left") {
  const rect = await bounds(page);
  await page.mouse.click(rect.x + point[0] * rect.width, rect.y + point[1] * rect.height, {
    button,
  });
}
async function begin(page: Page, point: Point = [0.75, 0.3]) {
  await clickPoint(page, point, "right");
  await page.getByRole("menuitem", { name: "切割多边形", exact: true }).click();
  await expect(controls(page)).toBeVisible();
}
async function preview(
  page: Page,
  points: Point[] = [
    [0.7, 0.03],
    [0.7, 0.97],
  ],
  keyboard = false,
) {
  for (const point of points) await clickPoint(page, point);
  if (keyboard) await page.keyboard.press("Enter");
  else await controls(page).getByRole("button", { name: "预览切割", exact: true }).click();
  await expect(controls(page)).toContainText("预览 2 块");
}
async function commit(page: Page, fixture: Case, keyboard = false) {
  const pending = page.waitForResponse(
    (response) =>
      pathOf(response.url()) ===
      `/api/v1/tasks/${fixture.taskId}/annotations/polygon-slices:commit`,
  );
  if (keyboard) await page.keyboard.press("Enter");
  else await controls(page).getByRole("button", { name: "确认切割", exact: true }).click();
  const result = await json<SliceResult>(await pending);
  await expect(controls(page)).toBeHidden();
  return result;
}
async function restore(page: Page, fixture: Case, direction: "undo" | "redo") {
  const pending = page.waitForResponse(
    (response) =>
      pathOf(response.url()).startsWith(`/api/v1/tasks/${fixture.taskId}/annotations/slices/`) &&
      pathOf(response.url()).endsWith(":restore"),
  );
  await page.keyboard.press(direction === "undo" ? "Control+z" : "Control+Shift+z");
  const response = await pending;
  if (response.ok())
    await expect(
      page.getByRole("button", {
        name: direction === "undo" ? "重做 (Ctrl+Shift+Z)" : "撤销 (Ctrl+Z)",
        exact: true,
      }),
    ).toBeEnabled();
  return response;
}
async function reloadAndRead(page: Page, request: APIRequestContext, fixture: Case) {
  await page.reload();
  await expect(stage(page)).toHaveAttribute("data-image-ready", "true");
  const annotations = await read(request, fixture);
  fixture.evidence.push({ afterReload: annotations });
  return annotations;
}
function history(page: Page, taskId: string) {
  return page.evaluate((id) => {
    const userId = JSON.parse(localStorage.getItem("auth-storage") ?? "null")?.state?.user?.id;
    if (!userId) throw new Error("Expected an authenticated workbench history owner");
    return JSON.parse(sessionStorage.getItem(`wb:hist:${userId}:${id}`) ?? "null");
  }, taskId) as Promise<{ undo: unknown[]; redo: unknown[] }>;
}
function allowHttpError(fixture: Case, path: string, status: number) {
  fixture.allowedErrors.add(`http:${path}:${status}`);
  fixture.allowedErrors.add(
    `console:${path}:Failed to load resource: the server responded with a status of ${status} (${status === 409 ? "Conflict" : "Gone"})`,
  );
}

test("H4a-1 concave preview cancels without writes, then commits the two displayed regions", async ({
  page,
  request,
  sliceCase: fixture,
}, testInfo) => {
  const source = await seedSource(request, fixture);
  await open(page, fixture);
  await begin(page);
  await preview(page);
  await testInfo.attach("concave-preview", {
    contentType: "image/png",
    body: await page.screenshot({ fullPage: true }),
  });
  await controls(page).getByRole("button", { name: "取消切割", exact: true }).click();
  expect(fixture.writes).toHaveLength(0);
  expect(await reloadAndRead(page, request, fixture)).toEqual([source]);
  await begin(page);
  await preview(page, undefined, true);
  const shown = JSON.parse((await controls(page).getAttribute("data-slice-preview"))!) as Array<{
    points: Point[];
  }>;
  const result = await commit(page, fixture, true);
  const actual = await reloadAndRead(page, request, fixture);
  expect(actual).toHaveLength(2);
  expect(result.source_annotation_id).toBe(source.id);
  const kept = actual.find((item) => item.id === source.id)!;
  const created = actual.find((item) => item.id === result.created_annotation_id)!;
  const close = (actual: Point[], expected: Point[]) => {
    expect(actual).toHaveLength(expected.length);
    actual.forEach((point, index) =>
      point.forEach((value, axis) => expect(value).toBeCloseTo(expected[index][axis], 4)),
    );
  };
  close(kept.geometry.points, shown[0].points);
  close(created.geometry.points, shown[1].points);
  expect(fixture.writes).toHaveLength(1);
  fixture.evidence.push({ result, ledger: ledger(fixture) });
});

test("H4a-2 undo and redo preserve IDs, attributes and the original parent across reload", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const parent = await json<Annotation>(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
      data: {
        annotation_type: "bbox",
        tool_unit_id: "bbox",
        class_name: "car",
        geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
      },
    }),
  );
  let source = await seedSource(request, fixture, undefined, parent.id);
  source = await json<Annotation>(
    await request.patch(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${source.id}`, {
      headers: { ...fixture.headers, "If-Match": String(source.version) },
      data: { z_order: 9 },
    }),
  );
  await open(page, fixture);
  await begin(page);
  await preview(page);
  const sliced = await commit(page, fixture);
  const after = await read(request, fixture);
  const undone = await json<SliceResult>(await restore(page, fixture, "undo"));
  const before = await reloadAndRead(page, request, fixture);
  expect(before).toHaveLength(2);
  expect(before.find((item) => item.id === source.id)!.geometry).toEqual(source.geometry);
  const redone = await json<SliceResult>(await restore(page, fixture, "redo"));
  const actual = await reloadAndRead(page, request, fixture);
  expect(actual.map((item) => item.id).sort()).toEqual(after.map((item) => item.id).sort());
  for (const item of actual.filter((item) => item.id !== parent.id)) {
    expect(item).toMatchObject({
      parent_annotation_id: parent.id,
      attributes: source.attributes,
      attributes_meta: source.attributes_meta,
      class_name: "car",
      tool_unit_id: "region",
      z_order: 9,
    });
    expect(item.geometry).toEqual(after.find((candidate) => candidate.id === item.id)!.geometry);
    expect(item.version).toBeGreaterThan(
      after.find((candidate) => candidate.id === item.id)!.version,
    );
  }
  expect(redone.restore_expires_at).toBe(sliced.restore_expires_at);
  expect(undone.restore_expires_at).toBe(sliced.restore_expires_at);
  const keys = fixture.writes.map(
    (write) => (write.body as { idempotency_key: string }).idempotency_key,
  );
  expect(new Set(keys).size).toBe(3);
  fixture.evidence.push({ sliced, undone, redone, ledger: ledger(fixture) });
});

test("H4a-3 invalid cuts, holes, multipart, locked objects and parents refuse without writes", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const source = await seedSource(request, fixture);
  await open(page, fixture);
  for (const points of [
    [
      [0.9, 0.3],
      [0.9, 0.6],
    ],
    [
      [0.5, 0.9],
      [0.6, 0.8],
      [0.55, 0.6],
    ],
    [
      [0.03, 0.3],
      [0.97, 0.3],
      [0.97, 0.6],
      [0.03, 0.6],
    ],
    [
      [0.7, 0.03],
      [0.7, 0.03],
    ],
  ] as Point[][]) {
    fixture.evidence.push({ invalidCutAttempt: points });
    await begin(page);
    for (const point of points) await clickPoint(page, point);
    await controls(page).getByRole("button", { name: "预览切割", exact: true }).click();
    await expect(controls(page).getByRole("alert")).toBeVisible();
    await expect(controls(page).getByRole("button", { name: "确认切割", exact: true })).toHaveCount(
      0,
    );
    await page.keyboard.press("Escape");
    await expect(controls(page)).toBeHidden();
  }
  expect(fixture.writes).toHaveLength(0);
  expect(await reloadAndRead(page, request, fixture)).toEqual([source]);
  for (const patch of [
    {
      geometry: {
        type: "polygon",
        points: concave,
        holes: [
          [
            [0.3, 0.2],
            [0.4, 0.2],
            [0.4, 0.3],
          ],
        ],
      },
    },
    { geometry: { type: "multi_polygon", polygons: [{ type: "polygon", points: square }] } },
    { geometry: { type: "polygon", points: concave }, is_locked: true },
  ]) {
    const fresh = (await read(request, fixture)).find((item) => item.id === source.id)!;
    await json(
      await request.patch(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${source.id}`, {
        headers: { ...fixture.headers, "If-Match": String(fresh.version) },
        data: patch,
      }),
    );
    await reloadAndRead(page, request, fixture);
    await clickPoint(page, [0.75, 0.3], "right");
    await expect(page.getByRole("menuitem", { name: /^切割多边形/ })).toBeDisabled();
    await page.keyboard.press("Escape");
  }
  const fresh = (await read(request, fixture))[0];
  await json(
    await request.patch(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${source.id}`, {
      headers: { ...fixture.headers, "If-Match": String(fresh.version) },
      data: { is_locked: false },
    }),
  );
  await seedSource(
    request,
    fixture,
    {
      type: "polygon",
      points: [
        [0.3, 0.2],
        [0.4, 0.2],
        [0.4, 0.3],
      ],
    },
    source.id,
  );
  await reloadAndRead(page, request, fixture);
  await clickPoint(page, [0.75, 0.3], "right");
  await expect(page.getByRole("menuitem", { name: /切割多边形.*子对象/ })).toBeDisabled();
  await page.keyboard.press("Escape");
  expect(fixture.writes).toHaveLength(0);
  expect(ledger(fixture).operations).toHaveLength(0);
});

test("H4a-4 a lost successful response retries the same key and creates only two objects", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const source = await seedSource(request, fixture);
  await open(page, fixture);
  await begin(page);
  await preview(page);
  const path = `/api/v1/tasks/${fixture.taskId}/annotations/polygon-slices:commit`;
  let committed!: SliceResult;
  let interrupted = false;
  fixture.allowedErrors.add(`request:${path}:net::ERR_CONNECTION_RESET`);
  fixture.allowedErrors.add(`console:${path}:Failed to load resource: net::ERR_CONNECTION_RESET`);
  // Explicit fault injection: the real backend commits before the response is discarded.
  await page.route(`**${path}`, async (route) => {
    if (interrupted) return route.continue();
    interrupted = true;
    committed = await json<SliceResult>(await route.fetch());
    await route.abort("connectionreset");
  });
  await controls(page).getByRole("button", { name: "确认切割", exact: true }).click();
  await expect(controls(page).getByRole("button", { name: "重试切割", exact: true })).toBeEnabled();
  expect(await read(request, fixture)).toHaveLength(2);
  const pending = page.waitForResponse((response) => pathOf(response.url()) === path);
  await controls(page).getByRole("button", { name: "重试切割", exact: true }).click();
  const replay = await json<SliceResult>(await pending);
  await expect(controls(page)).toBeHidden();
  expect(replay.idempotent_replay).toBe(true);
  expect(replay.operation_id).toBe(committed.operation_id);
  expect(replay.source_annotation_id).toBe(source.id);
  expect(fixture.writes).toHaveLength(2);
  expect(fixture.writes[0].body).toEqual(fixture.writes[1].body);
  expect(await reloadAndRead(page, request, fixture)).toHaveLength(2);
  expect((await history(page, fixture.taskId)).undo).toHaveLength(1);
  expect(ledger(fixture).operations).toHaveLength(1);
  fixture.evidence.push({
    injectedFailure: "real commit followed by response loss",
    committed,
    replay,
    ledger: ledger(fixture),
  });
});

test("H4a-5 another session's edit blocks undo without moving history", async ({
  page,
  request,
  playwright,
  seed,
  sliceCase: fixture,
}) => {
  await seedSource(request, fixture);
  await open(page, fixture);
  await begin(page);
  await preview(page);
  const sliced = await commit(page, fixture);
  const secondary = await playwright.request.newContext({
    extraHTTPHeaders: {
      Authorization: `Bearer ${await seed.accessToken(fixture.data.annotator_email)}`,
    },
  });
  try {
    const created = (await read(request, fixture)).find(
      (item) => item.id === sliced.created_annotation_id,
    )!;
    await json(
      await secondary.patch(
        `${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${created.id}`,
        {
          headers: { "If-Match": String(created.version) },
          data: { attributes: { verified: false, count: 12 } },
        },
      ),
    );
    const before = await read(request, fixture);
    const position = await history(page, fixture.taskId);
    const path = `/api/v1/tasks/${fixture.taskId}/annotations/slices/${sliced.operation_id}:restore`;
    allowHttpError(fixture, path, 409);
    const rejected = await restore(page, fixture, "undo");
    expect(rejected.status()).toBe(409);
    const rejectedBody: unknown = await rejected.json();
    await expect(page.getByText("切割恢复失败，历史记录已保留", { exact: true })).toBeVisible();
    expect(await read(request, fixture)).toEqual(before);
    const after = await history(page, fixture.taskId);
    expect(after.undo).toHaveLength(position.undo.length);
    expect(after.redo).toHaveLength(position.redo.length);
    expect(ledger(fixture).operations).toHaveLength(1);
    expect(await reloadAndRead(page, request, fixture)).toEqual(before);
    fixture.evidence.push({
      secondaryAuthenticatedApiSession: true,
      rejected: rejectedBody,
      history: after,
    });
  } finally {
    await secondary.dispose();
  }
});

test("H4a-5 expired receipt refuses restore and leaves both objects and history intact", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  await seedSource(request, fixture);
  await open(page, fixture);
  await begin(page);
  await preview(page);
  const sliced = await commit(page, fixture);
  const before = await read(request, fixture);
  const position = await history(page, fixture.taskId);
  fixture.evidence.push({
    expiryFixture: "original receipt aged 31 days in verified disposable DB",
    ledger: ledger(fixture, "expire", sliced.operation_id),
  });
  const path = `/api/v1/tasks/${fixture.taskId}/annotations/slices/${sliced.operation_id}:restore`;
  allowHttpError(fixture, path, 410);
  const rejected = await restore(page, fixture, "undo");
  expect(rejected.status()).toBe(410);
  await expect(page.getByText("切割恢复失败，历史记录已保留", { exact: true })).toBeVisible();
  await expect(page.getByText("切割已超过 30 天恢复期限", { exact: true })).toBeVisible();
  const after = await history(page, fixture.taskId);
  expect(after.undo).toHaveLength(position.undo.length);
  expect(after.redo).toHaveLength(position.redo.length);
  expect(await reloadAndRead(page, request, fixture)).toEqual(before);
  expect(ledger(fixture).operations).toHaveLength(1);
});
