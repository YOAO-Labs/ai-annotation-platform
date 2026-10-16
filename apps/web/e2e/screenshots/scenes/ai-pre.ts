import type { ScreenshotScene } from "./_types";

import { expect } from "@playwright/test";
import type { AsyncJob } from "../../../src/api/asyncJobs";

// Deterministic management observations, not model-inference evidence.
function makeJob(i: number, status: AsyncJob["status"], now: number): AsyncJob {
  const startedAt = new Date(now - (i + 1) * 3600_000).toISOString();
  const completedAt = status === "running" ? null : new Date(now - i * 3600_000).toISOString();
  const prompts = [
    "person, car, truck",
    "traffic light, stop sign",
    "person",
    "bicycle, motorcycle",
    "dog, cat",
    "vehicle plates",
    "person, helmet, vest",
    "fire extinguisher",
  ];
  return {
    id: `mock-job-${i}-${"0".repeat(28)}`.slice(0, 36),
    kind: "batch_predict",
    project_id: `mock-proj-${i}-${"0".repeat(26)}`.slice(0, 36),
    project_name: `演示项目 ${i + 1}`,
    project_display_id: `P-${100 + i}`,
    user_id: null,
    status,
    progress_pct: status === "running" ? 38 : 100,
    payload: {
      prompt: prompts[i % prompts.length],
      output_mode: "manual",
      total_tasks: 80 + i * 20,
      model_label: "YOLO 目标检测",
    },
    result: {
      success_count: status === "running" ? 30 : 80 + i * 20,
      failed_count: status === "failed" ? 3 : 0,
      duration_ms: 60_000 + i * 12_000,
    },
    started_at: startedAt,
    completed_at: completedAt,
    created_at: startedAt,
    updated_at: completedAt ?? startedAt,
    celery_task_id: null,
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
      // Match the current async-jobs API and its server-side prompt search.
      const now = await page.evaluate(() => Date.now());
      await page.route("**/api/v1/async-jobs*", async (route) => {
        const search = new URL(route.request().url()).searchParams.get("search") ?? "";
        const items = Array.from({ length: 8 }, (_, i) =>
          makeJob(i, i === 1 ? "running" : i === 7 ? "failed" : "completed", now),
        ).filter((job) => String(job.payload.prompt).includes(search));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items, total: items.length }),
        });
      });
      await page.goto("/ai-pre/jobs");
      await page.waitForLoadState("networkidle");
      // 在搜索框打个示例查询词, 截搜索 + 列表态
      const searchInput = page.getByPlaceholder("搜索 prompt...");
      await expect(searchInput).toBeVisible();
      await searchInput.fill("person");
      await expect(page).toHaveURL(/[?&]q=person(?:&|$)/);
      await expect(page.getByRole("row").filter({ hasText: "person, car, truck" })).toBeVisible();
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
