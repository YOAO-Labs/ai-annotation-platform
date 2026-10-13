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
    // Konva's double-click interval also applies to clicks at different positions.
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

// Reject invalid input without entering the retryable transport/server-failure queue.
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
  await queue.getByText(target.display_id, { exact: true }).click();
  const selected = page.getByTestId(`box-list-item-${existing.id}`);
  await selected.click();
  await expect(selected).toHaveClass(/!border-brand/);
  const response = page.waitForResponse(
    (item) =>
      item.request().method() === "POST" &&
      item.url().includes(`/tasks/${data.task_ids[0]}/annotations`),
  );
  release();
  expect((await response).ok()).toBe(true);
  await expect(selected).toHaveClass(/!border-brand/);
  expect(await saved(request, data, data.task_ids[1])).toHaveLength(1);
  expect(await saved(request, data)).toHaveLength(2);
  await page.unroute(path);
  await page.reload();
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
    // Keep writes offline while allowing the page bundle and read APIs to reload.
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
  test.setTimeout(90_000);
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
  // Let the project's normal 30s query freshness expire, then reconnect to revalidate.
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
  // The test-only status mutation does not emit the normal UI cache invalidation.
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

  // Reach review through the real workflow so the reviewer's mount-time claim
  // sees frozen contributor evidence instead of a silent 409.
  const submitToken = await seed.accessToken(data.annotator_email);
  const submitted = await request.post(`${API_BASE}/api/v1/tasks/${data.task_ids[0]}/submit`, {
    headers: { Authorization: `Bearer ${submitToken}` },
  });
  expect(submitted.ok(), await submitted.text()).toBe(true);
  await seed.injectToken(page, data.reviewer_email);
  await page.goto(`/projects/${data.project_id}/review?task=${data.task_ids[0]}`);
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
  await expect(
    page
      .getByTestId("continuous-creation-controls")
      .getByRole("switch", { name: "连续创建", exact: true }),
  ).toBeDisabled();
  expect(await saved(request, data)).toHaveLength(0);
});
