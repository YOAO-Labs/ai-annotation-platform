import { readFile } from "node:fs/promises";
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

test("project members include the owner and zero-activity members, with scoped CSV", async ({
  page,
  filtering,
}) => {
  const projectId = filtering.image.project_id;
  const endpoint = `/api/v1/projects/${projectId}/performance/members`;
  const listResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === endpoint && response.request().method() === "GET",
  );
  await page.goto(`/projects/${projectId}/data-manager?section=members&keep=management-test`);
  const response = await listResponse;
  expect(response.ok(), await response.text()).toBe(true);
  const result = await response.json();
  expect(result.items.map((item: { user_id: string }) => item.user_id)).toEqual(
    expect.arrayContaining([filtering.users.admin, filtering.users.anno, filtering.users.rev]),
  );
  for (const item of result.items) {
    await expect(page.getByText(item.name, { exact: true }).first()).toBeVisible();
  }
  expect(
    result.items.find((item: { user_id: string }) => item.user_id === filtering.users.admin)
      .is_owner,
  ).toBe(true);
  await expect(page.getByRole("table")).toBeVisible();
  expect(new URL(page.url()).searchParams.get("keep")).toBe("management-test");

  const downloaded = page.waitForEvent("download");
  const exportResponse = page.waitForResponse(
    (item) => new URL(item.url()).pathname === `/api/v1/projects/${projectId}/performance/export`,
  );
  await page.getByRole("button", { name: /导出 CSV/ }).click();
  const exported = await exportResponse;
  expect(exported.ok(), await exported.text()).toBe(true);
  const listQuery = new URL(response.url()).searchParams;
  const exportQuery = new URL(exported.url()).searchParams;
  for (const key of [
    "from",
    "to",
    "timezone",
    "work_type",
    "account_status",
    "include_historical",
  ]) {
    expect(exportQuery.get(key)).toBe(listQuery.get(key));
  }
  const download = await downloaded;
  const path = await download.path();
  expect(path).not.toBeNull();
  const csv = await readFile(path!, "utf8");
  expect(csv).toContain(filtering.users.anno);
  expect(csv).toContain(filtering.users.admin);
});

test("Data filters stay separate from member analytics through section navigation", async ({
  page,
  filtering,
}) => {
  const projectId = filtering.image.project_id;
  const expression = { field: "task.status", op: "eq", value: "pending" };
  const params = new URLSearchParams({
    section: "data",
    filter: JSON.stringify({ v: 1, value: expression }),
  });
  await page.goto(`/projects/${projectId}/data-manager?${params}`);
  await expect(page.getByRole("table")).toBeVisible();
  const membersResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1/projects/${projectId}/performance/members`,
  );
  await page
    .getByRole("navigation", { name: "Data Manager 项目区域" })
    .getByRole("button", { name: /成员绩效/ })
    .click();
  const response = await membersResponse;
  expect(response.ok(), await response.text()).toBe(true);
  const query = new URL(response.url()).searchParams;
  expect(query.has("filter")).toBe(false);
  expect(query.has("filter_json")).toBe(false);
  expect(query.get("q")).toBeNull();
  await page
    .getByRole("navigation", { name: "Data Manager 项目区域" })
    .getByRole("button", { name: /数据浏览/ })
    .click();
  await expect(page.getByRole("table")).toBeVisible();
  expect(JSON.parse(new URL(page.url()).searchParams.get("filter")!).value).toEqual(expression);
});

test("task columns separate unresolved issues from comments and open a read-only preview", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const projectId = filtering.image.project_id;
  const taskId = filtering.image.task_ids.cross_object;
  const annotationId = filtering.image.object_ids.cross_object[0];
  const token = await seed.accessToken(filtering.user_emails.admin);
  const headers = { Authorization: `Bearer ${token}` };

  // Plan example: one open issue with two replies plus three task comments and
  // one annotation comment shows 未解决问题 1 / 评论 4.
  const issueResponse = await request.post(`${API_BASE}/api/v1/feedbacks`, {
    headers,
    data: {
      kind: "issue",
      anchor_type: "task",
      project_id: projectId,
      task_id: taskId,
      body: "root issue",
    },
  });
  expect(issueResponse.ok(), await issueResponse.text()).toBe(true);
  const issueId = (await issueResponse.json()).id;
  for (const body of ["reply 1", "reply 2"]) {
    const reply = await request.post(`${API_BASE}/api/v1/feedbacks`, {
      headers,
      data: {
        kind: "comment",
        anchor_type: "task",
        project_id: projectId,
        task_id: taskId,
        thread_parent_id: issueId,
        body,
      },
    });
    expect(reply.ok(), await reply.text()).toBe(true);
  }
  for (let index = 0; index < 3; index += 1) {
    const note = await request.post(`${API_BASE}/api/v1/feedbacks`, {
      headers,
      data: {
        kind: "comment",
        anchor_type: "task",
        project_id: projectId,
        task_id: taskId,
        body: `note ${index}`,
      },
    });
    expect(note.ok(), await note.text()).toBe(true);
  }
  const annotationComment = await request.post(
    `${API_BASE}/api/v1/annotations/${annotationId}/comments`,
    { headers, data: { body: "annotation comment" } },
  );
  expect(annotationComment.ok(), await annotationComment.text()).toBe(true);

  const queryResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1/projects/${projectId}/tasks/query` &&
      response.request().method() === "POST",
  );
  await page.goto(`/projects/${projectId}/data-manager`);
  const response = await queryResponse;
  expect(response.ok(), await response.text()).toBe(true);
  const result = await response.json();
  const seeded = result.items.find((item: { id: string }) => item.id === taskId);
  expect(seeded.unresolved_issue_count).toBe(1);
  expect(seeded.comment_count).toBe(4);
  expect(seeded.unresolved_feedback_count).toBe(1);

  await expect(page.getByRole("columnheader", { name: "未解决问题" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "评论" })).toBeVisible();
  const row = page.getByRole("row", { name: new RegExp(seeded.display_id) });
  await expect(row).toBeVisible();

  // The read-only preview opens from the task row and can compare with the
  // original image; it never claims, locks or mutates the task.
  const mutations: string[] = [];
  page.on("request", (req) => {
    // The match-evidence endpoint is a read-only POST projection.
    if (req.method() !== "GET" && !req.url().includes("/data-manager/matches")) {
      mutations.push(`${req.method()} ${req.url()}`);
    }
  });
  await row.click();
  await expect(page.getByLabel("标注预览")).toBeVisible();
  await expect(page.getByText(/已保存标注/)).toBeVisible();
  for (const width of [390, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect
      .poll(async () =>
        page.getByLabel("标注预览").evaluate((element) => {
          const canvas = element.querySelector("canvas");
          if (!canvas) return Number.POSITIVE_INFINITY;
          return Math.abs(canvas.getBoundingClientRect().width - element.clientWidth);
        }),
      )
      .toBeLessThan(2);
  }
  await page.getByRole("button", { name: /隐藏标注/ }).click();
  await expect(page.getByRole("button", { name: /显示标注/ })).toBeVisible();
  expect(mutations).toEqual([]);

  // Gallery summaries remain correct when restored table columns omit all
  // counters. Additional query projections must not rewrite those columns.
  await page.getByRole("button", { name: "Close", exact: true }).click();
  const gallery = new URLSearchParams({
    layout: "gallery",
    columns: JSON.stringify({ v: 1, value: ["display_id"] }),
  });
  await page.goto(`/projects/${projectId}/data-manager?${gallery}`);
  const card = page.locator("article").filter({ hasText: seeded.display_id });
  await expect(card.getByText("1 未解决问题", { exact: true })).toBeVisible();
  await expect(card.getByText("4 评论", { exact: true })).toBeVisible();
  expect(JSON.parse(new URL(page.url()).searchParams.get("columns")!).value).toEqual([
    "display_id",
  ]);
});

test("member rows show saved content before submission counts", async ({ page, filtering }) => {
  const projectId = filtering.image.project_id;
  const endpoint = `/api/v1/projects/${projectId}/performance/members`;
  const listResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === endpoint && response.request().method() === "GET",
  );
  await page.goto(`/projects/${projectId}/data-manager?section=members`);
  const response = await listResponse;
  expect(response.ok(), await response.text()).toBe(true);
  const result = await response.json();

  // Saved content appears in the project summary and before submission counts.
  await expect(
    page.locator("section[aria-label='项目成员绩效汇总']").getByText("已标注图片"),
  ).toBeVisible();
  await expect(
    page.locator("section[aria-label='项目成员绩效汇总']").getByText("保留标注"),
  ).toBeVisible();
  const headerCells = await page.getByRole("columnheader").allInnerTexts();
  expect(headerCells.indexOf("已标注图片")).toBeGreaterThan(0);
  expect(headerCells.indexOf("已标注图片")).toBeLessThan(headerCells.indexOf("提交任务"));
  expect(headerCells.indexOf("保留标注")).toBeLessThan(headerCells.indexOf("提交任务"));

  // Row values agree with the API projection for the annotator who saved
  // objects without any submission.
  const annotator = result.items.find(
    (item: { user_id: string }) => item.user_id === filtering.users.anno,
  );
  expect(annotator).toBeTruthy();
  expect(annotator.metrics.annotated_images.value).toBeGreaterThan(0);
  expect(annotator.metrics.retained_objects.value).toBeGreaterThan(0);
  const row = page
    .getByRole("button", { name: new RegExp(annotator.name) })
    .locator("xpath=ancestor::tr");
  await expect(row).toBeVisible();
  await expect(row).toContainText(annotator.metrics.annotated_images.value.toLocaleString("zh-CN"));
  await expect(row).toContainText(annotator.metrics.retained_objects.value.toLocaleString("zh-CN"));
  expect(result.project_totals.annotated_images.value).toBeGreaterThan(0);
});

test("ordinary members cannot read team analytics or team CSV", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const projectId = filtering.image.project_id;
  const token = await seed.accessToken(filtering.user_emails.anno);
  for (const path of ["members", "export"]) {
    const response = await request.get(
      `${API_BASE}/api/v1/projects/${projectId}/performance/${path}`,
      {
        headers: { Authorization: `Bearer ${token}` },
      },
    );
    expect([403, 404]).toContain(response.status());
  }
  await seed.injectToken(page, filtering.user_emails.anno);
  await page.goto(`/projects/${projectId}/data-manager`);
  await expect(page.getByRole("table")).toBeVisible();
  await expect(
    page
      .getByRole("navigation", { name: "Data Manager 项目区域" })
      .getByRole("button", { name: /成员绩效/ }),
  ).toHaveCount(0);
});
