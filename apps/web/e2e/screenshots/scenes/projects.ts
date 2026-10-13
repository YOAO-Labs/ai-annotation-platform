import type { Page } from "@playwright/test";
import type { ScreenshotScene } from "./_types";

async function waitForAdminProjectsDashboard(page: Page) {
  await page.getByRole("heading", { name: "Dashboard" }).waitFor({ timeout: 10_000 });
  await page.getByText("全部项目", { exact: true }).waitFor({ timeout: 10_000 });
}

export const PROJECT_SCENES: ScreenshotScene[] = [
  {
    name: "projects/create-entry",
    role: "admin",
    route: () => "/projects",
    prepare: async (page) => {
      await waitForAdminProjectsDashboard(page);
    },
    // 箭头指向「新建项目」按钮
    annotate: [{ selector: '[data-testid="new-project-btn"]', style: "rect-blue" }],
    matrix: { themes: ["light", "dark"] },
    target: "docs-site/user-guide/images/projects/create-entry.png",
  },
  {
    name: "projects/wizard-steps",
    role: "admin",
    route: () => "/projects",
    prepare: async (page) => {
      await waitForAdminProjectsDashboard(page);
      const newBtn = page.getByRole("button", { name: /新建项目|新建/ }).first();
      if (await newBtn.count()) {
        await newBtn.click();
        await page.locator('[data-testid="project-wizard"]').waitFor({ timeout: 10_000 });
      }
    },
    capture: { kind: "locator", selector: '[data-testid="project-wizard"]', padding: 0 },
    target: "docs-site/user-guide/images/projects/wizard-steps.png",
  },
  // ── mockState 示例场景 ────────────────────────────────────────────
  {
    name: "projects/empty-state",
    role: "admin",
    route: () => "/projects",
    prepare: async (page) => {
      await waitForAdminProjectsDashboard(page);
      await page.getByText("没有匹配的项目").waitFor({ timeout: 10_000 });
    },
    mockState: "empty",
    target: "docs-site/user-guide/images/projects/empty-state.png",
  },
  {
    name: "projects/error-state",
    role: "admin",
    route: () => "/projects",
    prepare: async (page) => {
      await page.getByRole("heading", { name: "Dashboard" }).waitFor({ timeout: 10_000 });
      await page.getByText("HTTP 500").first().waitFor({ timeout: 10_000 });
      await page.getByText("没有匹配的项目").waitFor({ timeout: 10_000 });
    },
    mockState: "error",
    target: "docs-site/user-guide/images/projects/error-state.png",
  },
  // ── Tier A 扩展（v0.15.26）：项目设置 / 数据管理 / 模板库 / 预测导入导出 ──
  {
    name: "projects/tool-units-panel",
    role: "admin",
    fixture: { project: "image_demo" },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/settings?section=classes`,
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500); // 类别面板 + 工具单位 tab 渲染
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/projects/tool-units-panel.png",
  },
  {
    name: "projects/batch-status-list",
    role: "admin",
    fixture: { project: "image_demo", batch: "review" },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/settings?section=batches`,
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500); // 批次列表彩色状态徽标
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/projects/batch-status-list.png",
  },
  {
    name: "projects/data-manager-overview",
    role: "admin",
    fixture: { project: "image_demo", task: "predicted" },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/data-manager`,
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page
        .getByRole("checkbox", { name: /^选择任务 / })
        .first()
        .waitFor();
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/projects/data-manager-overview.png",
  },
  {
    name: "projects/data-manager-filter-rules",
    role: "admin",
    fixture: { project: "image_demo", task: "predicted" },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/data-manager`,
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      // 展开/新增一条过滤条件行（字段选择器展开）
      const addRule = page.getByRole("button", { name: "筛选", exact: true });
      await addRule.click();
      await page.getByRole("textbox", { name: "搜索筛选字段" }).waitFor();
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/projects/data-manager-filter-rules.png",
  },
  {
    name: "projects/data-manager-members",
    role: "admin",
    clock: "live",
    fixture: { project: "image_demo" },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/data-manager?section=members`,
    prepare: async (page) => {
      await page.getByRole("heading", { name: "成员绩效", exact: true }).waitFor();
      await page.getByRole("table").getByRole("row").nth(1).getByRole("button").first().waitFor();
    },
    capture: { kind: "fullPage" },
    matrix: { themes: ["light", "dark"] },
    target: "docs-site/user-guide/images/projects/data-manager-members.png",
  },
  {
    name: "projects/template-library-overview",
    role: "admin",
    route: () => "/project-templates",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500); // 管理组入口 + 新建/导出按钮 + 四 tab + 卡片网格
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/projects/template-library-overview.png",
  },
  {
    name: "projects/ml-backends/register-form",
    role: "admin",
    fixture: { project: "image_demo", backend: "image_interactive" },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/settings?section=ml-backends`,
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(300);
      const reg = page.getByRole("button", { name: /管理 backend/ });
      await reg.click();
      await page.waitForTimeout(300);
      await page.waitForSelector('[role="dialog"]', { timeout: 3000 });
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/projects/ml-backends/register-form.png",
  },
  {
    name: "projects/ai-pre-config-panel",
    role: "admin",
    fixture: { project: "image_demo", batch: "active", backend: "image_interactive" },
    route: () => "/ai-pre",
    prepare: async (page, catalog) => {
      await page.waitForLoadState("networkidle");
      const card = page.getByText(catalog.projects.image_demo.name, { exact: true }).first();
      await card.click();
      await page.waitForTimeout(400);
      await page
        .getByText(/待预标批次|批跑预标|跑预标/)
        .first()
        .waitFor({ timeout: 3000 });
      await page.waitForLoadState("networkidle");
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/projects/ai-pre-config-panel.png",
  },
  {
    name: "projects/prediction-import-wizard",
    role: "admin",
    fixture: { project: "image_demo", task: "predicted", backend: "image_interactive" },
    route: () => "/ai-pre",
    prepare: async (page, catalog) => {
      await page.waitForLoadState("networkidle");
      const card = page.getByText(catalog.projects.image_demo.name, { exact: true }).first();
      await card.click();
      await page.waitForTimeout(400);
      const importBtn = page.getByRole("button", { name: /导入预测/ }).first();
      await importBtn.click();
      await page.waitForTimeout(300);
      await page.waitForSelector('[role="dialog"]', { timeout: 3000 });
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/projects/prediction-import-wizard.png",
  },
  {
    name: "projects/prediction-purge-modal",
    role: "admin",
    fixture: { project: "image_demo", task: "predicted" },
    // /projects（AdminProjectsDashboard）的项目卡/行才有 ProjectActionsMenu ⋮ 菜单
    route: () => "/projects",
    prepare: async (page, catalog) => {
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(400);
      const row = page.locator("tr", { hasText: catalog.projects.image_demo.name });
      await row.waitFor({ state: "visible" });
      const menuBtn = row.getByRole("button", { name: "更多操作" });
      await menuBtn.click({ timeout: 3000 });
      await page.waitForTimeout(300);
      const purge = page.getByText("清理预测", { exact: true }).first();
      await purge.click({ timeout: 3000 });
      await page.waitForTimeout(300);
      await page.waitForSelector('[role="dialog"]', { timeout: 3000 });
      await page.waitForTimeout(200);
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/projects/prediction-purge-modal.png",
  },
];
