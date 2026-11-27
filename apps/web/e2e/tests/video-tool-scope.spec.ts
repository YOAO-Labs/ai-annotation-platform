import { canvasBottomDivider } from "../fixtures/workbench-panel-actions";
import type { APIRequestContext, APIResponse, Locator, Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";

import { closeMaskSettings, openMaskSettings } from "../fixtures/mask-toolbar";
import { expect, test as base, type SeedData } from "../fixtures/seed";
import { layoutCommand } from "../helpers/workbench-layout";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const KINDS = ["bbox", "polygon", "polyline", "mask"] as const;
type Kind = (typeof KINDS)[number];
type Scope = "frame" | "track";
type Point = [number, number];
type MaskReference = { object_key: string; [key: string]: unknown };
interface Keyframe {
  frame_index: number;
  source?: string;
  bbox?: { x: number; y: number; w: number; h: number };
  points?: Point[];
  mask?: MaskReference;
}
interface Annotation {
  id: string;
  annotation_type: string;
  class_name: string;
  geometry: {
    type: string;
    frame_index?: number;
    x?: number;
    y?: number;
    w?: number;
    h?: number;
    bbox?: Keyframe["bbox"];
    points?: Point[];
    holes?: Point[][];
    mask?: MaskReference;
    track_id?: string;
    keyframes?: Keyframe[];
    outside?: Array<{ from: number; to: number }>;
  };
}
interface Binding {
  enabled: boolean;
  classes: Array<{ name: string; color: string }>;
  attribute_schema: { fields: never[] };
  video_modes: { box: boolean; track: boolean };
}
interface ScopeCase {
  data: SeedData;
  taskId: string;
  token: string;
  bindings: Record<string, Binding>;
  mask: MaskReference;
  annotationIds: string[];
  maskObjectKeys: Set<string>;
  writes: string[];
}

const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
const scopeControl = (page: Page, scope: Scope, includeHidden = false) =>
  page.getByRole("button", {
    name: scope === "frame" ? "单帧范围" : "轨迹范围",
    exact: true,
    includeHidden,
  });
const stage = (page: Page) => page.getByTestId("video-konva-stage");
const picker = (page: Page) => page.getByTestId("class-picker-popover");
const hint = (page: Page) => page.getByTestId("video-creation-scope-hint");
const toolFor = (kind: Kind, scope: Scope) =>
  scope === "frame"
    ? kind === "bbox"
      ? "box"
      : kind
    : kind === "bbox"
      ? "track"
      : `${kind}-track`;

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

function record(fixture: ScopeCase, annotation: Annotation) {
  if (!fixture.annotationIds.includes(annotation.id)) fixture.annotationIds.push(annotation.id);
  const masks = [
    annotation.geometry.mask,
    ...(annotation.geometry.keyframes ?? []).map((key) => key.mask),
  ];
  for (const mask of masks) if (mask) fixture.maskObjectKeys.add(mask.object_key);
  return annotation;
}

async function annotations(request: APIRequestContext, fixture: ScopeCase) {
  return json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
      headers: auth(fixture.token),
    }),
  );
}

const test = base.extend<{ scopeCase: ScopeCase }>({
  scopeCase: async ({ page, request, seed, owned: data }, provideFixture, testInfo) => {
    const { task_id: taskId } = await seed.videoTask(data.project_id);
    const token = await seed.accessToken(data.admin_email);
    await seed.configureRasterMask(data.project_id, true);
    const candidate = await seed.nativeMaskCandidate(taskId, { variant: "multimask_donut" });
    const bindings: Record<string, Binding> = Object.fromEntries(
      ["bbox", "region", "polyline", "rotated_bbox", "keypoint"].map((unit) => [
        unit,
        {
          enabled: true,
          classes: [{ name: "car", color: "#22c55e" }],
          attribute_schema: { fields: [] },
          video_modes: { box: true, track: true },
        },
      ]),
    );
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
        headers: auth(token),
        data: {
          ai_enabled: false,
          ai_interactive_enabled: false,
          ml_backend_id: null,
          tool_bindings: bindings,
        },
      }),
    );
    expect(
      (
        await request.delete(
          `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
          {
            headers: auth(token),
          },
        )
      ).status(),
    ).toBe(204);
    const mask = await json<MaskReference>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
        headers: auth(token),
        data: candidate.rles.at(-1) ?? candidate.rle,
      }),
    );
    await seed.injectToken(page, data.admin_email);
    await page.setViewportSize({ width: 1366, height: 900 });
    const fixture: ScopeCase = {
      data,
      taskId,
      token,
      bindings,
      mask,
      annotationIds: [],
      maskObjectKeys: new Set([mask.object_key]),
      writes: [],
    };
    const errors: string[] = [];
    const isBusinessPath = (path: string) =>
      path.startsWith(`/api/v1/tasks/${taskId}`) ||
      path.startsWith(`/api/v1/projects/${data.project_id}`) ||
      path.startsWith("/api/v1/annotations/");
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(`console: ${message.text()}`);
    });
    page.on("response", (response) => {
      const path = new URL(response.url()).pathname;
      if (response.status() >= 400 && isBusinessPath(path)) {
        errors.push(`${response.request().method()} ${path}: ${response.status()}`);
      }
    });
    page.on("request", (outgoing) => {
      const path = new URL(outgoing.url()).pathname;
      if (
        ["POST", "PATCH", "PUT", "DELETE"].includes(outgoing.method()) &&
        (path === `/api/v1/tasks/${taskId}/annotations` ||
          path.startsWith(`/api/v1/tasks/${taskId}/annotations/`) ||
          path.startsWith(`/api/v1/tasks/${taskId}/video/tracks/`) ||
          path === "/api/v1/annotations/mask-mutations:commit")
      ) {
        fixture.writes.push(`${outgoing.method()} ${path}`);
      }
    });
    try {
      await provideFixture(fixture);
      expect(errors).toEqual([]);
    } finally {
      await writeFile(
        testInfo.outputPath("video-tool-scope-metadata.json"),
        JSON.stringify(
          {
            projectId: data.project_id,
            taskIds: [taskId],
            annotationIds: fixture.annotationIds,
            maskObjectKeys: [...fixture.maskObjectKeys],
            businessErrors: errors,
          },
          null,
          2,
        ),
      );
    }
  },
});

async function open(page: Page, fixture: ScopeCase, layout = "标准标注布局") {
  await page.goto(`/projects/${fixture.data.project_id}/annotate?task=${fixture.taskId}`);
  await expect(stage(page)).toBeVisible({ timeout: 25_000 });
  await expect(stage(page).locator(".konvajs-content > canvas").first()).toBeVisible();
  await layoutCommand(page, layout);
  await expect(page.getByTestId("video-tool-scope")).toBeVisible();
}

async function expectScope(page: Page, scope: Scope) {
  await expect(page.getByTestId("video-tool-scope")).toHaveAttribute("data-scope", scope);
  await expect(scopeControl(page, scope, true)).toHaveAttribute("aria-pressed", "true");
  await expect(scopeControl(page, scope === "frame" ? "track" : "frame", true)).toHaveAttribute(
    "aria-pressed",
    "false",
  );
}

async function chooseTool(page: Page, id: string) {
  const button = page.getByTestId(`video-tool-btn-${id}`);
  if (await button.isVisible()) await button.click();
  else {
    await page.getByTestId("tool-dock-more").click();
    await page.getByTestId(`tool-overflow-item-${id}`).click();
    await expect(page.getByTestId("tool-dock-menu")).toBeHidden();
  }
  await expect(button).toHaveAttribute("aria-pressed", "true");
  await expect(button).toBeInViewport();
}

async function key(page: Page, value: string) {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press(value);
}

async function seek(page: Page, target: number) {
  await key(page, "k");
  let current = Number(await stage(page).getAttribute("data-video-frame-index"));
  while (current !== target) {
    const forward = target > current;
    current += forward ? 1 : -1;
    await key(page, forward ? "ArrowRight" : "ArrowLeft");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(current));
  }
}

async function renderedMediaBounds(stage: Locator) {
  const stageBox = await stage.boundingBox();
  if (!stageBox) throw new Error("视频工作台没有可见边界");
  const { x, y, width, height } = await stage.evaluate((element) => ({
    x: Number(element.getAttribute("data-media-x")),
    y: Number(element.getAttribute("data-media-y")),
    width: Number(element.getAttribute("data-media-width")),
    height: Number(element.getAttribute("data-media-height")),
  }));
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    !Number.isFinite(width) ||
    !Number.isFinite(height) ||
    width <= 0 ||
    height <= 0
  ) {
    throw new Error("视频工作台未暴露有效的媒体渲染区");
  }
  return { x: stageBox.x + x, y: stageBox.y + y, width, height };
}

async function clickPoint(page: Page, point: Point) {
  const bounds = await renderedMediaBounds(stage(page));
  await page.mouse.click(bounds.x + bounds.width * point[0], bounds.y + bounds.height * point[1]);
  await page.waitForTimeout(450);
}

async function draw(page: Page, kind: Kind, scope: Scope) {
  const expectedHint = scope === "frame" ? "仅当前源帧" : "新建轨迹，从当前源帧开始";
  if (kind === "polygon" || kind === "polyline") {
    for (const point of [
      [0.32, 0.3],
      [0.58, 0.3],
      [0.53, 0.58],
    ] as Point[]) {
      await clickPoint(page, point);
      await expect(hint(page)).toHaveText(expectedHint);
    }
    await key(page, "Enter");
  } else {
    const bounds = await renderedMediaBounds(stage(page));
    await page.mouse.move(bounds.x + bounds.width * 0.32, bounds.y + bounds.height * 0.3);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width * 0.58, bounds.y + bounds.height * 0.58, {
      steps: 10,
    });
    await expect(hint(page)).toHaveText(expectedHint);
    await page.mouse.up();
    if (kind === "mask") {
      await openMaskSettings(page);
      await page.getByTestId("mask-primary-action").click();
    }
  }
  await expect(picker(page)).toBeVisible();
}

async function save(page: Page, fixture: ScopeCase) {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/tasks/${fixture.taskId}/annotations`,
  );
  await picker(page).locator("span").filter({ hasText: /^car$/ }).click();
  const response = await responsePromise;
  expect(response.ok(), await response.text()).toBe(true);
  const payload = response.request().postDataJSON() as Annotation;
  const saved = record(fixture, (await response.json()) as Annotation);
  await expect(picker(page)).toBeHidden();
  return { payload, saved };
}

async function seedTracks(request: APIRequestContext, fixture: ScopeCase) {
  const tracks = {} as Record<Kind, Annotation>;
  for (const kind of KINDS) {
    const shape =
      kind === "bbox"
        ? { bbox: { x: 0.125, y: 0.125, w: 0.125, h: 0.25 } }
        : kind === "mask"
          ? { mask: fixture.mask }
          : {
              points: (kind === "polygon"
                ? [
                    [0.125, 0.5],
                    [0.25, 0.5],
                    [0.25, 0.625],
                  ]
                : [
                    [0.625, 0.5],
                    [0.75, 0.625],
                  ]) as Point[],
            };
    tracks[kind] = record(
      fixture,
      await json<Annotation>(
        await request.post(`${API_BASE}/api/v1/tasks/${fixture.taskId}/annotations`, {
          headers: auth(fixture.token),
          data: {
            annotation_type: `video_track_${kind}`,
            tool_unit_id: kind === "polygon" || kind === "mask" ? "region" : kind,
            class_name: "car",
            geometry: {
              type: `video_track_${kind}`,
              track_id: `f2-${kind}`,
              keyframes: [0, 10].map((frame_index) => ({
                frame_index,
                ...shape,
                source: "manual",
              })),
              outside: [],
            },
          },
        }),
      ),
    );
  }
  return tracks;
}

function row(page: Page, track: Annotation): Locator {
  return track.geometry.type === "video_track_bbox"
    ? page.getByTestId("video-track-row").filter({ hasText: track.geometry.track_id!.slice(0, 8) })
    : page.getByTestId(
        track.geometry.type === "video_track_mask"
          ? `video-mask-track-${track.id}`
          : `box-list-item-${track.id}`,
      );
}

async function collapseSelectedCard(page: Page) {
  const collapse = page.getByRole("button", { name: "收起浮窗", exact: true });
  if (await collapse.isVisible()) await collapse.click();
}

async function selectTrack(page: Page, track: Annotation) {
  await row(page, track).click();
  await expect(page.getByTestId("video-track-context-bar")).toContainText(
    track.geometry.track_id!.slice(0, 8),
  );
  await collapseSelectedCard(page);
}

async function confirmSwitch(page: Page, decision: "继续绘制" | "丢弃并切换") {
  const dialog = page.getByRole("alertdialog").filter({ hasText: "切换视频工具" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: decision, exact: true }).click();
  await expect(dialog).toBeHidden();
}

async function accessibleTools(page: Page) {
  const ids = await page
    .getByTestId("tool-dock")
    .locator("[data-tool-dock-entry]")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-tool-dock-entry")!).filter((id) => id !== "more"),
    );
  if (await page.getByTestId("tool-dock-more").isVisible()) {
    await page.getByTestId("tool-dock-more").click();
    const menu = page.getByTestId("tool-dock-menu");
    await expect(menu).toBeVisible();
    ids.push(
      ...(await menu
        .getByRole("menuitemradio")
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-testid")!.replace("tool-overflow-item-", "")),
        )),
    );
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    await expect(page.getByTestId("tool-dock-more")).toBeFocused();
  }
  expect(new Set(ids).size).toBe(ids.length);
  return ids.sort();
}

async function shortenCanvas(page: Page) {
  const divider = await canvasBottomDivider(page);
  await page.mouse.move(divider.x, divider.y);
  await page.mouse.down();
  await page.mouse.move(divider.x, divider.top + 210, { steps: 10 });
  await page.mouse.up();
  await expect
    .poll(async () => (await page.locator('[data-workbench-panel="canvas"]').boundingBox())!.height)
    .toBeLessThan(divider.height);
}

test.describe("视频工具作用范围：真实输入与持久化", () => {
  test.setTimeout(100_000);
  test.skip(
    process.env.PLAYWRIGHT_RASTER_MASK_MATRIX !== "native",
    "requires native Mask API writes",
  );

  for (const kind of KINDS) {
    test(`F2-1 ${kind} 同一几何的单帧与新轨迹分别保存并刷新读回`, async ({
      page,
      request,
      scopeCase,
    }) => {
      await open(page, scopeCase);
      const saved: Annotation[] = [];
      for (const scope of ["frame", "track"] as const) {
        const frameIndex = scope === "frame" ? 3 : 6;
        await seek(page, frameIndex);
        await scopeControl(page, scope).click();
        await expectScope(page, scope);
        await chooseTool(page, toolFor(kind, scope));
        if (kind === "mask") {
          await openMaskSettings(page);
          await page.getByTestId("mask-radius-slider").fill("2");
          await closeMaskSettings(page);
        }
        await draw(page, kind, scope);
        const result = await save(page, scopeCase);
        const type = `video_${scope === "track" ? "track_" : ""}${kind}`;
        expect(result.payload.annotation_type).toBe(type);
        expect(result.payload.geometry.type).toBe(type);
        expect(result.saved.class_name).toBe("car");
        if (scope === "frame") {
          expect(result.payload.geometry.frame_index).toBe(frameIndex);
          expect(result.payload.geometry).not.toHaveProperty("track_id");
          expect(result.payload.geometry).not.toHaveProperty("keyframes");
        } else {
          expect(result.payload.geometry.track_id).toBeTruthy();
          expect(
            result.payload.geometry.keyframes?.map((keyframe) => keyframe.frame_index),
          ).toEqual([frameIndex]);
        }
        saved.push(result.saved);
        await page.reload();
        await expect(stage(page)).toBeVisible();
        expect(
          (await annotations(request, scopeCase)).find(
            (annotation) => annotation.id === result.saved.id,
          )?.geometry,
        ).toEqual(result.saved.geometry);
      }
      const first = saved[0].geometry;
      const second = saved[1].geometry.keyframes![0];
      if (kind === "mask") {
        const pixels = await Promise.all(
          saved.map(async (annotation, index) =>
            json(
              await request.get(
                `${API_BASE}/api/v1/annotations/${annotation.id}/mask-content${index === 1 ? "/6" : ""}`,
                { headers: auth(scopeCase.token) },
              ),
            ),
          ),
        );
        expect(pixels[1]).toEqual(pixels[0]);
      } else if (kind === "bbox") {
        for (const key of ["x", "y", "w", "h"] as const)
          expect(second.bbox![key]).toBeCloseTo(first[key]!, 2);
      } else {
        expect(second.points).toHaveLength(first.points!.length);
        first.points!.forEach((point, index) =>
          point.forEach((coordinate, axis) =>
            expect(second.points![index][axis]).toBeCloseTo(coordinate, 2),
          ),
        );
      }
      expect(await annotations(request, scopeCase)).toHaveLength(2);
      expect(scopeCase.writes).toHaveLength(2);
    });
  }

  test("F2-2 选中轨迹后 B/P/M 明确进入单帧，后续渲染不覆盖用户命令", async ({
    page,
    request,
    scopeCase,
  }) => {
    const tracks = await seedTracks(request, scopeCase);
    await open(page, scopeCase);
    for (const [shortcut, tool] of [
      ["b", "box"],
      ["p", "polygon"],
      ["m", "mask"],
    ]) {
      await selectTrack(page, tracks.bbox);
      await expectScope(page, "track");
      await key(page, shortcut);
      await expectScope(page, "frame");
      await expect(page.getByTestId(`video-tool-btn-${tool}`)).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await seek(page, 1);
      await expectScope(page, "frame");
      await expect(page.getByTestId(`video-tool-btn-${tool}`)).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(page.getByTestId("video-track-context-bar")).toContainText("f2-bbox");
      await chooseTool(page, "select");
      await clickPoint(page, [0.94, 0.9]);
      await seek(page, 0);
    }
    await key(page, "t");
    await expectScope(page, "track");
    await expect(page.getByTestId("video-tool-btn-track")).toHaveAttribute("aria-pressed", "true");
    expect(scopeCase.writes).toEqual([]);
  });

  test("F2-3 四类选中事件映射精确工具，选择工具与空白取消保留范围", async ({
    page,
    request,
    scopeCase,
  }) => {
    const tracks = await seedTracks(request, scopeCase);
    await open(page, scopeCase);
    for (const kind of KINDS) {
      await scopeControl(page, "frame").click();
      await chooseTool(page, "select");
      await selectTrack(page, tracks[kind]);
      await expectScope(page, "track");
      await expect(page.getByTestId(`video-tool-btn-${toolFor(kind, "track")}`)).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await chooseTool(page, "select");
      await expectScope(page, "track");
      await clickPoint(page, [0.94, 0.9]);
      await expect(page.getByTestId("video-track-context-bar")).toContainText("选择轨迹");
      await expectScope(page, "track");
    }
    expect(scopeCase.writes).toEqual([]);
    expect(await annotations(request, scopeCase)).toHaveLength(4);
  });

  test("带孔单帧多边形整体拖动、边缘限位、撤销重做与刷新", async ({ page, request, scopeCase }) => {
    const frame = 2;
    const rectangle = (left: number, top: number, right: number, bottom: number): Point[] => [
      [left, top],
      [right, top],
      [right, bottom],
      [left, bottom],
    ];
    const geometry = {
      type: "video_polygon",
      frame_index: frame,
      points: rectangle(0.18, 0.2, 0.48, 0.48),
      holes: [rectangle(0.27, 0.27, 0.31, 0.32), rectangle(0.37, 0.34, 0.44, 0.43)],
    };
    const endpoint = `/api/v1/tasks/${scopeCase.taskId}/annotations`;
    const original = record(
      scopeCase,
      await json<Annotation>(
        await request.post(`${API_BASE}${endpoint}`, {
          headers: auth(scopeCase.token),
          data: {
            annotation_type: "video_polygon",
            tool_unit_id: "region",
            class_name: "car",
            geometry,
          },
        }),
      ),
    );
    const annotationPath = `${endpoint}/${original.id}`;
    const savedGeometry = async (action: () => Promise<void>) => {
      const responsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "PATCH" &&
          new URL(response.url()).pathname === annotationPath,
      );
      await action();
      const response = await responsePromise;
      const stored = (await json<Annotation>(response)).geometry;
      expect(response.request().postDataJSON().geometry).toEqual(stored);
      return stored as typeof geometry;
    };
    const expectStored = async (expected: typeof geometry) => {
      const stored = await annotations(request, scopeCase);
      expect(stored.map(({ id, geometry }) => ({ id, geometry }))).toEqual([
        { id: original.id, geometry: expected },
      ]);
    };
    const translate = (source: typeof geometry, dx: number, dy: number) => {
      const move = ([x, y]: Point): Point => [x + dx, y + dy];
      return {
        ...source,
        points: source.points.map(move),
        holes: source.holes.map((ring) => ring.map(move)),
      };
    };
    const expectTranslation = (
      before: typeof geometry,
      after: typeof geometry,
      expectedX: number,
      expectedY: number,
    ) => {
      const dx = after.points[0][0] - before.points[0][0];
      const dy = after.points[0][1] - before.points[0][1];
      expect(dx).toBeCloseTo(expectedX, 2);
      expect(dy).toBeCloseTo(expectedY, 2);
      expect(after).toMatchObject({ type: "video_polygon", frame_index: frame });
      const expected = translate(before, dx, dy);
      const actualRings = [after.points, ...after.holes];
      expect(actualRings).toHaveLength(1 + before.holes.length);
      [expected.points, ...expected.holes].forEach((ring, index) => {
        expect(actualRings[index]).toHaveLength(ring.length);
        ring.forEach(([x, y], vertex) => {
          const [actualX, actualY] = actualRings[index][vertex];
          expect(actualX).toBeGreaterThanOrEqual(0);
          expect(actualX).toBeLessThanOrEqual(1);
          expect(actualY).toBeGreaterThanOrEqual(0);
          expect(actualY).toBeLessThanOrEqual(1);
          expect(actualX).toBeCloseTo(x, 10);
          expect(actualY).toBeCloseTo(y, 10);
        });
      });
    };

    const surface = stage(page);
    const tracksCanvas = surface.locator(".konvajs-content > canvas").nth(1);
    const clientPoint = async ([x, y]: Point): Promise<Point> => {
      const bounds = await renderedMediaBounds(surface);
      return [bounds.x + x * bounds.width, bounds.y + y * bounds.height];
    };
    const expectPaint = async (solid: Point, empty: Point[]) => {
      const locations = await Promise.all([solid, ...empty].map(clientPoint));
      await expect
        .poll(() =>
          tracksCanvas.evaluate((node, positions) => {
            const canvas = node as HTMLCanvasElement;
            const rect = canvas.getBoundingClientRect();
            const context = canvas.getContext("2d")!;
            return positions.map(([clientX, clientY]) => {
              const x = Math.floor(((clientX - rect.left) * canvas.width) / rect.width);
              const y = Math.floor(((clientY - rect.top) * canvas.height) / rect.height);
              return context.getImageData(x, y, 1, 1).data[3] > 0;
            });
          }, locations),
        )
        .toEqual([true, ...empty.map(() => false)]);
    };
    const interiorSamples = (shape: typeof geometry): Point[] =>
      shape.holes.map(([a, , c]) => [(a[0] + c[0]) / 2, (a[1] + c[1]) / 2]);
    const solidSample = (shape: typeof geometry): Point => [
      shape.points[0][0] + 0.045,
      shape.points[0][1] + 0.05,
    ];
    const moveBody = async (before: typeof geometry, end: Point, dx: number, dy: number) => {
      const preview = translate(before, dx, dy);
      const writesBefore = scopeCase.writes.length;
      await page.mouse.move(...(await clientPoint(solidSample(before))));
      await page.mouse.down();
      await page.mouse.move(...(await clientPoint(end)));
      await expectPaint(solidSample(preview), [...interiorSamples(preview), solidSample(before)]);
      await expect(surface).toHaveAttribute("data-video-frame-index", String(frame));
      expect(scopeCase.writes).toHaveLength(writesBefore);
      const after = await savedGeometry(() => page.mouse.up());
      expectTranslation(before, after, dx, dy);
      await expectScope(page, "frame");
      await expectPaint(solidSample(after), interiorSamples(after));
      await expectStored(after);
      return after;
    };

    await open(page, scopeCase);
    await seek(page, frame);
    await scopeControl(page, "frame").click();
    await chooseTool(page, "select");
    await expectPaint(solidSample(geometry), interiorSamples(geometry));
    await page.mouse.click(...(await clientPoint(solidSample(geometry))));
    await collapseSelectedCard(page);

    const moved = await moveBody(geometry, [0.405, 0.39], 0.18, 0.14);
    const boundaryDx = 1 - moved.points[1][0];
    const clamped = await moveBody(moved, [0.93, solidSample(moved)[1]], boundaryDx, 0);
    expect(clamped.points[1][0]).toBe(1);
    expect(await savedGeometry(() => key(page, "ControlOrMeta+z"))).toEqual(moved);
    await expectPaint(solidSample(moved), [...interiorSamples(moved), solidSample(clamped)]);
    await expectStored(moved);
    expect(await savedGeometry(() => key(page, "ControlOrMeta+Shift+z"))).toEqual(clamped);
    await expectStored(clamped);
    await expectPaint(solidSample(clamped), [...interiorSamples(clamped), solidSample(moved)]);

    await page.reload();
    await expect(surface).toBeVisible({ timeout: 25_000 });
    await seek(page, frame);
    await expectStored(clamped);
    await expectPaint(solidSample(clamped), interiorSamples(clamped));
    await chooseTool(page, "select");
    await page.mouse.click(...(await clientPoint(solidSample(clamped))));
    await collapseSelectedCard(page);
    const [vertexX, vertexY] = clamped.points[0];
    await page.mouse.move(...(await clientPoint(clamped.points[0])));
    await page.mouse.down();
    await page.mouse.move(...(await clientPoint([vertexX - 0.05, vertexY - 0.05])));
    const reshaped = await savedGeometry(() => page.mouse.up());
    expect(reshaped.holes).toEqual(clamped.holes);
    expect(reshaped.points.slice(1)).toEqual(clamped.points.slice(1));
    expect(reshaped.points[0][0]).toBeCloseTo(vertexX - 0.05, 2);
    expect(reshaped.points[0][1]).toBeCloseTo(vertexY - 0.05, 2);
    await expectStored(reshaped);
    await expectPaint(solidSample(clamped), interiorSamples(reshaped));
    await expectScope(page, "frame");
    expect(scopeCase.writes).toEqual(Array(5).fill(`PATCH ${annotationPath}`));
  });

  test("F2-4 未完成点集切范围先确认，继续后仍按原单帧提交", async ({
    page,
    request,
    scopeCase,
  }) => {
    await open(page, scopeCase);
    await chooseTool(page, "polygon");
    await clickPoint(page, [0.32, 0.3]);
    await clickPoint(page, [0.58, 0.3]);
    await expect(hint(page)).toHaveText("仅当前源帧");
    await scopeControl(page, "track").click();
    await confirmSwitch(page, "继续绘制");
    await expectScope(page, "frame");
    expect(scopeCase.writes).toEqual([]);
    await clickPoint(page, [0.53, 0.58]);
    await key(page, "Enter");
    const first = await save(page, scopeCase);
    expect(first.payload.geometry.type).toBe("video_polygon");
    expect(first.payload.geometry.points).toHaveLength(3);
    await seek(page, 1);
    await chooseTool(page, "polygon");
    await clickPoint(page, [0.32, 0.3]);
    await scopeControl(page, "track").click();
    await confirmSwitch(page, "丢弃并切换");
    await expectScope(page, "track");
    await key(page, "Enter");
    await expect(picker(page)).toBeHidden();
    await expect(hint(page)).toBeHidden();
    expect(scopeCase.writes).toHaveLength(1);
    await page.reload();
    expect(
      (await annotations(request, scopeCase)).map((annotation) => annotation.geometry.type),
    ).toEqual(["video_polygon"]);
  });

  test("F2-4 类别选择待定时切范围不提前保存未分类，继续或丢弃均保留准确语义", async ({
    page,
    request,
    scopeCase,
  }) => {
    await open(page, scopeCase);
    await chooseTool(page, "polygon");
    await draw(page, "polygon", "frame");
    await scopeControl(page, "track").click();
    await confirmSwitch(page, "继续绘制");
    await expectScope(page, "frame");
    await expect(picker(page)).toBeVisible();
    expect(await annotations(request, scopeCase)).toEqual([]);
    expect(scopeCase.writes).toEqual([]);
    const first = await save(page, scopeCase);
    expect(first.payload.geometry.type).toBe("video_polygon");
    await seek(page, 1);
    await chooseTool(page, "polygon");
    await draw(page, "polygon", "frame");
    await scopeControl(page, "track").click();
    await confirmSwitch(page, "丢弃并切换");
    await expectScope(page, "track");
    await expect(picker(page)).toBeHidden();
    expect(scopeCase.writes).toHaveLength(1);
    await page.reload();
    const stored = await annotations(request, scopeCase);
    expect(stored).toHaveLength(1);
    expect(stored[0].class_name).toBe("car");
    expect(stored[0].geometry).toEqual(first.saved.geometry);
  });

  test("F2-4 两点草稿点击矩形轨迹行，继续保留原源帧而丢弃才跳 F0", async ({
    page,
    request,
    scopeCase,
  }) => {
    const tracks = await seedTracks(request, scopeCase);
    const points: Point[] = [
      [0.32, 0.3],
      [0.58, 0.3],
      [0.53, 0.58],
    ];
    await open(page, scopeCase);
    await seek(page, 8);
    await chooseTool(page, "polygon");
    for (const point of points.slice(0, 2)) await clickPoint(page, point);

    await row(page, tracks.bbox).click();
    await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toBeVisible();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await expectScope(page, "frame");
    await expect(page.getByTestId("video-tool-btn-polygon")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(scopeCase.writes).toEqual([]);
    await confirmSwitch(page, "继续绘制");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await expectScope(page, "frame");
    await expect(hint(page)).toHaveText("仅当前源帧");
    await clickPoint(page, points[2]);
    await key(page, "Enter");
    const first = await save(page, scopeCase);
    expect(first.payload.geometry.type).toBe("video_polygon");
    expect(first.payload.geometry.frame_index).toBe(8);
    expect(first.payload.geometry.points).toHaveLength(3);
    points.forEach((point, index) =>
      point.forEach((coordinate, axis) =>
        expect(first.payload.geometry.points![index][axis]).toBeCloseTo(coordinate, 2),
      ),
    );

    await collapseSelectedCard(page);
    await seek(page, 9);
    await chooseTool(page, "polygon");
    for (const point of points.slice(0, 2)) await clickPoint(page, point);
    await row(page, tracks.bbox).click();
    await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toBeVisible();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    expect(scopeCase.writes).toHaveLength(1);
    await confirmSwitch(page, "丢弃并切换");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "0");
    await expectScope(page, "track");
    await expect(page.getByTestId("video-tool-btn-track")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("video-track-context-bar")).toContainText("f2-bbox");
    await expect(hint(page)).toBeHidden();
    await expect(picker(page)).toBeHidden();
    expect(scopeCase.writes).toHaveLength(1);
    await page.reload();
    const stored = await annotations(request, scopeCase);
    expect(stored).toHaveLength(5);
    expect(stored.find((annotation) => annotation.id === first.saved.id)?.geometry).toEqual(
      first.saved.geometry,
    );
  });

  test("F2-4 类别待定点击多边形轨迹行，继续保留原源帧而丢弃才跳 F0", async ({
    page,
    request,
    scopeCase,
  }) => {
    const tracks = await seedTracks(request, scopeCase);
    await open(page, scopeCase);
    await seek(page, 8);
    await chooseTool(page, "polygon");
    await draw(page, "polygon", "frame");

    await row(page, tracks.polygon).click();
    await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toBeVisible();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await expectScope(page, "frame");
    expect(scopeCase.writes).toEqual([]);
    expect(await annotations(request, scopeCase)).toHaveLength(4);
    await confirmSwitch(page, "继续绘制");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await expectScope(page, "frame");
    await expect(page.getByTestId("video-tool-btn-polygon")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(picker(page)).toBeVisible();
    const first = await save(page, scopeCase);
    expect(first.payload.geometry.type).toBe("video_polygon");
    expect(first.payload.geometry.frame_index).toBe(8);
    expect(first.saved.class_name).toBe("car");
    expect(first.payload.geometry.points).toHaveLength(3);
    const points: Point[] = [
      [0.32, 0.3],
      [0.58, 0.3],
      [0.53, 0.58],
    ];
    points.forEach((point, index) =>
      point.forEach((coordinate, axis) =>
        expect(first.payload.geometry.points![index][axis]).toBeCloseTo(coordinate, 2),
      ),
    );

    await collapseSelectedCard(page);
    await seek(page, 9);
    await chooseTool(page, "polygon");
    await draw(page, "polygon", "frame");
    await row(page, tracks.polygon).click();
    await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toBeVisible();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    expect(scopeCase.writes).toHaveLength(1);
    await confirmSwitch(page, "丢弃并切换");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "0");
    await expectScope(page, "track");
    await expect(page.getByTestId("video-tool-btn-polygon-track")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByTestId("video-track-context-bar")).toContainText("f2-polyg");
    await expect(picker(page)).toBeHidden();
    await expect(hint(page)).toBeHidden();
    expect(scopeCase.writes).toHaveLength(1);
    await page.reload();
    const stored = await annotations(request, scopeCase);
    expect(stored).toHaveLength(5);
    expect(stored.every((annotation) => annotation.class_name === "car")).toBe(true);
    expect(stored.find((annotation) => annotation.id === first.saved.id)?.geometry).toEqual(
      first.saved.geometry,
    );
  });

  test("F2-4 拖框中按 T 后松手，确认仍保护迁移到类别选择器的原单帧草稿", async ({
    page,
    request,
    scopeCase,
  }) => {
    await open(page, scopeCase);
    await seek(page, 8);
    await chooseTool(page, "box");
    const requestTrackWhileDragging = async () => {
      const bounds = await renderedMediaBounds(stage(page));
      await page.mouse.move(bounds.x + bounds.width * 0.32, bounds.y + bounds.height * 0.3);
      await page.mouse.down();
      await page.mouse.move(bounds.x + bounds.width * 0.58, bounds.y + bounds.height * 0.58, {
        steps: 10,
      });
      await page.keyboard.press("t");
      await page.mouse.up();
      await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toBeVisible();
      await expect(picker(page)).toBeVisible();
      await expectScope(page, "frame");
    };

    await requestTrackWhileDragging();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    expect(scopeCase.writes).toEqual([]);
    await confirmSwitch(page, "继续绘制");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await expectScope(page, "frame");
    await expect(page.getByTestId("video-tool-btn-box")).toHaveAttribute("aria-pressed", "true");
    await expect(picker(page)).toBeVisible();
    await expect(hint(page)).toHaveText("仅当前源帧");
    const first = await save(page, scopeCase);
    expect(first.payload.geometry.type).toBe("video_bbox");
    expect(first.payload.geometry.frame_index).toBe(8);
    expect(first.saved.class_name).toBe("car");
    for (const [coordinate, expected] of Object.entries({ x: 0.32, y: 0.3, w: 0.26, h: 0.28 })) {
      expect(first.payload.geometry[coordinate as "x" | "y" | "w" | "h"]).toBeCloseTo(expected, 2);
    }

    await seek(page, 9);
    await chooseTool(page, "box");
    await requestTrackWhileDragging();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    expect(scopeCase.writes).toHaveLength(1);
    await confirmSwitch(page, "丢弃并切换");
    await expectScope(page, "track");
    await expect(page.getByTestId("video-tool-btn-track")).toHaveAttribute("aria-pressed", "true");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    await expect(picker(page)).toBeHidden();
    await expect(hint(page)).toBeHidden();
    expect(scopeCase.writes).toHaveLength(1);
    await page.reload();
    const stored = await annotations(request, scopeCase);
    expect(stored).toHaveLength(1);
    expect(stored[0].geometry).toEqual(first.saved.geometry);
    expect(stored[0].class_name).toBe("car");
  });

  test("F2-4 矩形轨迹续画中 B 不切工具，松手仅保存原轨迹本帧", async ({
    page,
    request,
    scopeCase,
  }) => {
    const tracks = await seedTracks(request, scopeCase);
    const original = tracks.bbox;
    for (const kind of ["polygon", "polyline", "mask"] as const) {
      const removed = await request.delete(
        `${API_BASE}/api/v1/tasks/${scopeCase.taskId}/annotations/${tracks[kind].id}`,
        { headers: auth(scopeCase.token) },
      );
      expect(removed.status(), await removed.text()).toBe(204);
    }
    await open(page, scopeCase);
    await selectTrack(page, original);
    await seek(page, 8);
    await expectScope(page, "track");
    await expect(page.getByTestId("video-tool-btn-track")).toHaveAttribute("aria-pressed", "true");
    const bounds = await renderedMediaBounds(stage(page));
    let pointerDown = false;
    let saved: Annotation;
    try {
      await page.mouse.move(bounds.x + bounds.width * 0.32, bounds.y + bounds.height * 0.3);
      await page.mouse.down();
      pointerDown = true;
      await page.mouse.move(bounds.x + bounds.width * 0.58, bounds.y + bounds.height * 0.58, {
        steps: 10,
      });
      await page.keyboard.press("b");
      await expectScope(page, "track");
      await expect(page.getByTestId("video-tool-btn-track")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
      await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toHaveCount(
        0,
      );
      await expect(
        page.getByText("正在续画轨迹，请先松手完成本帧后再切换", { exact: true }),
      ).toBeVisible();
      expect(scopeCase.writes).toEqual([]);

      const responsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "PATCH" &&
          new URL(response.url()).pathname ===
            `/api/v1/tasks/${scopeCase.taskId}/annotations/${original.id}`,
      );
      await page.mouse.up();
      pointerDown = false;
      const response = await responsePromise;
      expect(response.ok(), await response.text()).toBe(true);
      saved = record(scopeCase, (await response.json()) as Annotation);
    } finally {
      if (pointerDown) await page.mouse.up();
    }

    expect(saved.id).toBe(original.id);
    expect(saved.geometry.type).toBe("video_track_bbox");
    expect(saved.geometry.keyframes!.map((keyframe) => keyframe.frame_index)).toEqual([0, 8, 10]);
    expect(saved.geometry.keyframes!.filter((keyframe) => keyframe.frame_index !== 8)).toEqual(
      original.geometry.keyframes,
    );
    const current = saved.geometry.keyframes!.find((keyframe) => keyframe.frame_index === 8)!;
    for (const [coordinate, expected] of Object.entries({ x: 0.32, y: 0.3, w: 0.26, h: 0.28 })) {
      expect(current.bbox![coordinate as "x" | "y" | "w" | "h"]).toBeCloseTo(expected, 2);
    }
    expect(scopeCase.writes).toEqual([
      `PATCH /api/v1/tasks/${scopeCase.taskId}/annotations/${original.id}`,
    ]);
    await key(page, "b");
    await expectScope(page, "frame");
    await expect(page.getByTestId("video-tool-btn-box")).toHaveAttribute("aria-pressed", "true");
    await page.reload();
    const stored = await annotations(request, scopeCase);
    expect(stored).toHaveLength(1);
    expect(stored.find((annotation) => annotation.id === original.id)?.geometry).toEqual(
      saved.geometry,
    );
    expect(scopeCase.writes).toHaveLength(1);
  });

  test("F2-4 追踪种子采集先保护点集草稿，退出后恢复原工具与范围", async ({
    page,
    request,
    scopeCase,
  }, testInfo) => {
    await json(
      await request.put(
        `${API_BASE}/api/v1/projects/${scopeCase.data.project_id}/ml-backends/${scopeCase.data.ml_backend_id}/enablement`,
        { headers: auth(scopeCase.token), data: { enabled: true } },
      ),
    );
    const backendPath = `/api/v1/projects/${scopeCase.data.project_id}/ml-backends/${scopeCase.data.ml_backend_id}`;
    const setupPath = `${backendPath}/setup`;
    const catalogPath = `${backendPath}/capabilities`;
    const trackerSetup = {
      name: "F2 tracker seed capability fixture",
      version: "1",
      infra: "pytorch",
      is_interactive: false,
      supported_prompts: ["point", "bbox", "correction_frame"],
      supported_inputs: ["video", "point_prompt", "bbox_prompt", "mask_prompt"],
      supported_geometric_outputs: ["mask"],
      supported_trackers: ["sam2_video"],
      models: [
        {
          id: "grounded-sam2-tracker",
          display_name: "Grounded-SAM2 Tracker",
          task: "tracker",
          model_family: "grounded-sam2",
          composition: "composite",
          is_interactive: false,
          supported_prompts: ["point", "bbox", "correction_frame"],
          supported_inputs: ["video", "point_prompt", "bbox_prompt", "mask_prompt"],
          supported_geometric_outputs: ["mask"],
          supported_trackers: ["sam2_video"],
          max_window_frames: 16,
          resource_profile: { device: "gpu", batchable: false },
        },
      ],
    };
    const trackerCatalog = {
      ...trackerSetup,
      protocol_version: "1",
      compat_protocol_versions: [],
      warmup_endpoint: false,
      text_driven_trackers: [],
      supported_text_outputs: [],
      modalities: ["video"],
      warnings: [],
      models: trackerSetup.models.map((model) => ({
        ...model,
        infra: "pytorch",
        modality: "video",
        default_input_type: "video",
      })),
    };
    const capabilityRequests = { setup: 0, catalog: 0 };
    await page.route(
      (url) => url.pathname === setupPath || url.pathname === catalogPath,
      (route) => {
        const catalog = new URL(route.request().url()).pathname === catalogPath;
        capabilityRequests[catalog ? "catalog" : "setup"] += 1;
        return route.fulfill({
          status: 200,
          json: catalog ? trackerCatalog : trackerSetup,
        });
      },
    );
    testInfo.annotations.push({
      type: "fixture",
      description:
        "Only ML /setup and /capabilities responses are supplied; no model or tracker execution.",
    });
    const modelRequests: string[] = [];
    page.on("request", (outgoing) => {
      const path = new URL(outgoing.url()).pathname;
      if (
        outgoing.method() === "POST" &&
        /interactive-annotating|predict|video:track|:propagate|correction-jobs|tracker-jobs/.test(
          path,
        )
      ) {
        modelRequests.push(path);
      }
    });
    await open(page, scopeCase);
    await seek(page, 8);
    await chooseTool(page, "polygon");
    const points: Point[] = [
      [0.32, 0.3],
      [0.58, 0.3],
      [0.53, 0.58],
    ];
    for (const point of points.slice(0, 2)) await clickPoint(page, point);
    await page.getByTestId("workbench-ai-tracker").click();
    const panel = page.getByTestId("video-tracker-propagate-dialog");
    await expect(panel).toBeVisible();
    const model = panel.locator("#tracker-model");
    await expect(model.locator('option[value="sam2_video"]')).toHaveCount(1);
    await model.selectOption("sam2_video");
    await panel.getByTestId("tracker-target-class").selectOption("car");
    const toggle = panel.getByTestId("tracker-seed-toggle");
    await toggle.click();
    await expect(page.getByRole("alertdialog").filter({ hasText: "切换视频工具" })).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expectScope(page, "frame");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "8");
    await confirmSwitch(page, "继续绘制");
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(page.getByTestId("video-tool-btn-polygon")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expectScope(page, "frame");
    expect(scopeCase.writes).toEqual([]);
    await panel.getByRole("button", { name: "关闭发现新目标", exact: true }).click();
    await expect(panel).toBeHidden();
    await clickPoint(page, points[2]);
    await key(page, "Enter");
    const first = await save(page, scopeCase);
    expect(first.payload.geometry.type).toBe("video_polygon");
    expect(first.payload.geometry.frame_index).toBe(8);
    expect(first.payload.geometry.points).toHaveLength(3);
    points.forEach((point, index) =>
      point.forEach((coordinate, axis) =>
        expect(first.payload.geometry.points![index][axis]).toBeCloseTo(coordinate, 2),
      ),
    );

    await collapseSelectedCard(page);
    await seek(page, 9);
    await chooseTool(page, "polygon");
    for (const point of points.slice(0, 2)) await clickPoint(page, point);
    await page.getByTestId("workbench-ai-tracker").click();
    await expect(panel).toBeVisible();
    await model.selectOption("sam2_video");
    await toggle.click();
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    await confirmSwitch(page, "丢弃并切换");
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    await clickPoint(page, [0.45, 0.45]);
    const target = panel.getByTestId("tracker-seed-target-1");
    await expect(target).toBeVisible();
    await expect(target).toContainText("F9");
    await expect(panel.getByTestId("tracker-seed-count")).toContainText("1 点");
    await expect(page.getByText("请先结束追踪种子采集", { exact: true })).toHaveCount(0);
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(page.getByTestId("video-tool-btn-polygon")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expectScope(page, "frame");
    await expect(stage(page)).toHaveAttribute("data-video-frame-index", "9");
    await expect(hint(page)).toBeHidden();
    expect(scopeCase.writes).toHaveLength(1);
    expect(capabilityRequests.setup).toBeGreaterThan(0);
    expect(capabilityRequests.catalog).toBeGreaterThan(0);
    expect(modelRequests).toEqual([]);
    await panel.getByRole("button", { name: "关闭发现新目标", exact: true }).click();
    await page.reload();
    const stored = await annotations(request, scopeCase);
    expect(stored).toHaveLength(1);
    expect(stored[0].geometry).toEqual(first.saved.geometry);
    expect(modelRequests).toEqual([]);
  });

  test("F2-4 Mask 范围切换沿用继续、丢弃与保存保护", async ({ page, request, scopeCase }) => {
    const tracks = await seedTracks(request, scopeCase);
    await open(page, scopeCase);
    const maskTool = page.getByTestId("video-tool-btn-mask-track");
    const expectMaskAction = async (label: string) => {
      await openMaskSettings(page);
      await expect(page.getByTestId("mask-primary-action")).toHaveText(label);
      await closeMaskSettings(page);
    };
    await selectTrack(page, tracks.mask);
    await expect(maskTool).toHaveAttribute("aria-pressed", "true");
    const original = tracks.mask.geometry;
    await expectMaskAction("已保存");
    await clickPoint(page, [0.9, 0.85]);
    await expectMaskAction("保存当前帧关键帧");
    const leaveDialog = page.getByRole("alertdialog");
    const decideLeave = async (action: "继续编辑" | "丢弃并离开" | "保存后离开") => {
      await expect(leaveDialog).toBeVisible();
      await leaveDialog.getByRole("button", { name: action, exact: true }).click();
      await expect(leaveDialog).toBeHidden();
    };
    await scopeControl(page, "frame").click();
    await decideLeave("继续编辑");
    await expectScope(page, "track");
    await expectMaskAction("保存当前帧关键帧");
    expect(scopeCase.writes).toEqual([]);
    await scopeControl(page, "frame").click();
    await decideLeave("丢弃并离开");
    await expectScope(page, "frame");
    expect(
      (await annotations(request, scopeCase)).find((annotation) => annotation.id === tracks.mask.id)
        ?.geometry,
    ).toEqual(original);
    expect(scopeCase.writes).toEqual([]);
    await chooseTool(page, "select");
    await clickPoint(page, [0.94, 0.9]);
    await selectTrack(page, tracks.mask);
    await expect(maskTool).toHaveAttribute("aria-pressed", "true");
    await expectMaskAction("已保存");
    await clickPoint(page, [0.9, 0.85]);
    const saved = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        new URL(response.url()).pathname ===
          `/api/v1/tasks/${scopeCase.taskId}/video/tracks/${tracks.mask.id}/mask-keyframes/0`,
    );
    await scopeControl(page, "frame").click();
    await decideLeave("保存后离开");
    expect((await saved).ok()).toBe(true);
    await expectScope(page, "frame");
    const persisted = record(
      scopeCase,
      (await annotations(request, scopeCase)).find(
        (annotation) => annotation.id === tracks.mask.id,
      )!,
    );
    expect(persisted.geometry.keyframes![0].mask).not.toEqual(original.keyframes![0].mask);
    expect(persisted.geometry.keyframes!.slice(1)).toEqual(original.keyframes!.slice(1));
    expect(scopeCase.writes).toHaveLength(1);
    await page.reload();
    expect(
      (await annotations(request, scopeCase)).find((annotation) => annotation.id === tracks.mask.id)
        ?.geometry,
    ).toEqual(persisted.geometry);
  });

  test("F2-4 禁用目标轨迹变体后保留选择工具和原因，不改为其他几何", async ({
    page,
    request,
    scopeCase,
  }) => {
    const tracks = await seedTracks(request, scopeCase);
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${scopeCase.data.project_id}`, {
        headers: auth(scopeCase.token),
        data: {
          tool_bindings: {
            ...scopeCase.bindings,
            region: { ...scopeCase.bindings.region, video_modes: { box: true, track: false } },
          },
        },
      }),
    );
    await open(page, scopeCase);
    await chooseTool(page, "select");
    await selectTrack(page, tracks.polygon);
    await expect(page.getByTestId("video-tool-btn-select")).toHaveAttribute("aria-pressed", "true");
    await expect(
      page
        .locator("[data-sonner-toast]")
        .filter({ hasText: /未启用|不可用|不支持/ })
        .first(),
    ).toBeVisible();
    expect((await accessibleTools(page)).includes("polygon-track")).toBe(false);
    expect(scopeCase.writes).toEqual([]);
    expect(await annotations(request, scopeCase)).toHaveLength(4);
  });

  test("F2-5 短面板反复切范围保留工具可达性、当前项与菜单键盘行为", async ({ page, scopeCase }) => {
    await page.setViewportSize({ width: 1440, height: 768 });
    await open(page, scopeCase, "审核协作布局");
    await shortenCanvas(page);
    const identity = await stage(page).elementHandle();
    const expected = new Map<Scope, string[]>();
    for (let turn = 0; turn < 3; turn += 1) {
      for (const scope of ["frame", "track"] as const) {
        await scopeControl(page, scope).click();
        await expectScope(page, scope);
        const id = scope === "frame" ? "mask" : "mask-track";
        await chooseTool(page, id);
        const ids = await accessibleTools(page);
        if (!expected.has(scope)) expected.set(scope, ids);
        expect(ids).toEqual(expected.get(scope));
        expect(ids).toContain(id);
        expect(ids).toContain("select");
        const dock = page.getByTestId("tool-dock");
        await expect
          .poll(() => dock.evaluate((node) => node.scrollHeight <= node.clientHeight + 1))
          .toBe(true);
        await expect(page.getByTestId(`video-tool-btn-${id}`)).toBeInViewport();
        await expect(page.getByTestId(`video-tool-btn-${id}`)).toHaveAttribute(
          "aria-pressed",
          "true",
        );
        const more = page.getByTestId("tool-dock-more");
        if (scope === "frame") await expect(more).toBeVisible();
        if (await more.isVisible()) {
          await more.press("ArrowDown");
          await expect(page.getByTestId("tool-dock-menu")).toBeVisible();
          await page.keyboard.press("End");
          await page.keyboard.press("Home");
          await page.keyboard.press("b");
          await page.keyboard.press("Escape");
          await expect(page.getByTestId("tool-dock-menu")).toBeHidden();
          await expect(more).toBeFocused();
          await expectScope(page, scope);
          await expect(page.getByTestId(`video-tool-btn-${id}`)).toHaveAttribute(
            "aria-pressed",
            "true",
          );
        }
        expect(await stage(page).evaluate((node, original) => node === original, identity)).toBe(
          true,
        );
      }
    }
    expect(expected.get("frame")).toEqual([
      "box",
      "keypoint",
      "mask",
      "polygon",
      "polyline",
      "rotated-box",
      "select",
    ]);
    expect(expected.get("track")).toEqual([
      "mask-track",
      "polygon-track",
      "polyline-track",
      "select",
      "track",
    ]);
    expect(scopeCase.writes).toEqual([]);
    await identity?.dispose();
  });
});
