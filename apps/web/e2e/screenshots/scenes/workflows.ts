import type { ScreenshotScene } from "./_types";
import type { AsyncJob } from "../../../src/api/asyncJobs";

// 工作流类截图：失败预测恢复 jobs 列表。
// failed-jobs 用 page.route mock 后端响应，不污染真实 DB（同 ai-pre.ts 思路）。
// v0.10.16+ 页面改读 /api/v1/async-jobs，响应为 { items, total } 的 AsyncJob 列表。

function makeFailedJob(i: number): AsyncJob {
  const prompts = [
    "person, car, truck",
    "traffic light, stop sign",
    "person",
    "helmet, vest",
    "vehicle plates",
  ];
  const errors = [
    "model timeout",
    "backend connection refused",
    "CUDA out of memory",
    "invalid prompt schema",
    "rate limited by upstream",
  ];
  const startedAt = new Date(Date.now() - (i + 1) * 3600_000).toISOString();
  const completedAt = new Date(Date.now() - i * 3600_000).toISOString();
  return {
    id: `mock-fail-${i}-${"0".repeat(28)}`.slice(0, 36),
    kind: "batch_predict",
    project_id: `mock-proj-${i}-${"0".repeat(26)}`.slice(0, 36),
    user_id: null,
    project_display_id: `P-${100 + i}`,
    project_name: `演示项目 ${i + 1}`,
    status: "failed",
    progress_pct: 96,
    payload: {
      prompt: prompts[i % prompts.length],
      output_mode: "manual",
      total_tasks: 80 + i * 20,
      model_label: "yolov8n",
    },
    result: {
      failed_count: 3,
      duration_ms: 60_000 + i * 12_000,
      total_cost: null,
    },
    error_message: errors[i % errors.length],
    celery_task_id: null,
    started_at: startedAt,
    completed_at: completedAt,
    created_at: startedAt,
    updated_at: completedAt,
  };
}

export const WORKFLOW_SCENES: ScreenshotScene[] = [
  {
    name: "workflows/failed-prediction-recovery-jobs-list",
    role: "admin",
    route: () => "/ai-pre/jobs",
    prepare: async (page) => {
      // mock 返回全 failed job；不带 status query / 不切 select（客户端筛选会清空 mock 列表），
      // 列表本身全为失败态 + 错误信息，即可展示「失败恢复」场景。
      await page.route("**/api/v1/async-jobs*", async (route) => {
        const items = Array.from({ length: 6 }, (_, i) => makeFailedJob(i));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items, total: items.length }),
        });
      });
      await page.goto("/ai-pre/jobs");
      await page.waitForLoadState("networkidle");
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/workflows/failed-prediction-recovery-jobs-list.png",
  },
];
