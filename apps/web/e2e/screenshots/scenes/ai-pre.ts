import type { ScreenshotScene } from "./_types";

// v0.10.18 · 用 page.route mock 后端响应填充空白态截图; 不污染真实 DB.
// 真实 PredictionJobOut shape 见 apps/api/app/api/v1/admin_preannotate_jobs.py:33.

interface MockJob {
  id: string;
  project_id: string;
  project_name: string;
  project_display_id: string | null;
  batch_id: string | null;
  ml_backend_id: string | null;
  prompt: string;
  output_mode: string;
  status: "running" | "completed" | "failed";
  total_tasks: number;
  success_count: number;
  failed_count: number;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  total_cost: number | null;
  error_message: string | null;
}

function makeJob(i: number, status: MockJob["status"] = "completed"): MockJob {
  const startedAt = new Date(Date.now() - i * 3600_000).toISOString();
  const duration = 60_000 + i * 12_000;
  const prompts = [
    "person, car, truck",
    "traffic light, stop sign, pedestrian crossing",
    "person",
    "bicycle, motorcycle",
    "dog, cat",
    "vehicle plates",
    "person, helmet, vest",
    "fire extinguisher",
  ];
  return {
    id: `mock-job-${i}-${"0".repeat(28)}`.slice(0, 36),
    project_id: `mock-proj-${i}-${"0".repeat(26)}`.slice(0, 36),
    project_name: `演示项目 ${i + 1}`,
    project_display_id: `P-${100 + i}`,
    batch_id: `mock-batch-${i}-${"0".repeat(25)}`.slice(0, 36),
    ml_backend_id: null,
    prompt: prompts[i % prompts.length],
    output_mode: i % 3 === 0 ? "auto" : "manual",
    status,
    total_tasks: 80 + i * 20,
    success_count: status === "running" ? 30 : 80 + i * 20 - (status === "failed" ? 3 : 0),
    failed_count: status === "failed" ? 3 : 0,
    started_at: startedAt,
    completed_at: status === "running" ? null : new Date().toISOString(),
    duration_ms: status === "running" ? null : duration,
    total_cost: null,
    error_message: status === "failed" ? "model timeout" : null,
  };
}

export const AI_PRE_SCENES: ScreenshotScene[] = [
  {
    name: "ai-pre/stepper",
    requiresML: true,
    role: "admin",
    route: () => "/ai-pre",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      // 默认 4 步全 pending；截信息架构入口态
      // maintainer 选完项目+batch 后再截「进行中」状态
    },
    target: "docs-site/user-guide/images/projects/ai-pre-stepper.png",
  },
  {
    name: "ai-pre/history-search",
    requiresML: true,
    role: "admin",
    route: () => "/ai-pre/jobs",
    prepare: async (page) => {
      // v0.10.18 · mock /admin/preannotate-jobs list 让搜索框 + 表格有内容可截
      await page.route("**/api/v1/admin/preannotate-jobs*", async (route) => {
        const items = Array.from({ length: 8 }, (_, i) =>
          makeJob(i, i === 1 ? "running" : i === 7 ? "failed" : "completed"),
        );
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items, next_cursor: null }),
        });
      });
      await page.goto("/ai-pre/jobs");
      await page.waitForLoadState("networkidle");
      // 在搜索框打个示例查询词, 截搜索 + 列表态
      const searchInput = page.getByPlaceholder("搜索 prompt...");
      if (await searchInput.count()) {
        await searchInput.fill("person");
      }
      await page.waitForLoadState("networkidle");
    },
    target: "docs-site/user-guide/images/projects/ai-pre-history-search.png",
  },
  // NOTE: ai-pre/empty-alias 已移除 —— PromptComposer（旧「类别无 alias」警告所在）在
  // v0.10.40 ai-pre 重构（项目卡片 + ProjectDetailPanel）中已删除，无对应 UI 可截。
  {
    name: "wizard/step4-backend",
    requiresML: true,
    role: "admin",
    route: () => "/projects",
    prepare: async (page) => {
      await page.waitForLoadState("networkidle");
      const newBtn = page.getByRole("button", { name: /新建项目|新建/ }).first();
      await newBtn.click();
      await page.waitForTimeout(300);
      await page.locator('[data-testid="project-wizard"]').waitFor({ timeout: 3000 });
      // step1 需填项目名才能前进（数据类型/工具单位默认已选）
      const nameInput = page.getByPlaceholder(/智能门店货架/).first();
      await nameInput.fill("演示项目-A");
      // 步骤指示器不可点，逐步点「下一步」到第 4 步「AI 接入」（按钮文本含箭头，正则不锚定）
      for (let i = 0; i < 3; i++) {
        const next = page.getByRole("button", { name: /下一步/ }).first();
        await next.click();
        await page.waitForTimeout(400);
      }
      await page.waitForTimeout(200);
    },
    capture: { kind: "locator", selector: '[data-testid="project-wizard"]', padding: 0 },
    target: "docs-site/user-guide/images/projects/wizard-step4-backend.png",
  },
];
