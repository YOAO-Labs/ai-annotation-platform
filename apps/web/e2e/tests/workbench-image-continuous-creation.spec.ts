import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, type SeedAPI } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const pageErrors = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const errors: string[] = [];
  pageErrors.set(page, errors);
  page.on("pageerror", (error) => errors.push(error.message));
});
test.afterEach(({ page }) => {
  expect(pageErrors.get(page)).toEqual([]);
});

async function prepare(request: APIRequestContext, seed: SeedAPI, required = false) {
  const data = await seed.owned();
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const response = await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, { headers });
  expect(response.ok()).toBe(true);
  const project = await response.json();
  const toolBindings = { ...project.tool_bindings };
  for (const unit of ["bbox", "region", "rotated_bbox", "polyline", "keypoint"]) {
    toolBindings[unit] = {
      ...toolBindings.bbox,
      enabled: true,
      classes: [{ name: "car" }, { name: "person" }],
      attribute_schema: {
        fields: [
          { key: "unit", label: "工具单元", type: "text", default: unit },
          { key: "verified", label: "已核验", type: "boolean", default: false, required: true },
          { key: "count", label: "数量", type: "number", default: 0, required: true },
          ...(required && unit === "bbox"
            ? [{ key: "serial", label: "实例编号", type: "text", required: true, mutable: false }]
            : []),
        ],
      },
    };
  }
  toolBindings.keypoint.keypoint_schema = {
    nodes: [{ name: "front" }, { name: "center" }, { name: "back" }],
    edges: [
      [0, 1],
      [1, 2],
    ],
  };
  const updated = await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
    headers,
    data: { tool_bindings: toolBindings },
  });
  expect(updated.ok(), await updated.text()).toBe(true);
  for (const taskId of data.task_ids.slice(0, 2))
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  return { ...data, headers };
}

async function open(page: Page, seed: SeedAPI, data: Awaited<ReturnType<typeof prepare>>) {
  await page.setViewportSize({ width: 1440, height: 1080 });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await page.getByRole("button", { name: "布局", exact: true }).click();
  await page.getByRole("button", { name: "标准标注布局", exact: true }).click();
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
}

async function choose(page: Page, unit = "bbox", cls = "car") {
  const controls = page.getByTestId("continuous-creation-controls");
  const toggle = controls.getByRole("switch", { name: "连续创建", exact: true });
  if ((await toggle.getAttribute("aria-checked")) !== "true") await toggle.click();
  await controls.getByRole("combobox", { name: "创建工具单元" }).selectOption(unit);
  await controls.getByRole("button", { name: cls, exact: true }).click();
  await expect(page.getByTestId("continuous-creation-status")).toContainText(cls);
}

async function drawBox(page: Page, x: number, y: number, w = 0.065, h = 0.065) {
  const bounds = (await page.getByTestId("workbench-stage").boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * x, bounds.y + bounds.height * y);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * (x + w), bounds.y + bounds.height * (y + h), {
    steps: 6,
  });
  await page.mouse.up();
}

async function drawPoints(page: Page, points: number[][]) {
  const bounds = (await page.getByTestId("workbench-stage").boundingBox())!;
  for (const [x, y] of points) {
    await page.mouse.click(bounds.x + bounds.width * x, bounds.y + bounds.height * y);
    await page.waitForTimeout(450);
  }
}

async function saved(
  request: APIRequestContext,
  data: Awaited<ReturnType<typeof prepare>>,
  taskId = data.task_ids[0],
) {
  const response = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
    headers: data.headers,
  });
  expect(response.ok()).toBe(true);
  return (await response.json()) as Array<{
    id: string;
    annotation_type: string;
    tool_unit_id: string;
    class_name: string;
    geometry: Record<string, unknown>;
    attributes: Record<string, unknown>;
  }>;
}

async function accepted(page: Page, count: number) {
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-pending-drawing", "false");
  await expect(stage).toHaveAttribute("data-user-box-count", String(count));
}

test("一次选类连续创建20个矩形框，零额外选类并刷新读回", async ({ page, request, seed }) => {
  test.setTimeout(90_000);
  const data = await prepare(request, seed);
  await open(page, seed, data);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let classPickers = 0;
  await page.exposeFunction("recordCreationPicker", () => {
    classPickers++;
  });
  await page.evaluate(() => {
    const observer = new MutationObserver((records) => {
      for (const record of records)
        for (const node of record.addedNodes) {
          if (
            node instanceof Element &&
            (node.matches('[data-testid="class-picker-popover"]') ||
              node.querySelector('[data-testid="class-picker-popover"]'))
          ) {
            void (
              window as unknown as { recordCreationPicker(): Promise<void> }
            ).recordCreationPicker();
          }
        }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });
  await choose(page);
  for (let i = 0; i < 20; i++) {
    await drawBox(page, 0.22 + (i % 5) * 0.105, 0.27 + Math.floor(i / 5) * 0.12);
    await accepted(page, i + 1);
  }
  expect(classPickers).toBe(0);
  await expect(page.getByTestId("tool-btn-box")).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  const annotations = await saved(request, data);
  expect(annotations).toHaveLength(20);
  for (const annotation of annotations)
    expect(annotation).toMatchObject({
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      attributes: { unit: "bbox", verified: false, count: 0 },
    });
  expect(errors).toEqual([]);
});

test("四种其它手工工具连续创建且同名单元保持独立，Mask恢复为Polygon", async ({
  page,
  request,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await prepare(request, seed);
  await open(page, seed, data);
  let count = 0;
  for (const unit of ["region", "rotated_bbox", "polyline", "keypoint"]) {
    await choose(page, unit);
    for (let i = 0; i < 2; i++) {
      const x = 0.26 + i * 0.24;
      if (unit === "rotated_bbox") await drawBox(page, x, 0.4, 0.13, 0.1);
      else if (unit === "keypoint") {
        await drawPoints(page, [[x, 0.36]]);
        await page.keyboard.down("Alt");
        await drawPoints(page, [[x + 0.07, 0.43]]);
        await page.keyboard.up("Alt");
        const bounds = (await page.getByTestId("workbench-stage").boundingBox())!;
        await page.mouse.click(
          bounds.x + bounds.width * (x + 0.12),
          bounds.y + bounds.height * 0.37,
          { button: "right" },
        );
      } else {
        await drawPoints(page, [
          [x, 0.35],
          [x + 0.13, 0.35],
          [x + 0.08, 0.5],
        ]);
        if (unit === "region" && i === 1) await drawPoints(page, [[x, 0.35]]);
        else await page.keyboard.press("Enter");
      }
      await accepted(page, ++count);
      await expect(page.getByTestId("class-picker-popover")).toBeHidden();
    }
  }
  const beforeSwitch = await saved(request, data);
  await page.getByTestId("tool-btn-mask").click();
  await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  await choose(page, "region", "person");
  await expect(page.getByTestId("tool-btn-polygon")).toHaveAttribute("aria-pressed", "true");
  expect(await saved(request, data)).toEqual(beforeSwitch);
  await drawPoints(page, [
    [0.29, 0.58],
    [0.44, 0.59],
    [0.37, 0.7],
  ]);
  await page.keyboard.press("Enter");
  await accepted(page, ++count);
  await page.reload();
  const annotations = await saved(request, data);
  expect(annotations).toHaveLength(9);
  for (const unit of ["region", "rotated_bbox", "polyline", "keypoint"]) {
    const objects = annotations.filter(
      (item) => item.tool_unit_id === unit && item.class_name === "car",
    );
    expect(objects).toHaveLength(2);
    for (const object of objects) expect(object.attributes.unit).toBe(unit);
    if (unit === "rotated_bbox")
      for (const object of objects) expect(object.geometry.angle).toBe(0);
    if (unit === "keypoint")
      for (const object of objects)
        expect((object.geometry.points as Array<{ v: number }>).map((point) => point.v)).toEqual([
          2, 1, 0,
        ]);
  }
});

test("每个对象独立补必填属性，立即Enter使用最新值且Esc先取消草稿", async ({
  page,
  request,
  seed,
}) => {
  const data = await prepare(request, seed, true);
  await open(page, seed, data);
  await choose(page);
  for (let i = 0; i < 2; i++) {
    await drawBox(page, 0.3 + i * 0.23, 0.35, 0.12, 0.13);
    const popover = page.getByTestId("manual-creation-popover");
    await expect(popover).toBeVisible();
    await expect(page.getByTestId("class-picker-popover")).toBeHidden();
    const serial = popover.getByRole("textbox", { name: /实例编号/ });
    await expect(serial).toHaveValue("");
    await serial.fill(`object-${i + 1}`);
    await page.keyboard.press("Enter");
    await accepted(page, i + 1);
  }
  await drawBox(page, 0.37, 0.6, 0.13, 0.13);
  await expect(page.getByTestId("manual-creation-popover")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("manual-creation-popover")).toBeHidden();
  await expect(page.getByTestId("continuous-creation-status")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  await expect(page.getByTestId("tool-btn-select")).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  expect((await saved(request, data)).map((item) => item.attributes.serial).sort()).toEqual([
    "object-1",
    "object-2",
  ]);
});

test("业务失败可重试，保存中切题保持意图且迟到结果只归原题", async ({ page, request, seed }) => {
  test.setTimeout(90_000);
  const data = await prepare(request, seed);
  const existing = await seed.createTaskAnnotation(data.task_ids[1], data.annotator_email, {
    annotation_type: "bbox",
    tool_unit_id: "bbox",
    class_name: "person",
    geometry: { type: "bbox", x: 0.2, y: 0.3, w: 0.15, h: 0.15 },
  });
  await open(page, seed, data);
  await choose(page);
  const path = `**/api/v1/tasks/${data.task_ids[0]}/annotations`;
  let rejectOnce = true;
  await page.route(path, async (route) => {
    if (route.request().method() === "POST" && rejectOnce) {
      rejectOnce = false;
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: '{"detail":"C validation fault fixture"}',
      });
    } else await route.continue();
  });
  await drawBox(page, 0.3, 0.35, 0.14, 0.1);
  const draft = page.getByTestId("manual-creation-popover");
  await expect(draft).toContainText("保存失败");
  expect(await saved(request, data)).toHaveLength(0);
  await draft.getByRole("button", { name: "重试", exact: true }).click();
  await accepted(page, 1);
  await page.unroute(path);

  const targetResponse = await request.get(`${API_BASE}/api/v1/tasks/${data.task_ids[1]}`, {
    headers: data.headers,
  });
  const target = await targetResponse.json();
  const sourceResponse = await request.get(`${API_BASE}/api/v1/tasks/${data.task_ids[0]}`, {
    headers: data.headers,
  });
  const source = await sourceResponse.json();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let pendingWrites = 0;
  await page.route(path, async (route) => {
    if (route.request().method() === "POST") {
      pendingWrites++;
      await gate;
    }
    await route.continue();
  });
  await drawBox(page, 0.54, 0.48, 0.14, 0.1);
  await expect.poll(() => pendingWrites).toBe(1);
  const queue = page.getByRole("tabpanel", { name: "任务队列", exact: true });
  await queue.getByText(target.display_id, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${data.task_ids[1]}`));
  await expect(page.getByTestId("continuous-creation-status")).toContainText("car");
  await expect(draft).toBeHidden();
  await queue.getByText(source.display_id, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${data.task_ids[0]}`));
  await queue.getByText(target.display_id, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${data.task_ids[1]}`));
  const selected = page.getByTestId(`box-list-item-${existing.id}`);
  await selected.click();
  const selectionCard = page.locator("[data-floating-panel]").filter({
    has: page.getByText("选中对象", { exact: true }),
  });
  await selectionCard.getByText("更多信息", { exact: true }).click();
  const selectedId = selectionCard.getByTitle(existing.id, { exact: true });
  await expect(selectedId).toBeVisible();
  const response = page.waitForResponse(
    (item) =>
      item.request().method() === "POST" &&
      item.url().includes(`/tasks/${data.task_ids[0]}/annotations`),
  );
  release();
  const lateWrite = await response;
  expect(lateWrite.ok()).toBe(true);
  await lateWrite.finished();
  expect(await saved(request, data, data.task_ids[1])).toMatchObject([
    { id: existing.id, class_name: "person" },
  ]);
  expect(await saved(request, data)).toHaveLength(2);
  await expect(selectedId).toBeVisible();
  await page.unroute(path);
  await page.reload();
  await expect(selected).toBeVisible();
  expect(await saved(request, data)).toHaveLength(2);
});

async function storedQueue(page: Page) {
  return page.evaluate(
    async () =>
      await new Promise<Array<{ kind: string; taskId: string; payload: { class_name: string } }>>(
        (resolve, reject) => {
          const opening = indexedDB.open("keyval-store");
          opening.onerror = () => reject(opening.error);
          opening.onsuccess = () => {
            const database = opening.result;
            if (!database.objectStoreNames.contains("keyval")) {
              database.close();
              resolve([]);
              return;
            }
            const transaction = database.transaction("keyval", "readonly");
            const read = transaction.objectStore("keyval").get("anno.offline-queue.v1");
            read.onsuccess = () => resolve(read.result ?? []);
            read.onerror = () => reject(read.error);
            transaction.oncomplete = () => database.close();
          };
        },
      ),
  );
}

async function installQueueAbort(page: Page) {
  await page.addInitScript(() => {
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (
      this: IDBObjectStore,
      value: unknown,
      key?: IDBValidKey,
    ) {
      const result = put.call(this, value, key);
      if (key === "anno.offline-queue.v1" && sessionStorage.getItem("abort-annotation-queue"))
        this.transaction.abort();
      return result;
    };
  });
}

for (const outcome of ["retry", "reconnect"] as const) {
  test(`未同步创建撤销的队列事务中止后保留原操作：${outcome}`, async ({
    page,
    context,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    const data = await prepare(request, seed);
    await installQueueAbort(page);
    await open(page, seed, data);
    await choose(page);
    await context.setOffline(true);
    await drawBox(page, 0.3, 0.35, 0.14, 0.13);
    await accepted(page, 1);
    await expect.poll(async () => (await storedQueue(page)).length).toBe(1);
    await page.evaluate(() => sessionStorage.setItem("abort-annotation-queue", "true"));
    await page.keyboard.press("Control+z");
    await expect(page.getByText("撤销未完成，可重试剩余操作", { exact: true })).toBeVisible();
    await accepted(page, 1);
    expect(await storedQueue(page)).toHaveLength(1);
    const path = `**/api/v1/tasks/${data.task_ids[0]}/annotations`;
    let allowReplay = false;
    let acceptedPosts = 0;
    if (outcome === "retry") {
      await page.evaluate(() => sessionStorage.removeItem("abort-annotation-queue"));
      await page.keyboard.press("Control+z");
      await accepted(page, 0);
      await expect.poll(async () => (await storedQueue(page)).length).toBe(0);
    } else {
      await page.route(path, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        if (!allowReplay) return route.fulfill({ status: 503, json: { detail: "offline" } });
        const response = await route.fetch();
        if (response.ok()) acceptedPosts++;
        await route.fulfill({ response });
      });
    }
    await context.setOffline(false);
    await page.reload();
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
    if (outcome === "reconnect") {
      expect(await storedQueue(page)).toHaveLength(1);
      await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[1]}`);
      await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
      await page.evaluate(() => sessionStorage.removeItem("abort-annotation-queue"));
      allowReplay = true;
      await context.setOffline(true);
      await context.setOffline(false);
      await expect.poll(async () => (await saved(request, data)).length).toBe(1);
      await expect.poll(async () => (await storedQueue(page)).length).toBe(0);
      expect(acceptedPosts).toBe(1);
      await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
      await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
      await page.keyboard.press("Control+z");
      await expect.poll(async () => (await saved(request, data)).length).toBe(0);
    } else {
      expect(await saved(request, data)).toHaveLength(0);
    }
  });
}

test("视频补关键帧只在 IndexedDB 提交后确认，冲突不入队且重连仅同步一次", async ({
  page,
  context,
  request,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await prepare(request, seed);
  const { task_id: taskId } = await seed.videoTask(data.project_id);
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  const annotator = await seed.accessToken(data.annotator_email);
  const created = await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
    headers: { Authorization: `Bearer ${annotator}` },
    data: {
      annotation_type: "video_track_bbox",
      class_name: "car",
      attributes: { verified: true },
      geometry: {
        type: "video_track_bbox",
        track_id: "durable-geometry",
        keyframes: [0, 10].map((frame_index) => ({
          frame_index,
          bbox: { x: 0.12 + frame_index / 100, y: 0.12, w: 0.12, h: 0.16 },
          source: "manual",
        })),
      },
    },
  });
  expect(created.ok(), await created.text()).toBe(true);
  const track: { id: string; geometry: { keyframes: unknown[] } } = await created.json();
  const readKeyframes = async () =>
    (await saved(request, data, taskId)).find(({ id }) => id === track.id)?.geometry.keyframes;
  const addedKeyframe = {
    frame_index: 1,
    bbox: { x: 0.13, y: 0.12, w: 0.12, h: 0.16 },
    source: "manual",
  };
  const [firstKeyframe, lastKeyframe] = track.geometry.keyframes;
  const materialized = [firstKeyframe, addedKeyframe, lastKeyframe];
  const routePath = `**/api/v1/tasks/${taskId}/annotations/${track.id}`;
  let status: 409 | 503 | null = 409;
  let acceptedPatches = 0;
  await page.route(routePath, async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    if (status) return route.fulfill({ status, json: { detail: "injected edit failure" } });
    const response = await route.fetch();
    if (response.ok()) acceptedPatches++;
    await route.fulfill({ response });
  });
  await installQueueAbort(page);
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 25_000 });
  const row = page.getByTestId("video-track-row").filter({ hasText: "durable-" });
  await row.click();
  await page.keyboard.press("k");
  await page.keyboard.press("ArrowRight");
  const frameState = page.getByTestId("video-track-context-state");
  await expect(frameState).toHaveAttribute("data-state", "interpolated");
  await expect(page.getByTestId("video-track-context-frame")).toHaveText("源帧 F1");
  const add = page.getByRole("button", { name: "补关键帧", exact: true });
  await add.click();
  await expect(page.getByText("视频修改未保存", { exact: true })).toBeVisible();
  await expect(frameState).toHaveAttribute("data-state", "interpolated");
  expect(await storedQueue(page)).toHaveLength(0);

  status = 503;
  await page.evaluate(() => sessionStorage.setItem("abort-annotation-queue", "true"));
  await add.click();
  await expect(page.getByText("本机保存失败，修改尚未保存", { exact: true })).toBeVisible();
  await expect(frameState).toHaveAttribute("data-state", "interpolated");
  expect(await storedQueue(page)).toHaveLength(0);
  expect(await readKeyframes()).toEqual(track.geometry.keyframes);

  await page.evaluate(() => sessionStorage.removeItem("abort-annotation-queue"));
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(frameState).toHaveAttribute("data-state", "keyframe");
  await expect.poll(async () => (await storedQueue(page)).length).toBe(1);
  await page.reload();
  await expect(page.getByTestId("video-konva-stage")).toBeVisible();
  expect(await storedQueue(page)).toHaveLength(1);
  await context.setOffline(true);
  status = null;
  await context.setOffline(false);
  await expect.poll(async () => (await storedQueue(page)).length).toBe(0);
  expect(acceptedPatches).toBe(1);
  expect(await readKeyframes()).toMatchObject(materialized);

  await row.click();
  await page.keyboard.press("Control+z");
  await expect.poll(readKeyframes).toEqual(track.geometry.keyframes);

  await page.keyboard.press("Control+Shift+z");
  await expect.poll(readKeyframes).toMatchObject(materialized);
  expect(acceptedPatches).toBe(3);
  expect(await storedQueue(page)).toHaveLength(0);
});

test("图片批量撤销在首项和中间冲突时停止，刷新重试不会重复已恢复的对象", async ({
  page,
  request,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await prepare(request, seed);
  const annotationsPath = `/api/v1/tasks/${data.task_ids[0]}/annotations`;
  const creationKeys = new Map<number, string>();
  await open(page, seed, data);
  await choose(page);
  for (const [index, x] of [0.25, 0.45, 0.65].entries()) {
    const creation = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === annotationsPath &&
        response.request().method() === "POST",
    );
    await drawBox(page, x, 0.4);
    await accepted(page, index + 1);
    const response = await creation;
    expect(response.status()).toBe(201);
    const key = await response.request().headerValue("idempotency-key");
    expect(key).toBeTruthy();
    creationKeys.set((await response.json()).geometry.x, key!);
  }
  await page.getByRole("switch", { name: "连续创建", exact: true }).click();
  await expect(page.getByTestId("tool-btn-select")).toHaveAttribute("aria-pressed", "true");
  await expect.poll(async () => (await storedQueue(page)).length).toBe(0);
  const original = (await saved(request, data)).sort(
    (a, b) => Number(a.geometry.x) - Number(b.geometry.x),
  );
  expect(original).toHaveLength(3);
  const stage = page.getByTestId("workbench-stage");
  for (const [index, { geometry }] of original.entries()) {
    const position = await stage.evaluate(
      (element, { x, y, w, h }) => ({
        x:
          Number(element.getAttribute("data-media-x")) +
          (Number(x) + Number(w) / 2) * Number(element.getAttribute("data-media-width")),
        y:
          Number(element.getAttribute("data-media-y")) +
          (Number(y) + Number(h) / 2) * Number(element.getAttribute("data-media-height")),
      }),
      geometry,
    );
    await stage.click({ position, modifiers: index ? ["Shift"] : [] });
    await expect(stage).toHaveAttribute("data-selected-box-count", String(index + 1));
  }
  await expect(page.getByText("3 个已选中 · 批量", { exact: true })).toBeVisible();
  await page
    .getByRole("group", { name: "批量操作", exact: true })
    .getByRole("button", { name: "批量删除", exact: true })
    .click();
  const confirm = page.getByTestId("delete-confirm-submit");
  const deleted = page.getByText("已删除 3/3 个标注", { exact: true });
  await expect(confirm.or(deleted)).toBeVisible();
  if (await confirm.isVisible()) await confirm.click();
  await expect(deleted).toBeVisible();
  await expect.poll(async () => (await saved(request, data)).length).toBe(0);

  const attempts: { x: number; key: string | undefined; status: number; reason?: string }[] = [];
  const restoredIds: string[] = [];
  await page.route(`**${annotationsPath}`, async (route) => {
    const restore = route.request();
    if (restore.method() !== "POST") return route.continue();
    const { x } = restore.postDataJSON().geometry;
    const headers = await restore.allHeaders();
    const key = headers["idempotency-key"];
    const shouldConflict = attempts.length === 0 || attempts.length === 2;
    const response = shouldConflict
      ? await route.fetch({
          headers: {
            ...headers,
            "idempotency-key": [...creationKeys].find(([originalX]) => originalX !== x)![1],
          },
        })
      : await route.fetch();
    const body = await response.json();
    attempts.push({ x, key, status: response.status(), reason: body.detail?.reason });
    if (response.ok()) restoredIds.push(body.id);
    await route.fulfill({ response });
  });
  const undo = page.getByTitle("撤销 (Ctrl+Z)", { exact: true });
  const redo = page.getByTitle("重做 (Ctrl+Shift+Z)", { exact: true });
  await undo.click();
  await expect(
    page.getByText("撤销未完成（本次已完成 0/3 项），可重试剩余操作", { exact: true }),
  ).toBeVisible();
  expect(attempts.map(({ status, reason }) => ({ status, reason }))).toEqual([
    { status: 409, reason: "idempotency_conflict" },
  ]);
  expect(await saved(request, data)).toHaveLength(0);
  await undo.click();
  await expect(
    page.getByText("撤销未完成（本次已完成 1/3 项），可重试剩余操作", { exact: true }),
  ).toBeVisible();
  expect(attempts.map(({ status, reason }) => ({ status, reason }))).toEqual([
    { status: 409, reason: "idempotency_conflict" },
    { status: 201 },
    { status: 409, reason: "idempotency_conflict" },
  ]);
  expect(restoredIds).toHaveLength(1);
  expect((await saved(request, data)).map(({ id }) => id)).toEqual(restoredIds);
  await page.reload();
  await expect(stage).toHaveAttribute("data-image-ready", "true");
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await undo.click();
  await expect.poll(async () => (await saved(request, data)).length).toBe(3);
  await expect(redo).toBeEnabled();
  expect(attempts.map(({ status }) => status)).toEqual([409, 201, 409, 201, 201]);
  expect(attempts[0].key).toBeTruthy();
  expect(attempts[2].key).toBeTruthy();
  expect(attempts[1]).toMatchObject({ x: attempts[0].x, key: attempts[0].key });
  expect(attempts[3]).toMatchObject({ x: attempts[2].x, key: attempts[2].key });
  const restored = (await saved(request, data)).sort(
    (a, b) => Number(a.geometry.x) - Number(b.geometry.x),
  );
  expect(restored.map(({ geometry }) => geometry)).toEqual(
    original.map(({ geometry }) => geometry),
  );
  expect(restored.map(({ id }) => id).sort()).toEqual([...restoredIds].sort());
  expect(await storedQueue(page)).toHaveLength(0);

  const redoneIds: string[] = [];
  await page.route(`**${annotationsPath}/*`, (route) => {
    if (route.request().method() === "DELETE")
      redoneIds.push(new URL(route.request().url()).pathname.split("/").at(-1)!);
    return route.continue();
  });
  await redo.click();
  await expect.poll(async () => (await saved(request, data)).length).toBe(0);
  await expect(redo).toBeDisabled();
  expect(redoneIds).toEqual([...restoredIds].reverse());
});

for (const failure of ["offline", "server"] as const) {
  test(`${failure === "offline" ? "断网" : "服务暂时不可用时"}创建等待IndexedDB接收，刷新队列保留且恢复网络仅同步一次`, async ({
    page,
    context,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    const data = await prepare(request, seed);
    await open(page, seed, data);
    await choose(page);
    const path = `**/api/v1/tasks/${data.task_ids[0]}/annotations`;
    if (failure === "offline") await context.setOffline(true);
    else
      await page.route(path, (route) =>
        route.request().method() === "POST"
          ? route.fulfill({ status: 503, json: { detail: "Temporary service failure" } })
          : route.continue(),
      );
    await drawBox(page, 0.3, 0.35, 0.14, 0.13);
    await accepted(page, 1);
    await expect.poll(async () => (await storedQueue(page)).length).toBe(1);
    if (failure === "offline")
      await page.route(path, (route) =>
        route.request().method() === "POST"
          ? route.abort("internetdisconnected")
          : route.continue(),
      );
    await context.setOffline(false);
    await page.reload();
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
    const queued = await storedQueue(page);
    expect(queued).toHaveLength(1);
    expect(queued[0]).toMatchObject({
      kind: "create",
      taskId: data.task_ids[0],
      payload: { class_name: "car" },
    });
    expect(await saved(request, data)).toHaveLength(0);
    await page.unroute(path);
    await context.setOffline(true);
    await context.setOffline(false);
    await expect.poll(async () => (await saved(request, data)).length).toBe(1);
    await expect.poll(async () => (await storedQueue(page)).length).toBe(0);
    await page.reload();
    expect(await saved(request, data)).toHaveLength(1);
  });
}

test("半成品Esc不保存，工具绑定失效和任务锁会退出连续创建", async ({
  page,
  context,
  request,
  seed,
}) => {
  test.setTimeout(150_000);
  const data = await prepare(request, seed);
  await open(page, seed, data);
  for (const unit of ["region", "keypoint"]) {
    await choose(page, unit);
    await drawPoints(page, [[0.35, 0.4]]);
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("continuous-creation-status")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  }
  await choose(page);
  const bounds = (await page.getByTestId("workbench-stage").boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * 0.3, bounds.y + bounds.height * 0.4);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.5, bounds.y + bounds.height * 0.5, {
    steps: 5,
  });
  await page.keyboard.press("Escape");
  await page.mouse.up();
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-drag-kind", "none");
  await expect(page.getByTestId("continuous-creation-status")).toBeVisible();
  expect(await saved(request, data)).toHaveLength(0);

  const response = await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, {
    headers: data.headers,
  });
  const project = await response.json();
  const updated = await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
    headers: data.headers,
    data: {
      tool_bindings: {
        ...project.tool_bindings,
        bbox: { ...project.tool_bindings.bbox, enabled: false },
      },
    },
  });
  expect(updated.ok()).toBe(true);
  await page.waitForTimeout(31_000);
  await context.setOffline(true);
  await context.setOffline(false);
  await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  await expect(page.getByTestId("tool-btn-select")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("当前工具已停用", { exact: true })).toBeVisible();

  await choose(page, "region");
  await seed.advanceTask({
    taskId: data.task_ids[0],
    toStatus: "review",
    reviewerEmail: data.reviewer_email,
  });
  await page.waitForTimeout(31_000);
  await context.setOffline(true);
  await context.setOffline(false);
  await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  await expect(
    page
      .getByTestId("continuous-creation-controls")
      .getByRole("switch", { name: "连续创建", exact: true }),
  ).toBeDisabled();
  expect(await saved(request, data)).toHaveLength(0);
});

test("切到视频退出连续模式，审核入口无法开启", async ({ page, request, seed }) => {
  const data = await prepare(request, seed);
  const reviewTaskId = data.task_ids[1];
  const submitToken = await seed.accessToken(data.annotator_email);
  const submitted = await request.post(`${API_BASE}/api/v1/tasks/${reviewTaskId}/submit`, {
    headers: { Authorization: `Bearer ${submitToken}` },
  });
  expect(submitted.ok(), await submitted.text()).toBe(true);
  await seed.injectToken(page, data.reviewer_email);
  await page.goto(`/projects/${data.project_id}/review?task=${reviewTaskId}`);
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
  await expect(page.getByTestId("review-approve")).toBeEnabled();
  await expect(
    page
      .getByTestId("continuous-creation-controls")
      .getByRole("switch", { name: "连续创建", exact: true }),
  ).toBeDisabled();
  expect(await saved(request, data, reviewTaskId)).toHaveLength(0);

  const { task_id: videoTaskId } = await seed.videoTask(data.project_id);
  await seed.advanceTask({
    taskId: videoTaskId,
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  const videoResponse = await request.get(`${API_BASE}/api/v1/tasks/${videoTaskId}`, {
    headers: data.headers,
  });
  const video = await videoResponse.json();
  await open(page, seed, data);
  await choose(page);
  await page
    .getByRole("tabpanel", { name: "任务队列", exact: true })
    .getByText(video.display_id, { exact: true })
    .click();
  await expect(page.getByTestId("video-konva-stage")).toBeVisible();
  await expect(page.getByTestId("continuous-creation-status")).toBeHidden();
  await expect(page.getByTestId("continuous-creation-controls")).toBeHidden();
  expect(await saved(request, data)).toHaveLength(0);
});
