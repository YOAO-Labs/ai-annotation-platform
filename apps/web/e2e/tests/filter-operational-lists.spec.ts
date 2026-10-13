import type { Page, Response } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { expect, test as base, type FilteringSeedManifest } from "../fixtures/seed";
import { resetFiltering } from "../fixtures/filtering";

const test = base.extend<{ filtering: FilteringSeedManifest }>({
  filtering: [
    async ({ seed, page }, provideFixture) => {
      const data = await resetFiltering(seed);
      await seed.injectToken(page, data.user_emails.admin);
      await provideFixture(data);
    },
    { auto: true },
  ],
});
test.setTimeout(90_000);

function getResponse(page: Page, path: string, params: Record<string, string> = {}) {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "GET" &&
      url.pathname === "/api/v1" + path &&
      Object.entries(params).every(([key, value]) => url.searchParams.get(key) === value)
    );
  });
}

async function checked(response: Response) {
  expect(response.ok(), response.status() + " " + (await response.text())).toBe(true);
  return response.json();
}

function keysOf(manifest: FilteringSeedManifest) {
  const {
    search_keys: keys,
    display_names: names,
    invitation_emails: emails,
    audit_scopes: scopes,
  } = manifest.operations;
  return { keys, names, emails, scopes };
}

function dashboardURL(query: string, extra: string): string {
  return `/dashboard?q=${encodeURIComponent(query)}&${extra}`;
}

test("project popover and status controls share one applied state and cancel keeps it", async ({
  page,
  filtering,
}) => {
  const searchKeys = keysOf(filtering).keys;
  const initial = getResponse(page, "/projects/query", { search: searchKeys.projects });
  await page.goto(dashboardURL(searchKeys.projects, "layout=grid&keep=filter-test"));
  const projects = await checked(await initial);
  expect(projects.items.map((project: { id: string }) => project.id).sort()).toEqual(
    [...filtering.operations.project_ids].sort(),
  );
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "高级筛选" });
  await drawer.getByRole("button", { name: "已完成", exact: true }).click();
  await drawer.getByRole("button", { name: "取消", exact: true }).click();
  await expect(drawer).not.toBeVisible();
  expect(new URL(page.url()).searchParams.has("status")).toBe(false);

  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await drawer.getByRole("button", { name: "已完成", exact: true }).click();
  const completed = getResponse(page, "/projects/query", { status: "completed" });
  await drawer.getByRole("button", { name: "应用", exact: true }).click();
  await expect(drawer).not.toBeVisible();
  await checked(await completed);
  await expect.poll(() => new URL(page.url()).searchParams.get("status")).toBe("completed");
  const inProgress = getResponse(page, "/projects/query", { status: "in_progress" });
  await page.getByRole("button", { name: "进行中", exact: true }).click();
  await checked(await inProgress);
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await expect(drawer.getByRole("button", { name: "进行中", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await drawer.getByRole("button", { name: "取消", exact: true }).click();
  await expect(drawer).not.toBeVisible();
  await page.reload();
  await expect(page.getByPlaceholder("搜索项目...")).toHaveValue(searchKeys.projects);
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("members restore filters, clear selection on filter changes, and isolate invitation keys", async ({
  page,
  filtering,
}) => {
  const { keys, names, emails } = keysOf(filtering);
  const members = getResponse(page, "/users/query", { status: "active", search: keys.users });
  await page.goto(
    `/users?q=${encodeURIComponent(keys.users)}&status=active&invite_q=${encodeURIComponent(keys.invitations)}&keep=filter-test`,
  );
  const active = await checked(await members);
  expect(active.items.map((user: { id: string }) => user.id)).toContain(
    filtering.operations.user_ids[0],
  );
  expect(active.items.map((user: { id: string }) => user.id)).not.toContain(
    filtering.operations.user_ids[1],
  );
  await page.getByRole("checkbox", { name: `选择 ${names.active}`, exact: true }).check();
  const inactive = getResponse(page, "/users/query", { status: "inactive", search: keys.users });
  await page.getByRole("combobox", { name: "账号状态", exact: true }).selectOption("inactive");
  const result = await checked(await inactive);
  expect(result.items.map((user: { id: string }) => user.id)).toEqual([
    filtering.operations.user_ids[1],
  ]);
  await expect(page.getByRole("button", { name: "清除选择", exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: `选择 ${names.inactive}`, exact: true }),
  ).not.toBeChecked();
  const invitations = getResponse(page, "/invitations/query", { search: keys.invitations });
  await page.getByRole("button", { name: /邀请记录/ }).click();
  const inviteResult = await checked(await invitations);
  expect(inviteResult.items.map((invite: { id: string }) => invite.id)).toEqual([
    filtering.operations.invitation_ids[0],
  ]);
  await expect(page.getByRole("textbox", { name: "搜索邀请", exact: true })).toHaveValue(
    keys.invitations,
  );
  const exportResponse = getResponse(page, "/invitations/export", { search: keys.invitations });
  const downloadReady = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出筛选结果", exact: true }).click();
  const exported = await exportResponse;
  expect(exported.ok()).toBe(true);
  const download = await downloadReady;
  const csv = await readFile((await download.path())!, "utf8");
  expect(csv).toContain(emails.pending);
  expect(csv).not.toContain(emails.accepted);
  const params = new URL(page.url()).searchParams;
  expect(params.get("q")).toBe(keys.users);
  expect(params.get("status")).toBe("inactive");
  expect(params.get("keep")).toBe("filter-test");
});

test("dataset filters preserve deep-link state and browser history", async ({
  page,
  filtering,
}) => {
  const initial = getResponse(page, "/datasets", {
    search: keysOf(filtering).keys.projects,
    data_type: "image",
  });
  await page.goto(
    "/datasets?" +
      new URLSearchParams({
        q: keysOf(filtering).keys.projects,
        data_type: "image",
        dataset: filtering.operations.dataset_ids[0],
        keep: "filter-test",
      }),
  );
  const images = await checked(await initial);
  expect(images.items.map((item: { id: string }) => item.id)).toEqual([
    filtering.operations.dataset_ids[0],
  ]);
  const imageRow = page.locator("#dataset-row-" + filtering.operations.dataset_ids[0]);
  await expect(imageRow.getByRole("button", { name: "收起" })).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.has("dataset")).toBe(false);
  const next = getResponse(page, "/datasets", {
    search: keysOf(filtering).keys.projects,
    data_type: "video",
  });
  await page.getByRole("button", { name: "视频", exact: true }).click();
  const videos = await checked(await next);
  expect(videos.items.map((item: { id: string }) => item.id)).toEqual([
    filtering.operations.dataset_ids[1],
  ]);
  await expect(
    page
      .locator("#dataset-row-" + filtering.operations.dataset_ids[1])
      .getByRole("button", { name: "展开" }),
  ).toBeVisible();
  await page.goBack();
  await expect.poll(() => new URL(page.url()).searchParams.get("data_type")).toBe("image");
  await expect(imageRow).toBeVisible();
  await page.goForward();
  await expect.poll(() => new URL(page.url()).searchParams.get("data_type")).toBe("video");
  await page.reload();
  await expect(page.locator("#dataset-row-" + filtering.operations.dataset_ids[1])).toBeVisible();
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("template default scope stays private and explicit all survives reload", async ({
  page,
  filtering,
}) => {
  const initial = getResponse(page, "/project-templates", {
    search: keysOf(filtering).keys.templates,
    scope: "private",
  });
  await page.goto(
    `/project-templates?q=${encodeURIComponent(keysOf(filtering).keys.templates)}&keep=filter-test`,
  );
  const mine = await checked(await initial);
  expect(mine.map((item: { id: string }) => item.id)).toEqual([
    filtering.operations.template_ids[0],
  ]);
  const allResponse = getResponse(page, "/project-templates", {
    search: keysOf(filtering).keys.templates,
  });
  await page.getByRole("button", { name: "全部", exact: true }).click();
  const all = await checked(await allResponse);
  expect(all.map((item: { id: string }) => item.id).sort()).toEqual(
    [...filtering.operations.template_ids].sort(),
  );
  await expect.poll(() => new URL(page.url()).searchParams.get("scope")).toBe("all");
  await page.reload();
  await expect(
    page.getByText(filtering.operations.display_names.template_public, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(filtering.operations.display_names.template_private, { exact: true }),
  ).toBeVisible();
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("audit detail edits preserve an explicit empty value through reload", async ({
  page,
  filtering,
}) => {
  const rows = getResponse(page, "/audit-logs", {
    detail_key: "scope",
    detail_value: keysOf(filtering).scopes.alpha,
  });
  await page.goto(
    `/audit?detail_key=scope&detail_value=${encodeURIComponent(keysOf(filtering).scopes.alpha)}&scope=all&keep=filter-test`,
  );
  const initial = await checked(await rows);
  expect(JSON.stringify(initial)).toContain(filtering.operations.project_ids[0]);
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  const empty = getResponse(page, "/audit-logs", { detail_key: "scope", detail_value: "" });
  await page.getByPlaceholder("detail 键值（如 super_admin）").fill("");
  await checked(await empty);
  await expect.poll(() => new URL(page.url()).searchParams.get("detail_value")).toBe("");
  await page.reload();
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await expect(page.getByPlaceholder("detail 键名（如 role）")).toHaveValue("scope");
  await expect(page.getByPlaceholder("detail 键值（如 super_admin）")).toHaveValue("");
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("video job filters survive refresh and keep the image status namespace", async ({
  page,
  filtering,
}) => {
  const project = filtering.video.project_id;
  const jobs = getResponse(page, "/video-tracker-jobs", {
    project_id: project,
    status: "pending_review",
  });
  await page.goto(
    "/ai-pre/jobs?" +
      new URLSearchParams({
        tab: "video",
        project_id: project,
        status: "failed",
        video_status: "pending_review",
        keep: "filter-test",
      }),
  );
  const result = await checked(await jobs);
  expect(result.items.length).toBeGreaterThan(0);
  expect(result.items.every((job: { status: string }) => job.status === "pending_review")).toBe(
    true,
  );
  await expect(page.getByRole("combobox", { name: "筛选视频任务状态" })).toHaveValue(
    "pending_review",
  );
  await page.reload();
  await expect(page.getByRole("combobox", { name: "筛选视频任务状态" })).toHaveValue(
    "pending_review",
  );
  expect(new URL(page.url()).searchParams.get("status")).toBe("failed");
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("image job search keeps spaces and applies its page reset with the debounced query", async ({
  page,
  filtering,
}) => {
  const initial = getResponse(page, "/async-jobs", { offset: "20" });
  await page.goto("/ai-pre/jobs?page=2&keep=filter-test");
  await checked(await initial);
  const applied = getResponse(page, "/async-jobs", {
    search: keysOf(filtering).keys.jobs,
    offset: "0",
  });
  const seen: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/async-jobs" && url.searchParams.has("kind"))
      seen.push(url.search);
  });
  const search = page.getByPlaceholder("搜索 prompt...");
  await search.pressSequentially(keysOf(filtering).keys.jobs, { delay: 15 });
  const jobs = await checked(await applied);
  expect(jobs.items.map((job: { id: string }) => job.id)).toEqual([
    filtering.operations.job_ids[0],
  ]);
  await expect(search).toHaveValue(keysOf(filtering).keys.jobs);
  await expect
    .poll(() => new URL(page.url()).searchParams.get("q"))
    .toBe(keysOf(filtering).keys.jobs);
  expect(new URL(page.url()).searchParams.has("page")).toBe(false);
  expect(seen).toHaveLength(1);
  await page.reload();
  await expect(search).toHaveValue(keysOf(filtering).keys.jobs);
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-test");
});

test("project filter stays attached, keeps its draft on resize, and cancels without a query", async ({
  page,
  filtering,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const searchKeys = keysOf(filtering).keys;
  const initial = getResponse(page, "/projects/query", { search: searchKeys.projects });
  await page.goto(dashboardURL(searchKeys.projects, "keep=filter-geometry"));
  await checked(await initial);
  const trigger = page.getByRole("button", { name: "筛选", exact: true });
  const dialog = page.getByRole("dialog", { name: "高级筛选" });
  const queries: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/projects/query") queries.push(request.url());
  });
  await trigger.click();
  await expect(dialog).toHaveAttribute("data-filter-panel", "popover");
  await expect(
    page.locator('[data-testid="modal-overlay"], [data-slot="sheet-overlay"]'),
  ).toHaveCount(0);
  await expect.poll(async () => Math.round((await dialog.boundingBox())!.width)).toBe(448);
  const anchor = (await trigger.boundingBox())!;
  const panel = (await dialog.boundingBox())!;
  expect(Math.abs(panel.x + panel.width - anchor.x - anchor.width)).toBeLessThanOrEqual(1);
  expect(Math.abs(panel.y - anchor.y - anchor.height - 8)).toBeLessThanOrEqual(1);
  await dialog.getByRole("checkbox", { name: "图片", exact: true }).check();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(dialog).toHaveAttribute("data-filter-panel", "sheet");
  await expect(dialog.getByRole("checkbox", { name: "图片", exact: true })).toBeChecked();
  await expect.poll(async () => Math.round((await dialog.boundingBox())!.width)).toBe(390);
  await expect(dialog.getByRole("button", { name: "应用", exact: true })).toBeInViewport();
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.setViewportSize({ width: 1280, height: 480 });
  await expect(dialog).toHaveAttribute("data-filter-panel", "popover");
  await expect(dialog.getByRole("checkbox", { name: "图片", exact: true })).toBeChecked();
  await expect
    .poll(async () => {
      const box = (await dialog.boundingBox())!;
      return box.y >= 11 && box.y + box.height <= 469;
    })
    .toBe(true);
  await expect(dialog.getByRole("button", { name: "应用", exact: true })).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
  expect(new URL(page.url()).searchParams.has("data_type")).toBe(false);
  expect(queries).toHaveLength(0);
  await trigger.click();
  await expect(dialog.getByRole("checkbox", { name: "图片", exact: true })).not.toBeChecked();
});

test("member filter summaries remove one condition without clearing search or account scope", async ({
  page,
  filtering,
}) => {
  const { keys: searchKeys } = keysOf(filtering);
  const initial = getResponse(page, "/users/query", { search: searchKeys.users, status: "active" });
  await page.goto(`/users?q=${encodeURIComponent(searchKeys.users)}&keep=filter-summary`);
  await checked(await initial);
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "成员筛选" });
  const filtered = getResponse(page, "/users/query", {
    search: searchKeys.users,
    status: "active",
    role: "employee",
  });
  await dialog.getByRole("combobox", { name: "角色筛选" }).selectOption("employee");
  await checked(await filtered);
  await dialog.getByRole("button", { name: "完成", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("button", { name: "角色：员工", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "移除角色筛选" }).click();
  await expect(page.getByRole("button", { name: "移除角色筛选" })).toHaveCount(0);
  // The initial query is still fresh in the 30-second cache. Reload verifies
  // restored request scope without requiring a redundant request on removal.
  const removed = getResponse(page, "/users/query", { search: searchKeys.users, status: "active" });
  await page.reload();
  const response = await removed;
  expect(new URL(response.url()).searchParams.has("role")).toBe(false);
  await checked(response);
  expect(new URL(page.url()).searchParams.get("q")).toBe(searchKeys.users);
  expect(new URL(page.url()).searchParams.get("keep")).toBe("filter-summary");
  await expect(page.getByRole("combobox", { name: "账号状态" })).toHaveValue("active");
});
