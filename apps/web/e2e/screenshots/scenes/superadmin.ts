import type { ScreenshotScene } from "./_types";
import type { NotificationItem, NotificationsResponse } from "../../../src/api/notifications";

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
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(800); // recharts 渲染：时间范围下拉 + 4 面板
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/analytics/overview.png",
  },
  {
    name: "superadmin/analytics/heatmap",
    role: "admin",
    route: () => "/admin/analytics",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.locator('[data-testid="panel-heatmap"]').waitFor({ timeout: 4000 });
      await page.waitForTimeout(500);
    },
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
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      // 切「数据组」tab（组创建 + 成员添加）
      const tab = page.getByRole("button", { name: /数据组|用户组/ }).first();
      await tab.click();
      await page.waitForTimeout(400);
      await page.waitForLoadState("networkidle");
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
      // 切「角色」tab 展示权限矩阵预览
      const tab = page.getByRole("button", { name: /角色/ }).first();
      await tab.click();
      await page.waitForTimeout(400);
      await page.waitForLoadState("networkidle");
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
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(400);
      // 切「全部」tab 展示四 tab + scope chip + usage_count
      const allTab = page.getByRole("button", { name: /^全部$|全部/ }).first();
      await allTab.click();
      await page.waitForTimeout(300);
      await page.waitForLoadState("networkidle");
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/superadmin/public-templates/templates-list.png",
  },
  // NOTE: bugs/status-transitions 不再从 DEV 库任取首条 BUG。若恢复自动截图，
  // 必须先将固定 BUG 工单纳入 screenshots seed catalog，否则空库会产生假成功。
];
