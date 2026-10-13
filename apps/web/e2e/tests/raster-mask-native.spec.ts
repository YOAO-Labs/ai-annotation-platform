import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { expect, test, type SeedAPI, type SeedData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const MATRIX = process.env.PLAYWRIGHT_RASTER_MASK_MATRIX;

interface MaskReference {
  encoding: "coco_rle_ref";
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
}

interface AnnotationDto {
  id: string;
  annotation_type: string;
  geometry: {
    type: string;
    mask?: MaskReference;
    points?: number[][];
    holes?: number[][][];
    polygons?: Array<{ type: "polygon"; points: number[][]; holes?: number[][][] }>;
  };
  version: number;
}

interface CocoRle {
  encoding: "coco_rle";
  size: [number, number];
  counts: number[];
}

interface ConversionResponse {
  updated_annotations: AnnotationDto[];
  created_annotations: AnnotationDto[];
  deleted_annotation_ids: string[];
  report: {
    source_count: number;
    result_count: number;
    lossy_count: number;
  };
}

interface RasterResourceSnapshot {
  generation: number;
  committedBytes: number;
  reservedBytes: number;
  chargedBytes: number;
  hardBudgetBytes: number;
  invariantOk: boolean;
  categories: Array<{ category: string; committedBytes: number; pinnedBytes: number }>;
}

async function rasterResources(page: Page): Promise<RasterResourceSnapshot | null> {
  return page.evaluate(
    () =>
      (
        window as unknown as {
          __rasterResourceDiagnostics?: { resources: RasterResourceSnapshot };
        }
      ).__rasterResourceDiagnostics?.resources ?? null,
  );
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

async function json<T>(response: APIResponse): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `${response.request().method()} ${response.url()} failed: ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as T;
}

async function listAnnotations(
  request: APIRequestContext,
  taskId: string,
  token: string,
): Promise<AnnotationDto[]> {
  return json<AnnotationDto[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: authHeaders(token),
    }),
  );
}

async function getMaskContent(
  request: APIRequestContext,
  annotationId: string,
  token: string,
): Promise<CocoRle> {
  return json<CocoRle>(
    await request.get(`${API_BASE}/api/v1/annotations/${annotationId}/mask-content`, {
      headers: authHeaders(token),
    }),
  );
}

async function openTask(
  page: Page,
  seed: SeedAPI,
  data: SeedData,
  taskId: string,
  projectOptIn = true,
): Promise<void> {
  await seed.configureRasterMask(data.project_id, projectOptIn);
  await seed.advanceTask({
    taskId,
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 15_000 });
}

async function paintStroke(
  page: Page,
  from: [number, number] = [0.42, 0.42],
  to: [number, number] = [0.58, 0.56],
): Promise<void> {
  await closeMaskSettings(page);
  const box = await page.getByTestId("workbench-stage").boundingBox();
  if (!box) throw new Error("workbench stage has no bounding box");
  await page.mouse.move(box.x + box.width * from[0], box.y + box.height * from[1]);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * to[0], box.y + box.height * to[1], { steps: 10 });
  await page.mouse.up();
}

async function beginRasterEdit(page: Page, annotationId: string): Promise<void> {
  const row = page.getByTestId(`box-list-item-${annotationId}`);
  await expect(row).toBeVisible({ timeout: 15_000 });
  await row.click();
  // 选中后优先从浮动详情卡进入；右侧面板展开时会合法遮住列表行操作。
  await page.locator('button[aria-label="\u7f16\u8f91 Mask"]:visible').last().click();
  await openMaskSettings(page);
  await expect(page.getByTestId("mask-toolbar")).toContainText("\u5c31\u7eea", { timeout: 15_000 });
}

test.describe("raster mask native write matrix", () => {
  test.skip(MATRIX !== "native", "requires RASTER_MASK_CREATE_ENABLED=true");

  test("1. blank create, refresh and re-edit preserve the committed pixels", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await openTask(page, seed, data, taskId);
    const token = await seed.accessToken(data.annotator_email);

    await page.keyboard.press("m");
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-toolbar")).toContainText("\u5c31\u7eea", {
      timeout: 10_000,
    });
    await paintStroke(page);
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-toolbar")).toContainText("\u672a\u4fdd\u5b58");

    const createResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotations`) &&
        response.request().method() === "POST" &&
        response.status() === 201,
    );
    await closeMaskSettings(page);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeVisible();
    await page.keyboard.press("Enter");
    const created = await json<AnnotationDto>(await createResponse);
    expect(created.annotation_type).toBe("raster_mask");
    expect(created.geometry.type).toBe("raster_mask");
    const firstContent = await getMaskContent(request, created.id, token);

    await page.reload();
    await expect(page.getByTestId(`box-list-item-${created.id}`)).toContainText(
      "\u6805\u683c\u63a9\u7801",
      { timeout: 15_000 },
    );
    await beginRasterEdit(page, created.id);
    await paintStroke(page, [0.62, 0.56], [0.7, 0.62]);
    const updateResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotations/${created.id}`) &&
        response.request().method() === "PATCH" &&
        response.ok(),
    );
    await closeMaskSettings(page);
    await page.keyboard.press("Enter");
    const updated = await json<AnnotationDto>(await updateResponse);
    expect(updated.id).toBe(created.id);
    expect(updated.version).toBeGreaterThan(created.version);
    const editedContent = await getMaskContent(request, created.id, token);
    expect(editedContent.counts).not.toEqual(firstContent.counts);

    await page.reload();
    await expect(page.getByTestId(`box-list-item-${created.id}`)).toBeVisible({ timeout: 15_000 });
    expect(await getMaskContent(request, created.id, token)).toEqual(editedContent);
  });

  test("2. one hole and three components survive persistence and refresh", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "donut_three",
    });
    await openTask(page, seed, data, taskId);
    const token = await seed.accessToken(data.annotator_email);

    const row = page.getByTestId(`box-list-item-${fixture.annotation_id}`);
    await expect(row).toContainText("3 \u7ec4\u4ef6", { timeout: 15_000 });
    await expect(row).toContainText("1 \u5b54\u6d1e");
    const [annotation] = (await listAnnotations(request, taskId, token)).filter(
      (item) => item.id === fixture.annotation_id,
    );
    const saved = await request.patch(
      `${API_BASE}/api/v1/tasks/${taskId}/annotations/${fixture.annotation_id}`,
      {
        headers: { ...authHeaders(token), "If-Match": `W/"${annotation.version}"` },
        data: { geometry: annotation.geometry },
      },
    );
    expect(saved.ok()).toBeTruthy();

    await page.reload();
    await expect(row).toContainText("3 \u7ec4\u4ef6", { timeout: 15_000 });
    await expect(row).toContainText("1 \u5b54\u6d1e");
  });

  test("3. failed save keeps the buffer and stroke history; retry commits once", async ({
    page,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    await openTask(page, seed, data, taskId);
    await beginRasterEdit(page, fixture.annotation_id);

    let patchAttempts = 0;
    let successfulMutations = 0;
    await page.route(
      `**/api/v1/tasks/${taskId}/annotations/${fixture.annotation_id}`,
      async (route) => {
        if (route.request().method() !== "PATCH") return route.continue();
        patchAttempts += 1;
        if (patchAttempts === 1) {
          await route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: { reason: "temporary_failure", message: "retry" } }),
          });
          return;
        }
        successfulMutations += 1;
        await route.continue();
      },
    );

    await paintStroke(page, [0.55, 0.48], [0.65, 0.5]);
    await page.keyboard.press("Enter");
    await openMaskSettings(page);
    const toolbar = page.getByTestId("mask-toolbar");
    await expect(toolbar).toContainText("\u64cd\u4f5c\u5931\u8d25", { timeout: 10_000 });
    await toolbar.getByTestId("mask-primary-action").click();
    await expect(toolbar).toContainText("\u672a\u4fdd\u5b58");
    await expect(toolbar.getByTitle("\u64a4\u9500\u7b14\u753b (Ctrl+Z)")).toBeEnabled();

    const success = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotations/${fixture.annotation_id}`) &&
        response.request().method() === "PATCH" &&
        response.ok(),
    );
    await closeMaskSettings(page);
    await page.keyboard.press("Enter");
    await success;
    expect(patchAttempts).toBe(2);
    expect(successfulMutations).toBe(1);
  });

  test("4. late content from the previous task cannot flash back after rapid switching", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const firstTaskId = data.task_ids[0];
    const secondTaskId = data.task_ids[1];
    const first = await seed.injectRasterMask({
      taskId: firstTaskId,
      userEmail: data.annotator_email,
    });
    const second = await seed.injectRasterMask({
      taskId: secondTaskId,
      userEmail: data.annotator_email,
      variant: "donut_three",
    });
    await seed.advanceTask({
      taskId: secondTaskId,
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    const token = await seed.accessToken(data.annotator_email);
    const secondTask = await json<{ display_id: string }>(
      await request.get(`${API_BASE}/api/v1/tasks/${secondTaskId}`, {
        headers: authHeaders(token),
      }),
    );

    await page.route(`**/api/v1/annotations/${first.annotation_id}/mask-content`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 900));
      await route.continue();
    });
    await openTask(page, seed, data, firstTaskId);
    await page.getByText(secondTask.display_id, { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`task=${secondTaskId}`));
    await expect(page.getByTestId(`box-list-item-${second.annotation_id}`)).toContainText(
      "3 \u7ec4\u4ef6",
      { timeout: 15_000 },
    );
    await page.waitForTimeout(1_000);
    await expect(page.getByTestId(`box-list-item-${first.annotation_id}`)).toHaveCount(0);
  });

  test("5. locked raster masks reject pointer, hotkeys, Enter and Delete", async ({
    page,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      locked: true,
    });
    await openTask(page, seed, data, taskId);
    await page.getByTestId(`box-list-item-${fixture.annotation_id}`).click();
    await page.keyboard.press("m");
    await openMaskSettings(page);
    const toolbar = page.getByTestId("mask-toolbar");
    await expect(toolbar).toBeVisible({ timeout: 10_000 });
    await expect(toolbar.getByRole("radio", { name: "\u7b14\u5237" })).toBeDisabled();
    await expect(toolbar.getByRole("radio", { name: "\u6a61\u76ae" })).toBeDisabled();

    let mutationCount = 0;
    page.on("request", (request) => {
      if (
        request.url().includes("/annotations") &&
        ["POST", "PATCH", "PUT", "DELETE"].includes(request.method())
      )
        mutationCount += 1;
    });
    await closeMaskSettings(page);
    await page.keyboard.press("b");
    await page.keyboard.press("e");
    await paintStroke(page);
    await page.keyboard.press("Enter");
    await page.keyboard.press("Delete");
    await page.waitForTimeout(500);
    expect(mutationCount).toBe(0);
  });

  test("6. polygon with a hole replaces to Mask while preserving identity", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await openTask(page, seed, data, taskId);
    const token = await seed.accessToken(data.annotator_email);
    const sourceGeometry = {
      type: "polygon",
      points: [
        [0.1, 0.1],
        [0.85, 0.1],
        [0.85, 0.85],
        [0.1, 0.85],
      ],
      holes: [
        [
          [0.35, 0.3],
          [0.62, 0.3],
          [0.62, 0.6],
          [0.35, 0.6],
        ],
      ],
    };
    const created = await json<AnnotationDto>(
      await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
        headers: authHeaders(token),
        data: {
          annotation_type: "polygon",
          tool_unit_id: "region",
          class_name: "car",
          geometry: sourceGeometry,
        },
      }),
    );
    await page.reload();
    await page.getByTestId(`box-list-item-${created.id}`).click();
    const convertedResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await page.getByRole("button", { name: "\u8f6c\u4e3a Mask" }).click();
    const dialog = page.getByRole("dialog", { name: "\u6807\u6ce8\u8f6c\u6362\u4e2d\u5fc3" });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("\u7ed3\u679c\u7b56\u7565").click();
    await page.getByRole("option", { name: "\u66ff\u6362\u6765\u6e90" }).click();
    await dialog.getByRole("button", { name: "\u751f\u6210\u9884\u89c8" }).click();
    const report = dialog.getByLabel("\u8f6c\u6362\u9884\u89c8\u62a5\u544a");
    await expect(report).toContainText("\u5b54\u6d1e 1 \u2192 1");
    await dialog.getByRole("button", { name: "\u6267\u884c\u8f6c\u6362" }).click();
    await page
      .getByRole("alertdialog", { name: "\u786e\u8ba4\u6267\u884c\u8f6c\u6362\uff1f" })
      .getByRole("button", { name: "\u786e\u8ba4\u6267\u884c" })
      .click();
    const conversion = await json<ConversionResponse>(await convertedResponse);
    const converted = conversion.updated_annotations[0];
    expect(converted.id).toBe(created.id);
    expect(converted.annotation_type).toBe("raster_mask");
    expect(converted.geometry.type).toBe("raster_mask");
    expect(conversion.created_annotations).toHaveLength(0);
    expect(conversion.deleted_annotation_ids).toHaveLength(0);
    expect(conversion.report).toMatchObject({ source_count: 1, result_count: 1, lossy_count: 0 });
  });

  test("7. Mask to multi-polygon reports loss, cancel is inert, confirm syncs both types", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "donut_three",
    });
    await openTask(page, seed, data, taskId);
    const token = await seed.accessToken(data.annotator_email);
    const row = page.getByTestId(`box-list-item-${fixture.annotation_id}`);
    await expect(row).toContainText("3 \u7ec4\u4ef6", { timeout: 15_000 });
    await row.click();

    await page.getByRole("button", { name: "\u8f6c\u4e3a\u77e2\u91cf\u51e0\u4f55" }).click();
    let dialog = page.getByRole("dialog", { name: "\u6807\u6ce8\u8f6c\u6362\u4e2d\u5fc3" });
    await dialog.getByLabel("\u7ed3\u679c\u7b56\u7565").click();
    await page.getByRole("option", { name: "\u66ff\u6362\u6765\u6e90" }).click();
    await dialog.getByRole("button", { name: "\u751f\u6210\u9884\u89c8" }).click();
    let report = dialog.getByLabel("\u8f6c\u6362\u9884\u89c8\u62a5\u544a");
    await expect(report).toContainText("\u7ec4\u4ef6 3 \u2192 3");
    await expect(report).toContainText("\u5b54\u6d1e 1 \u2192 1");
    await dialog.getByRole("button", { name: "\u53d6\u6d88" }).click();
    await expect(dialog).not.toBeVisible();
    await expect
      .poll(
        async () =>
          (await listAnnotations(request, taskId, token)).find(
            (item) => item.id === fixture.annotation_id,
          )?.geometry.type,
      )
      .toBe("raster_mask");

    const convertedResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotation-conversions:execute`) &&
        response.request().method() === "POST" &&
        response.ok(),
    );
    await page.getByRole("button", { name: "\u8f6c\u4e3a\u77e2\u91cf\u51e0\u4f55" }).click();
    dialog = page.getByRole("dialog", { name: "\u6807\u6ce8\u8f6c\u6362\u4e2d\u5fc3" });
    await dialog.getByLabel("\u7ed3\u679c\u7b56\u7565").click();
    await page.getByRole("option", { name: "\u66ff\u6362\u6765\u6e90" }).click();
    await dialog.getByRole("button", { name: "\u751f\u6210\u9884\u89c8" }).click();
    report = dialog.getByLabel("\u8f6c\u6362\u9884\u89c8\u62a5\u544a");
    await expect(report).toContainText("\u6709\u635f\u9879");
    await dialog.getByRole("button", { name: "\u6267\u884c\u8f6c\u6362" }).click();
    await page
      .getByRole("alertdialog", { name: "\u786e\u8ba4\u6267\u884c\u8f6c\u6362\uff1f" })
      .getByRole("button", { name: "\u786e\u8ba4\u6267\u884c" })
      .click();
    const conversion = await json<ConversionResponse>(await convertedResponse);
    const converted = conversion.updated_annotations[0];
    expect(converted.id).toBe(fixture.annotation_id);
    expect(converted.annotation_type).toBe("multi_polygon");
    expect(converted.geometry.type).toBe("multi_polygon");
    expect(converted.geometry.polygons).toHaveLength(3);
    expect(
      converted.geometry.polygons?.some((polygon) => (polygon.holes?.length ?? 0) === 1),
    ).toBeTruthy();
  });

  test("8. corrupt content is diagnosed on its own row while a healthy sibling remains usable", async ({
    page,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const healthy = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    const corrupt = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "corrupt",
    });
    await openTask(page, seed, data, taskId);

    await expect(page.getByTestId(`box-list-item-${healthy.annotation_id}`)).toContainText("px", {
      timeout: 15_000,
    });
    const corruptRow = page.getByTestId(`box-list-item-${corrupt.annotation_id}`);
    await expect(corruptRow).toContainText("missing_object", { timeout: 15_000 });
    await corruptRow.click();
    await expect(
      page.getByRole("button", { name: "\u590d\u5236 Mask \u8bca\u65ad" }),
    ).toBeVisible();
    await expect(page.getByLabel("Mask \u52a0\u8f7d\u72b6\u6001")).toContainText(
      "mask object is invalid",
    );
  });

  test("10. deployment create-on does not regress the legacy polygon Mask flow", async ({
    page,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await openTask(page, seed, data, taskId, false);

    await page.keyboard.press("m");
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible({ timeout: 10_000 });
    await paintStroke(page);
    const createdResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotations`) &&
        response.request().method() === "POST" &&
        response.status() === 201,
    );
    await closeMaskSettings(page);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeVisible();
    await page.keyboard.press("Enter");
    const created = await json<AnnotationDto>(await createdResponse);
    expect(created.annotation_type).toBe("polygon");
    expect(created.geometry.type).toBe("polygon");
  });

  test("12. retry reloads only the failed Mask and never reloads a healthy sibling", async ({
    page,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const healthy = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    const corrupt = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      variant: "corrupt",
    });
    const reads = new Map<string, number>();
    page.on("request", (request) => {
      for (const id of [healthy.annotation_id, corrupt.annotation_id]) {
        if (request.url().endsWith(`/api/v1/annotations/${id}/mask-content`)) {
          reads.set(id, (reads.get(id) ?? 0) + 1);
        }
      }
    });
    await openTask(page, seed, data, taskId);
    const healthyRow = page.getByTestId(`box-list-item-${healthy.annotation_id}`);
    const corruptRow = page.getByTestId(`box-list-item-${corrupt.annotation_id}`);
    await expect(healthyRow).toContainText("px", { timeout: 15_000 });
    await expect(corruptRow).toContainText("missing_object", { timeout: 15_000 });
    expect(reads.get(healthy.annotation_id)).toBe(1);
    expect(reads.get(corrupt.annotation_id)).toBe(1);

    await corruptRow
      .getByRole("button", { name: new RegExp(`Mask ${corrupt.annotation_id}`) })
      .click();
    await expect.poll(() => reads.get(corrupt.annotation_id)).toBe(2);
    expect(reads.get(healthy.annotation_id)).toBe(1);
  });

  test("13. 8K sparse Mask edits, saves and reloads through the tiled workbench", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
      canvas: "8k",
    });
    await openTask(page, seed, data, taskId);
    const token = await seed.accessToken(data.annotator_email);
    const before = await getMaskContent(request, fixture.annotation_id, token);
    expect(before.size).toEqual([8192, 8192]);

    await beginRasterEdit(page, fixture.annotation_id);
    const toolbar = page.getByTestId("mask-toolbar");
    await expect(toolbar).toContainText("大画布分块模式");
    const advancedTools = toolbar.getByTitle("Mask 高级工具");
    await advancedTools.click();
    await expect(page.getByRole("menuitem", { name: "填充全部孔洞" })).toBeDisabled();
    await expect(page.getByText("形态学（当前视口 ROI）", { exact: true })).toBeVisible();
    await page.mouse.click(1, 1);
    await expect(page.getByRole("menuitem", { name: "填充全部孔洞" })).toHaveCount(0);
    await openMaskSettings(page);
    await expect(toolbar).toBeVisible();

    await paintStroke(page, [0.35, 0.35], [0.46, 0.42]);
    await openMaskSettings(page);
    await expect(toolbar).toContainText("未保存");
    await expect.poll(async () => (await rasterResources(page))?.reservedBytes).toBe(0);
    const beforePageHide = await rasterResources(page);
    expect(beforePageHide).toMatchObject({ invariantOk: true, reservedBytes: 0 });
    expect(beforePageHide!.chargedBytes).toBeLessThanOrEqual(beforePageHide!.hardBudgetBytes);
    expect(beforePageHide!.categories).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ category: "mask-edit" }),
        expect.objectContaining({ category: "mask-history" }),
      ]),
    );

    await page.evaluate(() =>
      window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })),
    );
    const bfcache = await rasterResources(page);
    expect(bfcache).toMatchObject({
      generation: beforePageHide!.generation + 1,
      reservedBytes: 0,
      invariantOk: true,
    });
    expect(bfcache!.categories).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ category: "mask-edit" }),
        expect.objectContaining({ category: "mask-history" }),
      ]),
    );
    await page.evaluate(() =>
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })),
    );
    await expect(toolbar).toContainText("未保存");
    await expect(toolbar.getByTitle("撤销笔画 (Ctrl+Z)")).toBeEnabled();
    await expect.poll(async () => (await rasterResources(page))?.invariantOk).toBe(true);

    const updateResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotations/${fixture.annotation_id}`) &&
        response.request().method() === "PATCH" &&
        response.ok(),
    );
    await closeMaskSettings(page);
    await page.keyboard.press("Enter");
    await updateResponse;
    const saved = await getMaskContent(request, fixture.annotation_id, token);
    expect(saved.size).toEqual([8192, 8192]);
    expect(saved.counts).not.toEqual(before.counts);

    await page.reload();
    await beginRasterEdit(page, fixture.annotation_id);
    await expect(page.getByTestId("mask-toolbar")).toContainText("大画布分块模式");
    expect(await getMaskContent(request, fixture.annotation_id, token)).toEqual(saved);
  });
});

test.describe("raster mask read-only and closed-gate matrix", () => {
  test.skip(MATRIX !== "readonly", "requires RASTER_MASK_CREATE_ENABLED=false");

  test("9. read-on/create-off renders existing content but exposes no native create or edit", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const fixture = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    await openTask(page, seed, data, taskId);
    const token = await seed.accessToken(data.annotator_email);
    const capabilities = await json<{
      read_enabled: boolean;
      write_enabled: boolean;
      legacy_polygon_commit_enabled: boolean;
    }>(
      await request.get(`${API_BASE}/api/v1/tasks/${taskId}/mask-capabilities`, {
        headers: authHeaders(token),
      }),
    );
    expect(capabilities).toMatchObject({
      read_enabled: true,
      write_enabled: false,
      legacy_polygon_commit_enabled: true,
    });

    const row = page.getByTestId(`box-list-item-${fixture.annotation_id}`);
    await expect(row).toContainText("px", { timeout: 15_000 });
    await row.click();
    await expect(page.getByRole("button", { name: "\u7f16\u8f91 Mask" })).toHaveCount(0);
    const upload = await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
      headers: authHeaders(token),
      data: { encoding: "coco_rle", size: [48, 64], counts: [3072] },
    });
    expect(upload.status()).toBe(409);
    expect((await upload.json()).detail.reason).toBe("raster_mask_create_disabled");
  });

  test("11. closed gate rejects upload, direct create, prediction accept and import without new rows", async ({
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const token = await seed.accessToken(data.admin_email);
    const existing = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
    const prediction = await seed.injectRasterPrediction({
      taskId,
      userEmail: data.annotator_email,
    });
    const before = await listAnnotations(request, taskId, token);
    const rle = await getMaskContent(request, existing.annotation_id, token);

    const upload = await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
      headers: authHeaders(token),
      data: rle,
    });
    expect(upload.status()).toBe(409);

    const direct = await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: authHeaders(token),
      data: {
        annotation_type: "raster_mask",
        tool_unit_id: "region",
        class_name: "car",
        geometry: { type: "raster_mask", mask: existing.mask },
      },
    });
    expect(direct.status()).toBe(409);
    expect((await direct.json()).detail.reason).toBe("raster_mask_create_disabled");

    const accepted = await request.post(
      `${API_BASE}/api/v1/tasks/${taskId}/predictions/${prediction.prediction_id}/accept`,
      { headers: authHeaders(token), data: {} },
    );
    expect(accepted.status()).toBe(409);
    expect((await accepted.json()).detail.reason).toBe("raster_mask_create_disabled");

    const task = await json<{ display_id: string }>(
      await request.get(`${API_BASE}/api/v1/tasks/${taskId}`, { headers: authHeaders(token) }),
    );
    const envelope = {
      schema_version: "1.3",
      exported_from: { platform: "aap" },
      project: { name: "E2E", type_key: "image-det" },
      mask_objects: { [existing.mask.sha256]: rle },
      tasks: [
        {
          task_match: { display_id: task.display_id },
          annotations: [
            {
              geometry: { type: "raster_mask", mask: existing.mask },
              class_name: "car",
              tool_unit_id: "region",
              source: "manual",
            },
          ],
        },
      ],
    };
    const imported = await request.post(
      `${API_BASE}/api/v1/projects/${data.project_id}/annotations/import?format=aap_json`,
      {
        headers: authHeaders(token),
        multipart: {
          file: {
            name: "closed-gate-raster-mask.json",
            mimeType: "application/json",
            buffer: Buffer.from(JSON.stringify(envelope)),
          },
        },
      },
    );
    expect(imported.status()).toBe(200);
    expect(await imported.json()).toMatchObject({ imported: 0, skipped: 1 });
    expect(await listAnnotations(request, taskId, token)).toHaveLength(before.length);
  });
});
