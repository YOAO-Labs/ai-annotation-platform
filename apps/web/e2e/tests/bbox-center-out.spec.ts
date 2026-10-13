import { openContextToolbar } from "../fixtures/context-toolbar";
import {
  imageWorkbenchAborts,
  isExpectedRequestAbort,
  objectCommentsAborts,
  type RequestErrorSignal,
} from "../helpers/request-errors";
import type { APIRequestContext, APIResponse, Page, Response } from "@playwright/test";
import {
  expect,
  test as base,
  type SeedData,
  type SeedNativeMaskCandidateData,
} from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type Point = [number, number];
type Box = { x: number; y: number; w: number; h: number };
interface Annotation {
  id: string;
  geometry: Box & { type: string };
  class_name: string;
  attributes: Record<string, unknown>;
}
interface Case {
  data: SeedData;
  taskId: string;
  headers: Record<string, string>;
  candidate?: SeedNativeMaskCandidateData;
  writes: Array<{ method: string; path: string; body: unknown }>;
  evidence: unknown[];
  prompts: unknown[];
}
const stage = (page: Page) => page.getByTestId("workbench-stage");
const picker = (page: Page) => page.getByTestId("class-picker-popover");
const mode = (page: Page, name: "角点" | "中心") =>
  page
    .getByRole("group", { name: "画框起点", exact: true })
    .getByRole("button", { name, exact: true });
const pathOf = (url: string) => new URL(url).pathname;
const inference = /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/interactive-annotating$/;
async function json<T>(response: APIResponse | Response): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

// Only model capability/inference responses are deterministic. Geometry and Mask writes use the API.
const prompts = ["point", "interactive_box", "mask"];
const inputs = ["full_image", "point_prompt", "bbox_prompt", "mask_prompt"];
const nativeSetup = {
  name: "H1 deterministic candidate fixture",
  version: "1",
  is_interactive: true,
  supported_prompts: prompts,
  supported_inputs: inputs,
  supported_geometric_outputs: ["polygon", "mask"],
  models: [
    {
      id: "e2e-native-mask",
      display_name: "E2E Native Mask",
      task: "interactive_seg",
      model_family: "e2e",
      composition: "atom",
      is_interactive: true,
      supported_prompts: prompts,
      supported_inputs: inputs,
      supported_geometric_outputs: ["polygon", "mask"],
      resource_profile: { device: "cpu", batchable: false },
    },
  ],
};

const test = base.extend<{ centerCase: Case; withCandidate: boolean }>({
  withCandidate: [false, { option: true }],
  centerCase: async ({ page, request, seed, browser, withCandidate }, provideFixture, testInfo) => {
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
          ...(!withCandidate
            ? { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null }
            : {}),
          tool_bindings: {
            ...project.tool_bindings,
            bbox: {
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
    if (!withCandidate)
      expect(
        (
          await request.delete(
            `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
            { headers },
          )
        ).status(),
      ).toBe(204);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const fixture: Case = { data, taskId, headers, writes: [], evidence: [], prompts: [] };
    if (withCandidate) {
      await seed.configureRasterMask(data.project_id, true);
      fixture.candidate = await seed.nativeMaskCandidate(taskId, { variant: "multimask_donut" });
      await page.route(
        /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/(setup|capabilities)(?:\?|$)/,
        (route) =>
          route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(nativeSetup),
          }),
      );
      await page.route(inference, (route) => {
        const context = route.request().postDataJSON().context;
        if ("output_geometry" in context) fixture.prompts.push(context);
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(fixture.candidate!.response),
        });
      });
    }
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
        isExpectedRequestAbort(error, imageWorkbenchAborts, objectCommentsAborts),
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
        await testInfo.attach("bbox-center-evidence", {
          contentType: "application/json",
          body: JSON.stringify(
            {
              projectId: data.project_id,
              taskId,
              browser: browser.version(),
              viewport: page.viewportSize(),
              dpr: page.isClosed() ? null : await page.evaluate(() => devicePixelRatio),
              modelFixture: withCandidate
                ? "deterministic capability/inference with stored native Mask receipts"
                : null,
              writes: fixture.writes,
              prompts: fixture.prompts,
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
  await page.getByTestId("tool-btn-box").click();
  await expect(mode(page, "角点")).toHaveAttribute("aria-pressed", "true");
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

async function expectCandidatePixel(page: Page, point: Point) {
  const b = await bounds(page);
  await expect
    .poll(() =>
      stage(page).evaluate(
        (node, at) => {
          return Array.from(node.querySelectorAll(".konvajs-content canvas")).some((element) => {
            const canvas = element as HTMLCanvasElement;
            const rect = canvas.getBoundingClientRect();
            const x = Math.floor(((at.x - rect.x) * canvas.width) / rect.width);
            const y = Math.floor(((at.y - rect.y) * canvas.height) / rect.height);
            if (x < 0 || y < 0 || x >= canvas.width || y >= canvas.height) return false;
            try {
              const rgba = canvas.getContext("2d")?.getImageData(x, y, 1, 1).data;
              return !!rgba && rgba[3] > 0 && rgba[2] > rgba[0] + 30 && rgba[0] > rgba[1] + 30;
            } catch (error) {
              if (error instanceof DOMException && error.name === "SecurityError") return false;
              throw error;
            }
          });
        },
        { x: b.x + b.width * point[0], y: b.y + b.height * point[1] },
      ),
    )
    .toBe(true);
}
async function drag(
  page: Page,
  start: Point,
  end: Point,
  options: { alt?: boolean; releaseAlt?: boolean; lateAlt?: boolean } = {},
) {
  const b = await bounds(page);
  const origin = { x: b.x + b.width * start[0], y: b.y + b.height * start[1] };
  await expect
    .poll(() =>
      stage(page).evaluate((node, at) => {
        const target = document.elementFromPoint(at.x, at.y);
        return target instanceof HTMLCanvasElement && node.contains(target);
      }, origin),
    )
    .toBe(true);
  // Independent receipts use the real browser event coordinates, including pixel quantization.
  await page.evaluate(() => {
    const target = window as unknown as { __h1Pointer?: { down?: Point; up?: Point } };
    target.__h1Pointer = {};
    document.addEventListener(
      "mousedown",
      (event) => {
        target.__h1Pointer!.down = [event.clientX, event.clientY];
      },
      { capture: true, once: true },
    );
    document.addEventListener(
      "pointerup",
      (event) => {
        target.__h1Pointer!.up = [event.clientX, event.clientY];
      },
      { capture: true, once: true },
    );
  });
  await page.mouse.move(origin.x, origin.y);
  if (options.alt) await page.keyboard.down("Alt");
  try {
    await page.mouse.down();
    await expect(stage(page)).toHaveAttribute("data-drag-kind", "draw");
    if (options.releaseAlt) await page.keyboard.up("Alt");
    if (options.lateAlt) await page.keyboard.down("Alt");
    await page.mouse.move(b.x + b.width * end[0], b.y + b.height * end[1], { steps: 8 });
    await page.mouse.up();
  } finally {
    await page.keyboard.up("Alt");
  }
  const receipt = await page.evaluate(() => {
    const target = window as unknown as { __h1Pointer?: { down?: Point; up?: Point } };
    const result = target.__h1Pointer;
    delete target.__h1Pointer;
    return result;
  });
  expect(receipt?.down).toBeDefined();
  expect(receipt?.up).toBeDefined();
  const normalize = (p: Point): Point => [(p[0] - b.x) / b.width, (p[1] - b.y) / b.height];
  return { start: normalize(receipt!.down!), end: normalize(receipt!.up!) };
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
function expectCentered(box: Box, receipt: { start: Point; end: Point }) {
  const [cx, cy] = receipt.start;
  expect(box.x + box.w / 2).toBeCloseTo(cx, 5);
  expect(box.y + box.h / 2).toBeCloseTo(cy, 5);
  expect(box.w / 2).toBeCloseTo(Math.min(Math.abs(receipt.end[0] - cx), cx, 1 - cx), 5);
  expect(box.h / 2).toBeCloseTo(Math.min(Math.abs(receipt.end[1] - cy), cy, 1 - cy), 5);
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.x + box.w).toBeLessThanOrEqual(1.000001);
  expect(box.y + box.h).toBeLessThanOrEqual(1.000001);
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

test.describe("H1 center bbox creation", () => {
  test.setTimeout(90_000);
  test.use({ actionTimeout: 10_000 });

  test("H1-1 Alt四向拖动均以按下位置为中心，普通角点创建保持原语义", async ({
    page,
    request,
    centerCase: fixture,
  }) => {
    await open(page, fixture);
    const saved: Annotation[] = [];
    for (const [start, end] of [
      [
        [0.25, 0.25],
        [0.34, 0.33],
      ],
      [
        [0.75, 0.25],
        [0.66, 0.33],
      ],
      [
        [0.25, 0.75],
        [0.34, 0.67],
      ],
      [
        [0.75, 0.75],
        [0.66, 0.67],
      ],
    ] as [Point, Point][]) {
      const receipt = await drag(page, start, end, { alt: true });
      const annotation = await save(page, fixture);
      expectCentered(annotation.geometry, receipt);
      saved.push(annotation);
      fixture.evidence.push({ receipt, annotation });
    }
    const receipt = await drag(page, [0.45, 0.45], [0.56, 0.56]);
    const corner = await save(page, fixture);
    expect(corner.geometry.x).toBeCloseTo(receipt.start[0], 5);
    expect(corner.geometry.y).toBeCloseTo(receipt.start[1], 5);
    expect(corner.geometry.x + corner.geometry.w).toBeCloseTo(receipt.end[0], 5);
    expect(corner.geometry.y + corner.geometry.h).toBeCloseTo(receipt.end[1], 5);
    saved.push(corner);
    expect(fixture.writes).toHaveLength(5);
    await reloadAndRead(page, request, fixture, saved);
  });

  test("H1-2 图像四边对称限制半径，零移动不创建对象", async ({
    page,
    request,
    centerCase: fixture,
  }) => {
    await open(page, fixture);
    const saved: Annotation[] = [];
    for (const [start, end] of [
      [
        [0.05, 0.5],
        [-0.025, 0.58],
      ],
      [
        [0.95, 0.5],
        [1.025, 0.58],
      ],
      [
        [0.5, 0.05],
        [0.58, -0.025],
      ],
      [
        [0.5, 0.95],
        [0.58, 1.025],
      ],
    ] as [Point, Point][]) {
      const receipt = await drag(page, start, end, { alt: true });
      const annotation = await save(page, fixture);
      expectCentered(annotation.geometry, receipt);
      saved.push(annotation);
      fixture.evidence.push({ receipt, annotation });
      if (saved.length === 1) {
        // Collapse once: the preference persists across selections and exposes the right edge.
        const collapseSelection = page.getByRole("button", { name: "收起浮窗", exact: true });
        await collapseSelection.click();
        await expect(collapseSelection).toBeHidden();
      }
    }
    await drag(page, [0.5, 0.5], [0.5, 0.5], { alt: true });
    await expect(stage(page)).toHaveAttribute("data-drag-kind", "none");
    await expect(picker(page)).toBeHidden();
    expect(fixture.writes).toHaveLength(4);
    await reloadAndRead(page, request, fixture, saved);
  });

  test("H1-3 会话中心选项和Alt按下锁存，后按Alt不改变角点，刷新恢复默认", async ({
    page,
    request,
    centerCase: fixture,
  }) => {
    await open(page, fixture);
    await mode(page, "中心").click();
    const first = await drag(page, [0.3, 0.3], [0.4, 0.4]);
    const a = await save(page, fixture);
    expectCentered(a.geometry, first);
    await page.getByTestId("tool-btn-select").click();
    await expect(page.getByTestId("bbox-creation-mode")).toBeHidden();
    await page.getByTestId("tool-btn-box").click();
    await expect(mode(page, "中心")).toHaveAttribute("aria-pressed", "true");
    await mode(page, "角点").click();
    const second = await drag(page, [0.7, 0.3], [0.8, 0.4], { alt: true, releaseAlt: true });
    const b = await save(page, fixture);
    expectCentered(b.geometry, second);
    const third = await drag(page, [0.3, 0.7], [0.4, 0.8], { lateAlt: true });
    const c = await save(page, fixture);
    expect(c.geometry.x).toBeCloseTo(third.start[0], 5);
    expect(c.geometry.y).toBeCloseTo(third.start[1], 5);
    await mode(page, "中心").click();
    await reloadAndRead(page, request, fixture, [a, b, c]);
    await page.getByTestId("tool-btn-box").click();
    await expect(mode(page, "角点")).toHaveAttribute("aria-pressed", "true");
    expect(fixture.writes).toHaveLength(3);
  });

  test("H1-4 已有Alt中心缩放与连续创建仍可用，连续中心框保留类别和默认属性", async ({
    page,
    request,
    centerCase: fixture,
  }) => {
    await open(page, fixture);
    await drag(page, [0.43, 0.43], [0.56, 0.56]);
    const original = await save(page, fixture);
    const b = await bounds(page);
    await page.mouse.move(
      b.x + (original.geometry.x + original.geometry.w) * b.width,
      b.y + (original.geometry.y + original.geometry.h) * b.height,
    );
    await page.keyboard.down("Alt");
    const updated = page.waitForResponse(
      (r) =>
        pathOf(r.url()) === `/api/v1/tasks/${fixture.taskId}/annotations/${original.id}` &&
        r.request().method() === "PATCH",
    );
    try {
      await page.mouse.down();
      await expect(stage(page)).toHaveAttribute("data-drag-kind", "resize");
      await page.mouse.move(
        b.x + (original.geometry.x + original.geometry.w + 0.03) * b.width,
        b.y + (original.geometry.y + original.geometry.h + 0.02) * b.height,
        { steps: 10 },
      );
      await page.mouse.up();
    } finally {
      await page.keyboard.up("Alt");
    }
    const resized = await json<Annotation>(await updated);
    expect(resized.geometry.x + resized.geometry.w / 2).toBeCloseTo(
      original.geometry.x + original.geometry.w / 2,
      5,
    );
    expect(resized.geometry.y + resized.geometry.h / 2).toBeCloseTo(
      original.geometry.y + original.geometry.h / 2,
      5,
    );
    expect(resized.geometry.w).toBeGreaterThan(original.geometry.w);
    await mode(page, "中心").click();
    const controls = page.getByTestId("continuous-creation-controls");
    await controls.getByRole("switch", { name: "连续创建", exact: true }).click();
    await controls.getByRole("combobox", { name: "创建工具单元" }).selectOption("bbox");
    await controls.getByRole("button", { name: "car", exact: true }).click();
    const saved = [resized];
    for (const start of [
      [0.25, 0.25],
      [0.75, 0.25],
      [0.5, 0.75],
    ] as Point[]) {
      const response = page.waitForResponse(
        (r) =>
          pathOf(r.url()) === `/api/v1/tasks/${fixture.taskId}/annotations` &&
          r.request().method() === "POST",
      );
      const receipt = await drag(page, start, [start[0] + 0.06, start[1] + 0.06]);
      const annotation = await json<Annotation>(await response);
      expectCentered(annotation.geometry, receipt);
      expect(annotation.class_name).toBe("car");
      expect(annotation.attributes).toMatchObject({ verified: false, count: 0 });
      await expect(picker(page)).toBeHidden();
      await expect(stage(page)).toHaveAttribute("data-pending-drawing", "false");
      saved.push(annotation);
      fixture.evidence.push({ receipt, annotation });
    }
    expect(fixture.writes.filter((w) => w.method === "POST")).toHaveLength(4);
    expect(fixture.writes.filter((w) => w.method === "PATCH")).toHaveLength(1);
    await reloadAndRead(page, request, fixture, saved);
  });
});

test.describe("H1 candidate modifier compatibility", () => {
  test.use({ withCandidate: true, actionTimeout: 10_000 });
  test("H1-4 中心选项保留时Ctrl/Cmd仍选择SAM候选，采纳结果与所选原生像素一致", async ({
    page,
    request,
    centerCase: fixture,
  }) => {
    test.setTimeout(90_000);
    await open(page, fixture);
    await mode(page, "中心").click();
    await page.getByTestId("tool-btn-smart-point").click();
    await openContextToolbar(page, "interactive");
    await expect(page.getByTestId("single-frame-output-geometry-select")).toHaveValue("mask");
    const b = await bounds(page);
    const generated = page.waitForResponse(inference);
    await page.mouse.click(b.x + b.width * 0.6, b.y + b.height * 0.5);
    expect((await generated).ok()).toBe(true);
    await expect(page.getByTestId("interactive-toolbar")).toBeHidden();
    const count = page.getByTestId("interactive-candidate-count");
    await expect(count).toContainText(/1\s*\/\s*3/);
    // The native preview owner decodes only the active mask. Modifier clicks must consume
    // its actual foreground without adding a fresh inference prompt or bbox draft.
    await page.getByTestId("interactive-settings-trigger").hover();
    await page.getByTestId("interactive-candidate-next").click();
    await expect(count).toContainText(/2\s*\/\s*3/);
    await page.getByTestId("interactive-settings-trigger").hover();
    await page.getByTestId("interactive-candidate-next").click();
    await expect(count).toContainText(/3\s*\/\s*3/);
    for (const [modifier, point, expected] of [
      ["Control", [0.8, 0.7], 3],
      ["Meta", [0.2, 0.6], 2],
    ] as const) {
      if (expected === 2) {
        await page.getByTestId("interactive-settings-trigger").hover();
        await page.getByTestId("interactive-candidate-previous").click();
        await expect(count).toContainText(/2\s*\/\s*3/);
      }
      await expectCandidatePixel(page, [...point]);
      await page.keyboard.down(modifier);
      try {
        await page.mouse.click(b.x + b.width * point[0], b.y + b.height * point[1]);
      } finally {
        await page.keyboard.up(modifier);
      }
      await expect(stage(page)).toHaveAttribute("data-drag-kind", "none");
      await expect(picker(page)).toBeHidden();
      await expect(count).toContainText(new RegExp(`${expected}\\s*\\/\\s*3`));
    }
    expect(fixture.writes).toEqual([]);
    expect(fixture.prompts).toHaveLength(1);
    await page.getByTestId("interactive-settings-trigger").hover();
    await page.getByTestId("interactive-candidate-accept").click();
    await expect(picker(page)).toBeVisible();
    const accepted = page.waitForResponse(
      (r) =>
        pathOf(r.url()) === `/api/v1/tasks/${fixture.taskId}/ai-mask-candidates/accept` &&
        r.request().method() === "POST",
    );
    await picker(page).locator("span").filter({ hasText: /^car$/ }).click();
    const result = await json<{ annotation: { id: string; annotation_type: string } }>(
      await accepted,
    );
    expect(result.annotation.annotation_type).toBe("raster_mask");
    await expect(picker(page)).toBeHidden();
    await page.getByTestId("tool-btn-box").click();
    await expect(mode(page, "中心")).toHaveAttribute("aria-pressed", "true");
    await page.reload();
    await expect(stage(page)).toHaveAttribute("data-image-ready", "true");
    const rle = await json(
      await request.get(`${API_BASE}/api/v1/annotations/${result.annotation.id}/mask-content`, {
        headers: fixture.headers,
      }),
    );
    expect(rle).toEqual(fixture.candidate!.rles[1]);
    fixture.evidence.push({ accepted: result, persistedRle: rle });
  });
});
