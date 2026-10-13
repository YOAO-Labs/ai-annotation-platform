import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  imageWorkbenchAborts,
  isExpectedRequestAbort,
  maskEditorAborts,
  objectCommentsAborts,
  type RequestErrorSignal,
} from "../helpers/request-errors";
import type { APIRequestContext, APIResponse, Page, Response } from "@playwright/test";
import { expect, test as base, type SeedData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type Point = [number, number];
interface Rle {
  encoding: "coco_rle";
  size: [number, number];
  counts: number[];
}
interface MaskReference {
  encoding: "coco_rle_ref";
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
}
interface MutationResult {
  operation_id: string;
  slice_restore: SliceResult;
  idempotent_replay: boolean;
}
interface Annotation {
  id: string;
  version: number;
  geometry: { type: string; mask: MaskReference };
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
const controls = (page: Page) => page.getByTestId("mask-toolbar");
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
    await seed.configureRasterMask(data.project_id, true);
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
        isExpectedRequestAbort(error, imageWorkbenchAborts, objectCommentsAborts, maskEditorAborts),
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
        await testInfo.attach("mask-slice-evidence", {
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
  activeLocks: number;
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
async function read(request: APIRequestContext, fixture: Case) {
  return json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
    }),
  );
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

function encode(width: number, height: number, pixel: (x: number, y: number) => boolean): Rle {
  const counts = [0];
  let last = false;
  for (let x = 0; x < width; x += 1)
    for (let y = 0; y < height; y += 1) {
      const next = pixel(x, y);
      if (next !== last) {
        counts.push(0);
        last = next;
      }
      counts[counts.length - 1] += 1;
    }
  return { encoding: "coco_rle", size: [height, width], counts };
}
function decode(rle: Rle): boolean[] {
  const [height, width] = rle.size;
  const output = Array<boolean>(width * height).fill(false);
  let cursor = 0;
  rle.counts.forEach((count, index) => {
    for (let position = cursor; position < cursor + count; position += 1) {
      const x = Math.floor(position / height),
        y = position % height;
      output[y * width + x] = index % 2 === 1;
    }
    cursor += count;
  });
  return output;
}
const area = (rle: Rle) => rle.counts.reduce((sum, n, i) => sum + (i % 2 ? n : 0), 0);
const holed = () =>
  encode(
    64,
    48,
    (x, y) =>
      (x >= 4 && x < 52 && y >= 8 && y < 43 && !(x >= 19 && x < 30 && y >= 18 && y < 29)) ||
      (x >= 56 && x < 61 && y >= 21 && y < 32),
  );
const full = () => encode(64, 48, () => true);
async function content(request: APIRequestContext, fixture: Case, id: string) {
  return json<Rle>(
    await request.get(`${API_BASE}/api/v1/annotations/${id}/mask-content`, {
      headers: fixture.headers,
    }),
  );
}
async function seedMask(request: APIRequestContext, fixture: Case, rle = holed()) {
  const reference = await json<MaskReference>(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/mask-content`, {
      headers: fixture.headers,
      data: rle,
    }),
  );
  const source = await json<Annotation>(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
      data: {
        annotation_type: "raster_mask",
        tool_unit_id: "region",
        class_name: "car",
        geometry: { type: "raster_mask", mask: reference },
        attributes: { verified: true, count: 7 },
      },
    }),
  );
  fixture.evidence.push({ source, sourceRle: rle });
  return source;
}
async function begin(page: Page, id: string) {
  await page.getByTestId(`box-list-item-${id}`).click();
  await page.locator('button[aria-label="编辑 Mask"]:visible').last().click();
  await openMaskSettings(page);
  await expect(controls(page)).toContainText("就绪", { timeout: 15_000 });
  await controls(page).getByTitle("Mask 高级工具").click();
  await page.getByRole("menuitem", { name: "直线切割为两个实例", exact: true }).click();
  await expect(page.getByRole("menu")).toBeHidden();
  await expect(controls(page)).toContainText("拖动两点定义直线");
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0, { timeout: 10_000 });
}
async function draw(
  page: Page,
  cut: [Point, Point] = [
    [0.58, 0.23],
    [0.58, 0.94],
  ],
) {
  await closeMaskSettings(page);
  const rect = await bounds(page);
  await page.mouse.move(rect.x + cut[0][0] * rect.width, rect.y + cut[0][1] * rect.height);
  await page.mouse.down();
  await page.mouse.move(rect.x + cut[1][0] * rect.width, rect.y + cut[1][1] * rect.height, {
    steps: 8,
  });
  await page.mouse.up();
  await openMaskSettings(page);
}
async function preview(page: Page, cut?: [Point, Point]) {
  await draw(page, cut);
  await expect(page.getByTestId("mask-primary-action")).toHaveText("提交 2 个实例");
  await expect(controls(page)).toContainText("直线切割");
  await expect(controls(page)).toContainText(/面积 \d+ → \d+\+\d+ px/);
}
async function primaryKey(page: Page, key = "Enter") {
  await closeMaskSettings(page);
  await page.keyboard.press(key);
}
async function commit(page: Page, fixture: Case, keyboard = false) {
  const pending = page.waitForResponse(
    (response) =>
      pathOf(response.url()) ===
      `/api/v1/tasks/${fixture.taskId}/annotations/mask-mutations:commit`,
  );
  if (keyboard) await primaryKey(page);
  else await page.getByTestId("mask-primary-action").click();
  const response = await json<MutationResult>(await pending);
  await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0);
  expect(response.slice_restore).toBeTruthy();
  return response;
}
function lifecycle(fixture: Case, action: string, operationId: string) {
  return JSON.parse(
    execFileSync(
      fileURLToPath(new URL("../../../api/.venv/bin/python", import.meta.url)),
      [
        fileURLToPath(new URL("../fixtures/mask-slice-lifecycle.py", import.meta.url)),
        action,
        fixture.taskId,
        operationId,
      ],
      { encoding: "utf8", env: process.env },
    ),
  );
}
async function checkPartition(
  request: APIRequestContext,
  fixture: Case,
  original: Rle,
  receipt: SliceResult,
) {
  const saved = await read(request, fixture);
  expect(saved).toHaveLength(2);
  const rles = await Promise.all(
    [receipt.source_annotation_id, receipt.created_annotation_id].map((id) =>
      content(request, fixture, id),
    ),
  );
  const [kept, created] = rles.map(decode),
    before = decode(original);
  expect(kept.length).toBe(before.length);
  expect(created.length).toBe(before.length);
  expect(kept.some((pixel, index) => pixel && created[index])).toBe(false);
  expect(kept.map((pixel, index) => pixel || created[index])).toEqual(before);
  expect(area(rles[0])).toBeGreaterThanOrEqual(area(rles[1]));
  const write = fixture.writes
    .filter((item) => item.path.endsWith("mask-mutations:commit"))
    .at(-1)!;
  const payload = write.body as {
    cut_path: [Point, Point];
    report: { source_areas: number[]; result_areas: number[] };
  };
  expect(payload.report.source_areas).toEqual([area(original)]);
  expect(payload.report.result_areas).toEqual(rles.map(area));
  const [[ax, ay], [bx, by]] = payload.cut_path;
  const [height, width] = original.size;
  const left = before.map(
    (value, index) =>
      value &&
      (bx - ax) * ((Math.floor(index / width) + 0.5) / height - ay) -
        (by - ay) * (((index % width) + 0.5) / width - ax) >=
        0,
  );
  const right = before.map((value, index) => value && !left[index]);
  const leftArea = left.filter(Boolean).length,
    rightArea = right.filter(Boolean).length;
  expect(kept).toEqual(leftArea >= rightArea ? left : right);
  expect(created).toEqual(leftArea >= rightArea ? right : left);
  fixture.evidence.push({
    receipt,
    saved,
    cutPath: payload.cut_path,
    resultAreas: rles.map(area),
    partition: "disjoint, complete, center rule and identity verified",
  });
  return { saved, rles, cutPath: payload.cut_path };
}

test("H4b-1 holed non-square multi-component mask cancels, then saves the displayed two regions", async ({
  page,
  request,
  sliceCase: fixture,
}, testInfo) => {
  const original = holed(),
    source = await seedMask(request, fixture, original);
  await open(page, fixture);
  await begin(page, source.id);
  await preview(page);
  await testInfo.attach("mask-slice-preview", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
  await primaryKey(page, "Escape");
  expect(fixture.writes).toHaveLength(0);
  await reloadAndRead(page, request, fixture);
  expect(await content(request, fixture, source.id)).toEqual(original);
  expect(ledger(fixture).operations).toHaveLength(0);
  await begin(page, source.id);
  await preview(page);
  const response = await commit(page, fixture, true);
  await reloadAndRead(page, request, fixture);
  const { saved, rles } = await checkPartition(request, fixture, original, response.slice_restore);
  for (const [index, id] of [source.id, response.slice_restore.created_annotation_id].entries())
    await expect(page.getByTestId(`box-list-item-${id}`)).toContainText(`${area(rles[index])} px`);
  expect(
    saved.every((row) => JSON.stringify(row.attributes) === JSON.stringify(source.attributes)),
  ).toBe(true);
  expect(ledger(fixture).operations).toHaveLength(1);
  expect((await history(page, fixture.taskId)).undo).toHaveLength(1);
});

test("H4b-2 native center cuts, reversed endpoints, equal-area identity and zero-length rejection", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const original = full(),
    source = await seedMask(request, fixture, original);
  await open(page, fixture);
  const fitNativePixelCenters = async () => {
    // Reload restores panel proportions, so re-establish the pixel-center
    // precondition before every cut instead of reusing the previous viewport.
    await page.setViewportSize({ width: 1440, height: 1200 });
    await page.waitForTimeout(400);
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const measured = await bounds(page);
      const target = Math.floor(measured.width / 128) * 128;
      if (measured.width === target) break;
      await page.setViewportSize({
        width: page.viewportSize()!.width + target - Math.round(measured.width),
        height: 1200,
      });
      await page.waitForTimeout(250);
      await page.getByTitle("适应视口（双击空白）").click();
      await page.waitForTimeout(100);
    }
    const adjusted = await bounds(page);
    expect(adjusted.width % 128).toBe(0);
    fixture.evidence.push({ nativeCenterMediaBounds: adjusted });
  };
  const center = 31.5 / 64;
  const paths: Array<[Point, Point]> = [
    [
      [center, 0.25],
      [center, 0.875],
    ],
    [
      [center, 0.875],
      [center, 0.25],
    ],
    [
      [0.15, 0.5],
      [0.85, 0.5],
    ],
  ];
  for (let index = 0; index < paths.length; index += 1) {
    await fitNativePixelCenters();
    await begin(page, source.id);
    if (index === 0) {
      await draw(page, [
        [0.4, 0.5],
        [0.4, 0.5],
      ]);
      await expect(controls(page).getByRole("alert")).toContainText("不同");
      expect(fixture.writes).toHaveLength(0);
    }
    await preview(page, paths[index]);
    const response = await commit(page, fixture);
    const checked = await checkPartition(request, fixture, original, response.slice_restore);
    if (index < 2) {
      expect(checked.cutPath[0][0]).toBe(center);
      expect(checked.cutPath[1][0]).toBe(center);
    } else expect(checked.rles.map(area)).toEqual([1536, 1536]);
    await restore(page, fixture, "undo");
    await reloadAndRead(page, request, fixture);
    expect(await content(request, fixture, source.id)).toEqual(original);
  }
});

test("H4b-3 repeated button and held Enter submit one transaction", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const original = holed(),
    source = await seedMask(request, fixture, original);
  await open(page, fixture);
  await begin(page, source.id);
  await preview(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requests = 0;
  // Explicit latency injection holds the real commit while native input repeats.
  await page.route("**/annotations/mask-mutations:commit", async (route) => {
    requests += 1;
    await gate;
    await route.continue();
  });
  const pending = page.waitForResponse((response) =>
    response.url().endsWith("mask-mutations:commit"),
  );
  try {
    await page.getByTestId("mask-primary-action").dblclick();
    await expect.poll(() => requests).toBe(1);
    await closeMaskSettings(page);
    await page.keyboard.down("Enter");
    await page.keyboard.down("Enter");
    await page.keyboard.up("Enter");
    expect(requests).toBe(1);
  } finally {
    release();
  }
  const response = await json<MutationResult>(await pending);
  await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0);
  await reloadAndRead(page, request, fixture);
  await checkPartition(request, fixture, original, response.slice_restore);
  expect(ledger(fixture).operations).toHaveLength(1);
});

test("H4b-3 lost commit response retries the same key without duplicating results or history", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const source = await seedMask(request, fixture);
  await open(page, fixture);
  await begin(page, source.id);
  await preview(page);
  const path = `/api/v1/tasks/${fixture.taskId}/annotations/mask-mutations:commit`;
  fixture.allowedErrors.add(`request:${path}:net::ERR_CONNECTION_RESET`);
  fixture.allowedErrors.add(`console:${path}:Failed to load resource: net::ERR_CONNECTION_RESET`);
  let committed: MutationResult | null = null;
  await page.route("**/annotations/mask-mutations:commit", async (route) => {
    if (!committed) {
      committed = await json<MutationResult>(await route.fetch());
      await route.abort("connectionreset");
    } else await route.continue();
  });
  await page.getByTestId("mask-primary-action").click();
  await expect(page.getByTestId("mask-primary-action")).toHaveText("重试实例提交");
  expect(await read(request, fixture)).toHaveLength(2);
  const replay = await commit(page, fixture);
  expect(replay.idempotent_replay).toBe(true);
  expect(replay.operation_id).toBe(committed!.operation_id);
  expect(fixture.writes[0].body).toEqual(fixture.writes[1].body);
  expect(ledger(fixture).operations).toHaveLength(1);
  expect((await history(page, fixture.taskId)).undo).toHaveLength(1);
  await reloadAndRead(page, request, fixture);
  await checkPartition(request, fixture, holed(), replay.slice_restore);
});

test("H4b-4 undo deactivates the new mask; real GC retains its pixels for browser redo", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const original = holed(),
    source = await seedMask(request, fixture, original);
  await open(page, fixture);
  await begin(page, source.id);
  await preview(page);
  const receipt = (await commit(page, fixture)).slice_restore;
  const before = await checkPartition(request, fixture, original, receipt);
  await restore(page, fixture, "undo");
  expect(
    ledger(fixture).annotations.find((row) => row.id === receipt.created_annotation_id)?.is_active,
  ).toBe(false);
  const gc = lifecycle(fixture, "gc", receipt.operation_id);
  fixture.evidence.push({ explicitGcAgeFixture: gc });
  expect(gc.orphan_deleted).toBe(true);
  await reloadAndRead(page, request, fixture);
  const redo = await json<SliceResult>(await restore(page, fixture, "redo"));
  expect(redo.restore_expires_at).toBe(receipt.restore_expires_at);
  await reloadAndRead(page, request, fixture);
  const after = await checkPartition(request, fixture, original, redo);
  expect(after.saved.map((row) => row.id).sort()).toEqual(before.saved.map((row) => row.id).sort());
  expect(after.rles).toEqual(before.rles);
  for (const [index, id] of [receipt.source_annotation_id, receipt.created_annotation_id].entries())
    await expect(page.getByTestId(`box-list-item-${id}`)).toContainText(
      `${area(after.rles[index])} px`,
    );
  expect(ledger(fixture).operations).toHaveLength(3);
});

for (const failure of ["expired", "missing_revision", "peer_edit"] as const) {
  test(`H4b-5 ${failure} rejects undo with unchanged pixels and history position`, async ({
    page,
    request,
    playwright,
    seed,
    sliceCase: fixture,
  }) => {
    const source = await seedMask(request, fixture);
    await open(page, fixture);
    await begin(page, source.id);
    await preview(page);
    const receipt = (await commit(page, fixture)).slice_restore;
    if (failure === "expired") ledger(fixture, "expire", receipt.operation_id);
    else if (failure === "missing_revision")
      fixture.evidence.push(lifecycle(fixture, "drop_revision", receipt.operation_id));
    else {
      const peer = await playwright.request.newContext({
        extraHTTPHeaders: {
          Authorization: `Bearer ${await seed.accessToken(fixture.data.annotator_email)}`,
        },
      });
      try {
        await json(
          await peer.patch(
            `${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${receipt.created_annotation_id}`,
            {
              headers: {
                "If-Match": String(receipt.result_versions[receipt.created_annotation_id]),
              },
              data: { attributes: { verified: false, count: 19 } },
            },
          ),
        );
      } finally {
        await peer.dispose();
      }
    }
    const before = await read(request, fixture),
      prior = await history(page, fixture.taskId);
    const restorePath = `/api/v1/tasks/${fixture.taskId}/annotations/slices/${receipt.operation_id}:restore`;
    allowHttpError(fixture, restorePath, failure === "expired" ? 410 : 409);
    const rejected = await restore(page, fixture, "undo");
    const rejection = await rejected.json();
    expect(rejected.status()).toBe(failure === "expired" ? 410 : 409);
    await expect(page.getByText("切割恢复失败，历史记录已保留", { exact: true })).toBeVisible();
    const afterHistory = await history(page, fixture.taskId);
    expect(afterHistory.undo).toHaveLength(prior.undo.length);
    expect(afterHistory.redo).toHaveLength(prior.redo.length);
    expect(await reloadAndRead(page, request, fixture)).toEqual(before);
    expect(ledger(fixture).operations).toHaveLength(1);
    fixture.evidence.push({ rejection, historyAfterFailure: afterHistory });
  });
}

test("H4b-3 source locking conflicts preserve preview; active children disable the slice entry", async ({
  page,
  request,
  sliceCase: fixture,
}) => {
  const source = await seedMask(request, fixture);
  await open(page, fixture);
  await begin(page, source.id);
  await preview(page);
  const updateUrl = `${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations/${source.id}`;
  const locked = await json<Annotation>(
    await request.patch(updateUrl, {
      headers: { ...fixture.headers, "If-Match": String(source.version) },
      data: { is_locked: true },
    }),
  );
  const path = `/api/v1/tasks/${fixture.taskId}/annotations/mask-mutations:commit`;
  allowHttpError(fixture, path, 409);
  const rejected = page.waitForResponse((response) => pathOf(response.url()) === path);
  await page.getByTestId("mask-primary-action").click();
  expect((await rejected).status()).toBe(409);
  await expect(controls(page)).toContainText("待原子提交");
  await expect(controls(page).getByRole("alert")).toBeVisible();
  expect(await content(request, fixture, source.id)).toEqual(holed());
  expect(ledger(fixture).operations).toHaveLength(0);
  const unlocked = await json<Annotation>(
    await request.patch(updateUrl, {
      headers: { ...fixture.headers, "If-Match": String(locked.version) },
      data: { is_locked: false },
    }),
  );
  await json(
    await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: fixture.headers,
      data: {
        annotation_type: "bbox",
        tool_unit_id: "region",
        class_name: "car",
        geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.2, h: 0.2 },
        attributes: { verified: true, count: 1 },
        parent_annotation_id: source.id,
      },
    }),
  );
  await reloadAndRead(page, request, fixture);
  await page.getByTestId(`box-list-item-${source.id}`).click();
  await page.locator('button[aria-label="编辑 Mask"]:visible').last().click();
  await openMaskSettings(page);
  await expect(controls(page)).toContainText("就绪");
  await controls(page).getByTitle("Mask 高级工具").click();
  await expect(page.getByRole("menuitem", { name: "直线切割为两个实例" })).toBeDisabled();
  fixture.evidence.push({
    lockedVersion: locked.version,
    unlockedVersion: unlocked.version,
    zeroOperations: ledger(fixture).operations,
  });
});

test("H4b-3 discard on task switch retires the mask slice preview without writes", async ({
  page,
  request,
  seed,
  sliceCase: fixture,
}) => {
  const source = await seedMask(request, fixture);
  const nextId = fixture.data.task_ids[1];
  await seed.advanceTask({
    taskId: nextId,
    toStatus: "pending",
    annotatorEmail: fixture.data.annotator_email,
  });
  const next = await json<{ display_id: string }>(
    await request.get(`${API_BASE}/api/v1/tasks/${nextId}`, { headers: fixture.headers }),
  );
  await open(page, fixture);
  await begin(page, source.id);
  await preview(page);
  // Chromium may report keepalive release as aborted during navigation; verify the DB release below.
  fixture.allowedErrors.add(`request:/api/v1/tasks/${fixture.taskId}/lock:net::ERR_ABORTED`);
  const dialogs: string[] = [];
  const leaveDialog = page.getByRole("alertdialog");
  await page.getByText(next.display_id, { exact: true }).first().click();
  await expect(leaveDialog).toBeVisible({ timeout: 5_000 });
  dialogs.push((await leaveDialog.locator('[data-slot="alert-dialog-title"]').textContent()) ?? "");
  await leaveDialog.getByRole("button", { name: "丢弃并离开", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${nextId}`));
  await expect(stage(page)).toHaveAttribute("data-image-ready", "true");
  await openMaskSettings(page);
  await expect(page.getByTestId("mask-primary-action")).toHaveText("已保存");
  await expect(page.getByTestId("mask-primary-action")).toBeDisabled();
  await expect(controls(page)).not.toContainText("待原子提交");
  await expect(page.getByTestId(`box-list-item-${source.id}`)).toHaveCount(0);
  await expect.poll(() => ledger(fixture).activeLocks).toBe(0);
  expect(dialogs).toEqual(["Mask 尚未保存"]);
  expect(fixture.writes).toHaveLength(0);
  expect(ledger(fixture).operations).toHaveLength(0);
  expect(await content(request, fixture, source.id)).toEqual(holed());
  const other = await json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${nextId}/annotations`, {
      headers: fixture.headers,
    }),
  );
  expect(other).toEqual([]);
  expect(await history(page, fixture.taskId)).toBeNull();
  fixture.evidence.push({ nextTask: nextId, dialogs, noWrites: true });
});
