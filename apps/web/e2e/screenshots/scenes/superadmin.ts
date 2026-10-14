import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { ScreenshotScene } from "./_types";
import type { NotificationItem, NotificationsResponse } from "../../../src/api/notifications";

const API_PREFIX = "/api/v1";
const GROUP_FIXTURE = {
  name: "截图示例 · 项目协作组",
  description: "文档截图示例组；成员为截图种子中的标注员与质检员。",
};
const TEMPLATE_FIXTURES = [
  {
    name: "截图示例 · 项目模板",
    description: "从截图种子项目导出的项目配置示例。",
    scope: "private",
  },
  {
    name: "截图示例 · 公共模板",
    description: "由超级管理员发布的公共项目配置示例。",
    scope: "public",
  },
] as const;

type ApiMethod = "GET" | "POST" | "PATCH" | "DELETE";

interface ScreenshotUserRow {
  id: string;
  email: string;
  role: string;
  group_id: string | null;
  is_active: boolean;
}

interface ScreenshotGroupRow {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
}

interface ScreenshotTemplateRow {
  id: string;
  name: string;
  description: string | null;
  scope: "private" | "organization" | "public";
  created_by: string;
  source_project_id: string | null;
  usage_count: number;
}

async function screenshotApi<T>(
  page: Page,
  method: ApiMethod,
  path: string,
  token: string,
  data?: unknown,
): Promise<T> {
  const response = await page.request.fetch(`${API_PREFIX}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, Connection: "close" },
    ...(data === undefined ? {} : { data }),
  });
  if (!response.ok()) {
    throw new Error(
      `[superadmin screenshot] ${method} ${path}: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  if (response.status() === 204) return undefined as T;
  return (await response.json()) as T;
}

async function screenshotToken(page: Page): Promise<string> {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  if (!token) throw new Error("[superadmin screenshot] 缺少截图管理员令牌");
  return token;
}

/** The seed endpoint itself refuses non-test databases and validates the screenshots profile. */
async function verifyScreenshotDatabase(page: Page, catalog: ScreenshotSeedCatalog): Promise<void> {
  const response = await page.request.get(
    `${API_PREFIX}/__test/seed/catalog?profile=screenshots&backend_requirements=none`,
  );
  if (!response.ok()) {
    throw new Error(
      `[superadmin screenshot] screenshot database verification failed: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const live = (await response.json()) as ScreenshotSeedCatalog;
  if (
    live.seed_revision !== catalog.seed_revision ||
    live.users.admin.id !== catalog.users.admin.id ||
    live.users.annotator.id !== catalog.users.annotator.id ||
    live.users.reviewer.id !== catalog.users.reviewer.id ||
    live.projects.image_demo.id !== catalog.projects.image_demo.id
  ) {
    throw new Error("[superadmin screenshot] screenshot seed catalog changed during capture");
  }
}

async function listScreenshotUsers(page: Page, token: string): Promise<ScreenshotUserRow[]> {
  return screenshotApi<ScreenshotUserRow[]>(page, "GET", "/users?status=all", token);
}

async function prepareScreenshotGroup(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ group: ScreenshotGroupRow; cleanup: () => Promise<void> }> {
  const token = await screenshotToken(page);
  await verifyScreenshotDatabase(page, catalog);
  const users = await listScreenshotUsers(page, token);
  const members = [catalog.users.annotator, catalog.users.reviewer].map((seeded) => {
    const user = users.find((candidate) => candidate.id === seeded.id);
    if (!user || !user.is_active || user.role !== seeded.role) {
      throw new Error(`[superadmin screenshot] seeded member ${seeded.email} is not ready`);
    }
    return user;
  });
  const group = await screenshotApi<ScreenshotGroupRow>(
    page,
    "POST",
    "/groups",
    token,
    GROUP_FIXTURE,
  );
  const cleanup = async () => {
    const currentUsers = await listScreenshotUsers(page, token);
    if (
      currentUsers.some(
        (user) => user.group_id === group.id && !members.some((member) => member.id === user.id),
      )
    ) {
      throw new Error("[superadmin screenshot] fixture group contains an unexpected member");
    }
    for (const original of members) {
      const current = currentUsers.find((user) => user.id === original.id);
      if (
        !current ||
        current.role !== original.role ||
        (current.group_id !== group.id && current.group_id !== original.group_id)
      ) {
        throw new Error("[superadmin screenshot] seeded member changed outside this capture");
      }
      if (current.group_id === group.id) {
        await screenshotApi<ScreenshotUserRow>(
          page,
          "PATCH",
          `/users/${original.id}/group`,
          token,
          { group_id: original.group_id },
        );
      }
    }
    await screenshotApi<void>(page, "DELETE", `/groups/${group.id}`, token);
  };
  try {
    for (const member of members) {
      const assigned = await screenshotApi<ScreenshotUserRow>(
        page,
        "PATCH",
        `/users/${member.id}/group`,
        token,
        { group_id: group.id },
      );
      if (assigned.group_id !== group.id || assigned.role !== member.role) {
        throw new Error("[superadmin screenshot] data-group API changed a seeded member role");
      }
    }
    const savedGroup = (
      await screenshotApi<ScreenshotGroupRow[]>(page, "GET", "/groups", token)
    ).find((candidate) => candidate.id === group.id);
    if (!savedGroup || savedGroup.member_count !== members.length) {
      throw new Error(
        "[superadmin screenshot] data-group member count did not reflect real assignments",
      );
    }
    return { group: savedGroup, cleanup };
  } catch (error) {
    await cleanup();
    throw error;
  }
}

async function prepareScreenshotTemplates(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ templates: ScreenshotTemplateRow[]; cleanup: () => Promise<void> }> {
  const token = await screenshotToken(page);
  await verifyScreenshotDatabase(page, catalog);
  const project = await screenshotApi<{
    id: string;
    name: string;
    type_label: string;
    type_key: string;
  }>(page, "GET", `/projects/${catalog.projects.image_demo.id}`, token);
  if (
    project.id !== catalog.projects.image_demo.id ||
    !project.name ||
    !project.type_label ||
    !project.type_key
  ) {
    throw new Error("[superadmin screenshot] catalog project is missing template source fields");
  }
  const created: ScreenshotTemplateRow[] = [];
  const cleanup = async () => {
    const templates = await screenshotApi<ScreenshotTemplateRow[]>(
      page,
      "GET",
      "/project-templates",
      token,
    );
    for (const original of created) {
      const current = templates.find((template) => template.id === original.id);
      if (
        !current ||
        current.created_by !== catalog.users.admin.id ||
        current.source_project_id !== project.id ||
        current.scope !== original.scope ||
        current.usage_count !== 0
      ) {
        throw new Error("[superadmin screenshot] fixture template changed outside this capture");
      }
      await screenshotApi<void>(page, "DELETE", `/project-templates/${current.id}`, token);
    }
  };
  try {
    for (const fixture of TEMPLATE_FIXTURES) {
      const template = await screenshotApi<ScreenshotTemplateRow>(
        page,
        "POST",
        "/project-templates",
        token,
        {
          name: fixture.name,
          description: fixture.description,
          type_label: project.type_label,
          type_key: project.type_key,
          source_project_id: project.id,
          scope: fixture.scope,
        },
      );
      created.push(template);
      if (
        template.created_by !== catalog.users.admin.id ||
        template.source_project_id !== project.id ||
        template.scope !== fixture.scope ||
        template.usage_count !== 0
      ) {
        throw new Error(
          "[superadmin screenshot] project-template API returned unexpected fixture data",
        );
      }
    }
  } catch (error) {
    await cleanup();
    throw error;
  }
  return { templates: created, cleanup };
}

async function waitForHealthyAnalytics(page: Page): Promise<void> {
  const panels = [
    { endpoint: "throughput_daily", testId: "panel-throughput" },
    { endpoint: "reject_rate_by_type", testId: "panel-reject-rate" },
    { endpoint: "duration_dist", testId: "panel-duration" },
    { endpoint: "activity_heatmap", testId: "panel-heatmap" },
  ] as const;
  const responses = Promise.all(
    panels.map(({ endpoint }) =>
      page.waitForResponse(
        (response) =>
          response.request().method() === "GET" &&
          new URL(response.url()).pathname === `${API_PREFIX}/admin/analytics/${endpoint}`,
        { timeout: 15_000 },
      ),
    ),
  );
  await page.reload({ waitUntil: "networkidle" });
  const bodies = await Promise.all(
    (await responses).map(async (response, index) => {
      if (!response.ok()) {
        throw new Error(
          `[superadmin analytics] ${panels[index].endpoint}: HTTP ${response.status()}`,
        );
      }
      const body = (await response.json()) as { panel?: unknown; data?: unknown };
      if (body.panel !== panels[index].endpoint || body.data === undefined || body.data === null) {
        throw new Error(
          `[superadmin analytics] ${panels[index].endpoint} returned an invalid panel`,
        );
      }
      return body.data;
    }),
  );
  const arrayData = (value: unknown, name: string): Array<Record<string, unknown>> => {
    if (!Array.isArray(value))
      throw new Error(`[superadmin analytics] ${name} data is not an array`);
    return value as Array<Record<string, unknown>>;
  };
  const throughput = arrayData(bodies[0], "throughput_daily");
  const reject = arrayData(bodies[1], "reject_rate_by_type");
  const duration = bodies[2] as Record<string, unknown>;
  const heatmap = arrayData(bodies[3], "activity_heatmap");
  if (
    !throughput.every(
      (row) => typeof row.day === "string" && typeof row.event_count === "number",
    ) ||
    !reject.every(
      (row) =>
        typeof row.reason_type === "string" &&
        typeof row.count === "number" &&
        typeof row.pct === "number",
    ) ||
    !["n", "p50", "p95", "mean"].every(
      (field) => typeof duration[field] === "number" && Number.isFinite(duration[field]),
    ) ||
    !heatmap.every(
      (row) =>
        typeof row.weekday === "number" &&
        typeof row.hour === "number" &&
        typeof row.count === "number",
    )
  ) {
    throw new Error("[superadmin analytics] healthy endpoint returned malformed panel data");
  }
  for (const { testId } of panels) await expect(page.getByTestId(testId)).toBeVisible();
  await expect(page.getByText(/数据初始化中|DuckDB 文件不存在|HTTP 503|HTTP 500/)).toHaveCount(0);
  if (throughput.length > 0) {
    await expect(page.getByTestId("panel-throughput").locator("svg")).toBeVisible();
  } else {
    await expect(page.getByText("所选范围内暂无数据")).toBeVisible();
  }
  if (reject.length > 0) {
    await expect(page.getByTestId("panel-reject-rate").locator("svg")).toBeVisible();
  } else {
    await expect(page.getByText("所选范围内无 reject 记录")).toBeVisible();
  }
  await expect(page.getByTestId("panel-duration")).toContainText("样本数");
  if (heatmap.some((row) => Number(row.count) > 0)) {
    await expect(page.getByTestId("panel-heatmap").locator("[title]")).toHaveCount(168);
  } else {
    await expect(page.getByText("所选范围内暂无工时数据")).toBeVisible();
  }
}

async function fitPermissionMatrix(page: Page): Promise<void> {
  const grid = page.locator("div.grid.grid-cols-2.gap-3.p-4");
  const cards = grid.locator(":scope > div");
  await expect(cards).toHaveCount(4);
  const viewport = page.viewportSize();
  if (!viewport) throw new Error("[superadmin screenshot] viewport is unavailable");
  const measuredBottom = await grid.evaluate((element) =>
    Math.ceil(element.getBoundingClientRect().bottom),
  );
  const height = Math.max(viewport.height, measuredBottom + 24);
  if (height !== viewport.height) {
    await page.setViewportSize({ width: viewport.width, height });
  }
  for (const card of await cards.all()) {
    await expect(card).toBeInViewport({ ratio: 0.95 });
  }
}
// notifications/panel-overview 使用确定性列表：截图库没有持久通知，
// created_at 对齐固定截图时钟（2026-07-13 10:00 +08），保证日期分组与相对时间稳定。
const NOTIFICATION_FIXTURE_ITEMS: NotificationItem[] = [
  {
    id: "8f6a1c40-0000-4000-8000-000000000101",
    type: "task.approved",
    target_type: "task",
    target_id: "8f6a1c40-0000-4000-8000-000000000001",
    payload: {
      task_display_id: "T-6",
      project_id: "8f6a1c40-0000-4000-8000-000000000010",
      project_display_id: "P-COCO8",
      actor_id: "8f6a1c40-0000-4000-8000-000000000020",
      actor_name: "李晓华",
    },
    read_at: null,
    created_at: "2026-07-13T01:52:00Z",
  },
  {
    id: "8f6a1c40-0000-4000-8000-000000000102",
    type: "task.rejected",
    target_type: "task",
    target_id: "8f6a1c40-0000-4000-8000-000000000002",
    payload: {
      task_display_id: "T-4",
      project_id: "8f6a1c40-0000-4000-8000-000000000010",
      project_display_id: "P-COCO8",
      reject_reason_type: "missing",
      reject_reason: "车辆漏标：左侧公交未标注",
      actor_id: "8f6a1c40-0000-4000-8000-000000000020",
      actor_name: "李晓华",
    },
    read_at: "2026-07-13T01:25:00Z",
    created_at: "2026-07-13T01:20:00Z",
  },
  {
    id: "8f6a1c40-0000-4000-8000-000000000103",
    type: "annotation.comment_mentioned",
    target_type: "annotation_comment",
    target_id: "8f6a1c40-0000-4000-8000-000000000003",
    payload: {
      project_id: "8f6a1c40-0000-4000-8000-000000000010",
      task_id: "8f6a1c40-0000-4000-8000-000000000001",
      source: "annotation_comment",
      actor_name: "王芳",
      annotation_id: "8f6a1c40-0000-4000-8000-000000000030",
    },
    read_at: null,
    created_at: "2026-07-13T00:30:00Z",
  },
  {
    id: "8f6a1c40-0000-4000-8000-000000000104",
    type: "feedback.comment_mentioned",
    target_type: "feedback",
    target_id: "8f6a1c40-0000-4000-8000-000000000004",
    payload: {
      project_id: "8f6a1c40-0000-4000-8000-000000000010",
      task_id: "8f6a1c40-0000-4000-8000-000000000002",
      source: "feedback",
      actor_name: "王芳",
    },
    read_at: null,
    created_at: "2026-07-12T23:00:00Z",
  },
  {
    id: "8f6a1c40-0000-4000-8000-000000000105",
    type: "batch.admin_locked",
    target_type: "batch",
    target_id: "8f6a1c40-0000-4000-8000-000000000005",
    payload: {
      batch_display_id: "B-SS-REVIEW",
      batch_name: "截图 · 待审核",
      project_id: "8f6a1c40-0000-4000-8000-000000000010",
      reason: "批量归档前需要核对车辆类别",
    },
    read_at: "2026-07-09T23:00:00Z",
    created_at: "2026-07-09T22:00:00Z",
  },
  {
    id: "8f6a1c40-0000-4000-8000-000000000106",
    type: "export.ready",
    target_type: "export",
    target_id: "8f6a1c40-0000-4000-8000-000000000006",
    payload: {
      project_display_id: "P-COCO8",
      targets: ["annotations", "tasks"],
      format: "coco",
      file_count: 8,
      expires_at: "2026-07-20T02:00:00Z",
    },
    read_at: "2026-07-08T02:10:00Z",
    created_at: "2026-07-08T02:00:00Z",
  },
  {
    id: "8f6a1c40-0000-4000-8000-000000000107",
    type: "job.completed",
    target_type: "async_job",
    target_id: "8f6a1c40-0000-4000-8000-000000000007",
    payload: {
      kind: "video_tracker",
      project_display_id: "P-VIDEO-DEV",
      project_name: "视频追踪（截图）",
      task_display_id: "T-1001",
    },
    read_at: "2026-07-02T02:20:00Z",
    created_at: "2026-07-02T02:00:00Z",
  },
];

const NOTIFICATION_FIXTURE_RESPONSE: NotificationsResponse = {
  items: NOTIFICATION_FIXTURE_ITEMS,
  total: NOTIFICATION_FIXTURE_ITEMS.length,
  unread: NOTIFICATION_FIXTURE_ITEMS.filter((item) => item.read_at === null).length,
};

// 平台管理页（super_admin 视角）：用户 / BUG 反馈 / 离线分析 / 系统健康 / 模型市场 / 审计 / 通知面板。
// 路由见 App.tsx：/users /bugs /admin/analytics /admin/health /model-market /audit。
// 本地 DB 已有 52 条 bug_reports、4w+ audit_logs，故列表 / 详情有内容可截。
// platform.ts 已覆盖 /users「角色」tab（权限矩阵）与侧边栏总览，这里补「成员」列表 + 邀请 modal。

export const SUPERADMIN_SCENES: ScreenshotScene[] = [
  {
    name: "superadmin/users/list",
    role: "admin",
    route: () => "/users",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(300); // 默认「成员」tab：4 张统计卡 + 成员表
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/users/list.png",
  },
  {
    name: "superadmin/users/invite-modal",
    role: "admin",
    route: () => "/users",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      const inviteBtn = page.getByRole("button", { name: /邀请成员/ }).first();
      await inviteBtn.click();
      await page.getByRole("dialog").waitFor({ timeout: 3000 });
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/superadmin/users/invite-modal.png",
  },
  {
    name: "superadmin/bugs/list",
    role: "admin",
    route: () => "/bugs",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(400); // 筛选栏 + 列表表格
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/bugs/list.png",
  },
  // NOTE: bugs/detail-panel 与 bugs/status-transitions 同样需要固定 BUG seed，
  // 不允许在空库时退化为截取列表页。
  {
    name: "superadmin/analytics/overview",
    role: "admin",
    route: () => "/admin/analytics",
    prepare: waitForHealthyAnalytics,
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/analytics/overview.png",
  },
  {
    name: "superadmin/analytics/heatmap",
    role: "admin",
    route: () => "/admin/analytics",
    prepare: waitForHealthyAnalytics,
    capture: { kind: "locator", selector: '[data-testid="panel-heatmap"]', padding: 0 },
    target: "docs-site/user-guide/images/superadmin/analytics/heatmap.png",
  },
  {
    name: "superadmin/system-monitoring/health-panel",
    role: "admin",
    route: () => "/admin/health",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(600); // 组件卡 + Celery 队列表 + Workers
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/system-monitoring/health-panel.png",
  },
  {
    name: "superadmin/model-market/list",
    requiresML: true,
    role: "admin",
    route: () => "/model-market",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500); // 3 张统计卡 + 3 tab（能力目录/运行时观测/注册管理）
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/model-market/list.png",
  },
  {
    name: "superadmin/audit-logs/filter-bar",
    role: "admin",
    route: () => "/audit",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500); // 筛选栏（scope/action/target）+ 分组列表
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/audit-logs/filter-bar.png",
  },
  {
    name: "notifications/panel-overview",
    role: "admin",
    route: () => "/dashboard",
    prepare: async (page) => {
      // 截图库没有持久通知；用确定性列表展示日期分组、类型筛选与未读样式。
      await page.route(
        (url) => url.pathname === "/api/v1/notifications",
        (route) =>
          route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(NOTIFICATION_FIXTURE_RESPONSE),
          }),
      );
      await page.waitForLoadState("networkidle");
      // 顶栏铃铛按钮（data-testid）→ 展开 NotificationsPopover；读屏标签随未读数变化。
      const bell = page.getByTestId("notifications-trigger").first();
      await bell.click();
      await page.getByRole("dialog").waitFor({ timeout: 3000 });
      await page.getByText("通过了任务").waitFor({ timeout: 3000 });
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/notifications/panel-overview.png",
  },
  // ── Tier A 扩展（v0.15.26）：用户 / 审计 / 失败预测 / 健康 / 模型注册 / 模板 / BUG ──
  {
    name: "superadmin/users/edit-modal",
    role: "admin",
    route: () => "/users",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(300);
      // 成员行编辑按钮（title="编辑成员"）
      const editBtn = page.getByRole("button", { name: /编辑成员|编辑/ }).first();
      await editBtn.click();
      await page.getByRole("dialog").waitFor({ timeout: 3000 });
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/superadmin/users/edit-modal.png",
  },
  {
    name: "superadmin/users/groups-tab",
    role: "admin",
    route: () => "/users",
    prepare: async (page, catalog, registerCleanup) => {
      const { group, cleanup } = await prepareScreenshotGroup(page, catalog);
      registerCleanup(cleanup);
      await page.reload({ waitUntil: "networkidle" });
      const tab = page.getByRole("button", { name: /数据组|用户组/ }).first();
      await tab.click();
      await expect(page.getByText(GROUP_FIXTURE.name, { exact: true })).toBeVisible();
      const memberCount = new RegExp(`^${group.member_count} 名成员`);
      await expect(page.getByText(memberCount)).toBeVisible();
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/users/groups-tab.png",
  },
  {
    name: "superadmin/users/permission-matrix",
    role: "admin",
    route: () => "/users",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      // 切「角色」tab 后按卡片真实高度扩展截图视口，避免内部滚动容器裁掉下排角色。
      const tab = page.getByRole("button", { name: /角色/ }).first();
      await tab.click();
      await page.waitForLoadState("networkidle");
      await fitPermissionMatrix(page);
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/users/permission-matrix.png",
  },
  {
    name: "superadmin/audit-logs/detail-modal",
    role: "admin",
    route: () => "/audit",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500);
      // 首行「详情」按钮 → detail_json Modal
      const detailBtn = page.getByRole("button", { name: /详情/ }).first();
      await detailBtn.click();
      await page.getByRole("dialog").waitFor({ timeout: 3000 });
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/superadmin/audit-logs/detail-modal.png",
  },
  // NOTE: superadmin/failed-predictions/list 暂不自动化 —— 带 ?status=failed 时客户端 status
  // 筛选会清空 mock 列表；不带 query 又会混入真实成功 job（绿色徽标），无法干净展示「失败筛选 +
  // 重试/放弃」视图。需真实 failed-job 种子数据，归 Tier B。失败 job 列表本身已由
  // workflows/failed-prediction-recovery-jobs-list 覆盖。
  {
    name: "superadmin/system-monitoring/workers-table",
    role: "admin",
    route: () => "/admin/health",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(600);
    },
    // 单独截 Workers 心跳表（含 Worker / Heartbeat / Pool / Status 表头）
    capture: { kind: "locator", selector: 'table:has-text("Heartbeat")', padding: 8 },
    target: "docs-site/user-guide/images/superadmin/system-monitoring/workers-table.png",
  },
  {
    name: "superadmin/ml-backend/register-form",
    requiresML: true,
    role: "admin",
    // tab 是 role="tab" 且由 URL ?tab= 驱动（ModelMarketPage.tsx），直接深链到注册管理 tab
    route: () => "/model-market?tab=registry",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(400);
      const reg = page.getByRole("button", { name: "注册实例", exact: true });
      await reg.click({ timeout: 3000 });
      const dialog = page.getByRole("dialog");
      await dialog.waitFor({ timeout: 3000 });
      const gpuResource = dialog.locator("#global-backend-gpu-resource");
      const resourceValues = await gpuResource
        .locator("option")
        .evaluateAll((options) =>
          options.map((option) => (option as HTMLOptionElement).value).filter(Boolean),
        );
      if (resourceValues[0]) {
        await gpuResource.selectOption(resourceValues[0]);
        await dialog.locator("#global-backend-vram-budget").fill("1024");
      }
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/superadmin/ml-backend/register-form.png",
  },
  {
    name: "superadmin/public-templates/templates-list",
    role: "admin",
    route: () => "/project-templates",
    prepare: async (page, catalog, registerCleanup) => {
      const { templates, cleanup } = await prepareScreenshotTemplates(page, catalog);
      registerCleanup(cleanup);
      await page.reload({ waitUntil: "networkidle" });
      const allTab = page.getByRole("button", { name: "全部", exact: true });
      await allTab.click();
      await expect(page).toHaveURL(/(?:\?|&)scope=all(?:&|$)/);
      const publicTemplate = templates.find((template) => template.scope === "public")!;
      await expect(page.getByTestId(`template-card-${publicTemplate.id}`)).toBeVisible();
      await page.getByPlaceholder("搜索模板名称…").fill("截图示例");
      const cards = page.locator('[data-testid^="template-card-"]');
      await expect(cards).toHaveCount(templates.length);
      for (const template of templates) {
        const card = page.getByTestId(`template-card-${template.id}`);
        await expect(card).toContainText(template.name);
        await expect(card).toContainText(`使用 ${template.usage_count} 次`);
        await expect(page.getByTestId(`template-scope-${template.id}`)).toHaveText(
          template.scope === "public" ? "公共" : "私有",
        );
      }
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/public-templates/templates-list.png",
  },
  // NOTE: bugs/status-transitions 不再从 DEV 库任取首条 BUG。若恢复自动截图，
  // 必须先将固定 BUG 工单纳入 screenshots seed catalog，否则空库会产生假成功。
];
