import type { Page, Response } from "@playwright/test";
import { expect, test as base, type FilteringSeedManifest } from "../fixtures/seed";
import { resetFiltering } from "../fixtures/filtering";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:18110";
const test = base.extend<{ filtering: FilteringSeedManifest }>({
  filtering: async ({ seed, page }, provideFixture) => {
    const data = await resetFiltering(seed);
    await seed.injectToken(page, data.user_emails.admin);
    await provideFixture(data);
  },
});

test.setTimeout(120_000);

function queryResponse(page: Page, projectId: string, grain: "tasks" | "objects" | "tracks") {
  const suffix =
    grain === "tasks"
      ? "/projects/" + projectId + "/tasks/query"
      : "/projects/" + projectId + "/data-manager/" + grain + "/query";
  return page.waitForResponse(
    (response) => response.url().endsWith(suffix) && response.request().method() === "POST",
  );
}

async function checked(response: Response) {
  expect(response.ok(), response.status() + " " + (await response.text())).toBe(true);
  return response.json();
}

// The Data section scrolls the whole page (the frame root scroll owner), not a
// table-local container; scroll it far enough to trigger cursor loading.
async function scrollDataPageToBottom(page: Page) {
  await page.locator("[data-dm-scroll-owner='data']").evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
}

function url(projectId: string, state: Record<string, string> = {}) {
  return "/projects/" + projectId + "/data-manager?" + new URLSearchParams(state).toString();
}

function envelope(value: unknown) {
  return JSON.stringify({ v: 1, value });
}

for (const scope of ["tasks", "objects", "tracks"] as const) {
  test(`wide ${scope} tables contain horizontal scrolling without losing page headers`, async ({
    page,
    filtering,
  }) => {
    const projectId =
      scope === "tasks"
        ? filtering.image.project_id
        : scope === "objects"
          ? filtering.paging.project_id
          : filtering.lidar.project_id;
    const response = queryResponse(page, projectId, scope);
    await page.goto(url(projectId, { lens: scope }));
    await checked(await response);
    const table = page.getByRole("table");
    await expect(table).toBeVisible();
    const viewport = scope === "tasks" ? table.locator("..") : table;
    const pageScroller = page.locator("[data-dm-scroll-owner='data']");
    const header = scope === "tasks" ? table.locator("thead") : table.locator("[role=row]").first();
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await expect
        .poll(() =>
          viewport.evaluate((element) => {
            const parent = element.parentElement!;
            return element.getBoundingClientRect().right - parent.getBoundingClientRect().right;
          }),
        )
        .toBeLessThanOrEqual(1);
      await expect
        .poll(() => pageScroller.evaluate((element) => element.scrollWidth - element.clientWidth))
        .toBeLessThanOrEqual(1);
      await viewport.evaluate((element) => {
        element.scrollLeft = element.scrollWidth;
      });
      expect(await viewport.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
      expect(await pageScroller.evaluate((element) => element.scrollLeft)).toBe(0);
      const headerCell = await header.getByRole("columnheader").last().boundingBox();
      expect(headerCell!.x + headerCell!.width).toBeLessThanOrEqual(width);
    }
    if (scope !== "tasks") {
      await pageScroller.evaluate((element) => {
        element.scrollTop = 1000;
      });
      await expect
        .poll(async () => {
          const h = await header.boundingBox();
          const p = await pageScroller.boundingBox();
          return Math.abs(h!.y - p!.y);
        })
        .toBeLessThanOrEqual(1);
      expect(await viewport.evaluate((element) => element.scrollTop)).toBe(0);
      await scrollDataPageToBottom(page);
      await expect(page.getByText(/已加载 10[12] \/ 10[12]/)).toBeVisible();
    }
  });
}

test("AI review restores OR and required attributes restore nested groups", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const videoResponse = queryResponse(page, filtering.video.project_id, "tasks");
  await page.goto(url(filtering.video.project_id, { view: "builtin:ai-review" }));
  const video = await checked(await videoResponse);
  expect(video.items.map((item: { id: string }) => item.id).sort()).toEqual(
    [...filtering.video.expected.ai_review_or_task_ids].sort(),
  );
  expect(video.total).toBe(3);
  await expect(page.getByRole("table")).toBeVisible();

  const token = await seed.accessToken(filtering.user_emails.admin);
  const viewsResponse = await request.get(
    API_BASE + "/api/v1/projects/" + filtering.image.project_id + "/task-views",
    { headers: { Authorization: "Bearer " + token } },
  );
  expect(viewsResponse.ok()).toBe(true);
  const views = await viewsResponse.json();
  const required = views.items.find(
    (view: { key: string }) => view.key === "missing-required-attributes",
  );
  expect(required.filter_json.op).toBe("or");
  expect(required.filter_json.rules.some((rule: { op?: string }) => rule.op === "and")).toBe(true);
  const requiredResponse = queryResponse(page, filtering.image.project_id, "tasks");
  await page.goto(url(filtering.image.project_id, { view: "builtin:missing-required-attributes" }));
  const response = await requiredResponse;
  expect(response.request().postDataJSON().filter_json).toEqual(required.filter_json);
  const missing = await checked(response);
  expect(missing.items.map((item: { id: string }) => item.id).sort()).toEqual(
    [...filtering.image.expected.missing_required_task_ids].sort(),
  );
  await expect(page.getByRole("table")).toBeVisible();
  // A short saved-view URL may stay short until an edit; reload must restore the same tree.
  const reloadResponse = queryResponse(page, filtering.image.project_id, "tasks");
  await page.reload();
  const reloaded = await reloadResponse;
  expect(reloaded.request().postDataJSON().filter_json).toEqual(required.filter_json);
  await checked(reloaded);
});

test("same-object conditions survive reload and separate from browser keyword state", async ({
  page,
  seed,
  filtering,
}) => {
  await seed.injectToken(page, filtering.user_emails.anno);
  const expression = {
    op: "and",
    rules: [
      { field: "annotation.class_name", op: "eq", value: "car" },
      { field: "annotation.attribute.bbox.color", op: "eq", value: "blue" },
    ],
  };
  const state = { lens: "tasks", filter: envelope(expression), keep: "filter-test" };
  for (let pass = 0; pass < 2; pass += 1) {
    const response = queryResponse(page, filtering.image.project_id, "tasks");
    if (pass === 0) await page.goto(url(filtering.image.project_id, state));
    else await page.reload();
    const result = await checked(await response);
    expect(result.items.map((item: { id: string }) => item.id)).toEqual(
      filtering.image.expected.same_object_task_ids,
    );
    await expect(page.getByRole("table")).toBeVisible();
    const search = new URL(page.url()).searchParams;
    expect(search.get("keep")).toBe("filter-test");
    expect(JSON.parse(search.get("filter")!).value).toEqual(expression);
  }

  const input = page.getByRole("textbox", { name: "搜索任务编号或文件名" });
  await expect(input).toHaveValue("");
  const requests: unknown[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/tasks/query") && request.method() === "POST") {
      requests.push(request.postDataJSON());
    }
  });
  const changed = queryResponse(page, filtering.image.project_id, "tasks");
  await input.pressSequentially("same_object", { delay: 20 });
  await checked(await changed);
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("same_object");
  expect(requests).toHaveLength(1);
  expect(JSON.stringify(requests[0])).toContain("same_object");
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("object and logical-track totals retain full scope across cursor pages", async ({
  page,
  seed,
  filtering,
}) => {
  const objectsResponse = queryResponse(page, filtering.paging.project_id, "objects");
  await page.goto(url(filtering.paging.project_id, { lens: "objects" }));
  const objects = await checked(await objectsResponse);
  expect(objects.total).toBe(101);
  expect(objects.items.map((item: { annotation_id: string }) => item.annotation_id)).toEqual(
    filtering.paging.expected_page_one_object_ids,
  );
  await expect(page.getByRole("table")).toHaveAttribute("aria-rowcount", "101");
  const moreObjects = queryResponse(page, filtering.paging.project_id, "objects");
  await scrollDataPageToBottom(page);
  const next = await checked(await moreObjects);
  expect(next.total).toBe(101);
  expect(next.items.map((item: { annotation_id: string }) => item.annotation_id)).toEqual(
    filtering.paging.expected_page_two_object_ids,
  );

  await seed.injectToken(page, filtering.user_emails.anno);
  const tracksResponse = queryResponse(page, filtering.lidar.project_id, "tracks");
  await page.goto(url(filtering.lidar.project_id, { lens: "tracks" }));
  const tracks = await checked(await tracksResponse);
  expect(tracks.total).toBe(filtering.lidar.track_refs.length);
  await expect(page.getByRole("table")).toHaveAttribute(
    "aria-rowcount",
    String(filtering.lidar.track_refs.length),
  );
  const moreTracks = queryResponse(page, filtering.lidar.project_id, "tracks");
  await scrollDataPageToBottom(page);
  const trackNext = await checked(await moreTracks);
  const refs = [...tracks.items, ...trackNext.items].map(
    (item: { track_id: string }) => item.track_id,
  );
  expect(new Set(refs).size).toBe(refs.length);
  expect(refs.sort()).toEqual([...filtering.lidar.track_refs].sort());
});

test("restricted actor cannot discover the hidden batch through rows or totals", async ({
  page,
  seed,
  filtering,
}) => {
  await seed.injectToken(page, filtering.user_emails.anno);
  const response = queryResponse(page, filtering.image.project_id, "tasks");
  await page.goto(url(filtering.image.project_id, { lens: "tasks" }));
  const result = await checked(await response);
  expect(result.items.map((item: { id: string }) => item.id).sort()).toEqual(
    [...filtering.image.expected.visible_task_ids].sort(),
  );
  expect(result.total).toBe(filtering.image.expected.visible_task_ids.length);
  await expect(page.getByRole("table")).toBeVisible();
  for (const hiddenId of filtering.image.expected.hidden_task_ids) {
    expect(JSON.stringify(result)).not.toContain(hiddenId);
  }
});

test("incomplete numeric draft blocks save and retains the last applied query", async ({
  page,
  filtering,
}) => {
  const expression = { field: "annotation.annotation_count", op: "gte", value: 0 };
  const initial = queryResponse(page, filtering.image.project_id, "tasks");
  await page.goto(
    url(filtering.image.project_id, { filter: envelope(expression), keep: "filter-test" }),
  );
  await checked(await initial);
  const requests: unknown[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/tasks/query") && request.method() === "POST")
      requests.push(request.postDataJSON());
  });
  await page.getByRole("button", { name: /标注数.*0/ }).click();
  const value = page.getByRole("textbox", { name: "条件值", exact: true });
  await value.fill("1.");
  await expect(value).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: "保存视图", exact: true })).toBeDisabled();
  expect(JSON.parse(new URL(page.url()).searchParams.get("filter")!).value).toEqual(expression);
  expect(requests).toHaveLength(0);
  await value.fill("2");
  const applied = queryResponse(page, filtering.image.project_id, "tasks");
  await value.press("Enter");
  const response = await applied;
  expect(response.request().postDataJSON().filter_json).toEqual({ ...expression, value: 2 });
  await checked(response);
  await expect
    .poll(() => JSON.parse(new URL(page.url()).searchParams.get("filter")!).value.value)
    .toBe(2);
});

test("deep restored expression stays visible as an error and never becomes an unfiltered query", async ({
  page,
  filtering,
}) => {
  let expression: unknown = { field: "task.status", op: "eq", value: "pending" };
  for (let depth = 0; depth < 40; depth += 1) expression = { op: "and", rules: [expression] };
  const raw = envelope(expression);
  const requests: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/tasks/query") && request.method() === "POST")
      requests.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(url(filtering.image.project_id, { filter: raw, keep: "filter-test" }));
  await expect(page.getByRole("alert").filter({ hasText: "筛选条件嵌套超过 32 层" })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("filter")).toBe(raw);
  expect(requests).toEqual([]);
  expect(errors).toEqual([]);
});

test("grouped saved views persist creation and edits, cancel navigation, and copy independently", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const projectId = filtering.image.project_id;
  const expression = {
    op: "or",
    rules: [
      { field: "task.status", op: "eq", value: "pending" },
      { field: "annotation.annotation_count", op: "gte", value: 0 },
    ],
  };
  const initial = queryResponse(page, projectId, "tasks");
  await page.goto(url(projectId, { filter: envelope(expression) }));
  await checked(await initial);
  await expect(page.getByLabel("root 逻辑关系")).toHaveValue("or");
  await page.getByRole("button", { name: "保存视图", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "保存任务视图" });
  await dialog.getByRole("textbox", { name: "视图名称" }).fill("Filter lifecycle");
  const savedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/projects/${projectId}/task-views`,
  );
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  const saved = await checked(await savedResponse);
  expect(saved.filter_json).toEqual(expression);
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => new URL(page.url()).searchParams.get("view")).toBe("saved:" + saved.id);

  await page.getByLabel("root 逻辑关系").selectOption("and");
  await expect(page.getByText("未保存", { exact: true })).toBeVisible();
  const updatedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname.endsWith(`/task-views/${saved.id}`),
  );
  await page.getByRole("button", { name: "保存视图", exact: true }).click();
  const updated = await checked(await updatedResponse);
  expect(updated.filter_json).toEqual({ ...expression, op: "and" });
  // Saving refetches the current query. Finish that response before registering
  // a reload listener, which must only observe the new document's request.
  await page.waitForLoadState("networkidle");
  const reload = queryResponse(page, projectId, "tasks");
  await page.reload();
  const reloaded = await reload;
  expect(reloaded.request().postDataJSON().filter_json).toEqual(updated.filter_json);
  await checked(reloaded);

  const changed = queryResponse(page, projectId, "tasks");
  await page.getByRole("textbox", { name: "搜索任务编号或文件名" }).fill("unsaved draft");
  await checked(await changed);
  await page.getByRole("button", { name: /^全部任务/ }).click();
  const discard = page.getByRole("alertdialog");
  await expect(discard).toContainText("放弃未保存的视图修改");
  await discard.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "搜索任务编号或文件名" })).toHaveValue(
    "unsaved draft",
  );
  expect(new URL(page.url()).searchParams.get("view")).toBe("saved:" + saved.id);

  const token = await seed.accessToken(filtering.user_emails.admin);
  const copiedResponse = await request.post(
    `${API_BASE}/api/v1/projects/${projectId}/task-views/${saved.id}/copy`,
    { headers: { Authorization: "Bearer " + token }, data: { name: "Filter lifecycle copy" } },
  );
  expect(copiedResponse.ok(), await copiedResponse.text()).toBe(true);
  const copied = await copiedResponse.json();
  expect(copied.id).not.toBe(saved.id);
  expect(copied.filter_json).toEqual(updated.filter_json);
  const copyQuery = queryResponse(page, projectId, "tasks");
  await page.goto(url(projectId, { view: "saved:" + copied.id }));
  const copyResponse = await copyQuery;
  expect(copyResponse.request().postDataJSON().filter_json).toEqual(updated.filter_json);
  await checked(copyResponse);
  await expect(page.getByRole("textbox", { name: "搜索任务编号或文件名" })).toHaveValue("");
});
