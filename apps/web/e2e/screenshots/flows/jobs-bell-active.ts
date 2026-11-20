/** 高清母版：顶栏后台任务混排、取消入口与完成产物下载。 */
import { expect, type Locator, type Page } from "@playwright/test";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

async function moveTo(page: Page, locator: Locator): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[jobs-bell-active] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(page, pointerByPage.get(page) ?? { x: 90, y: 110 }, target, 260);
  pointerByPage.set(page, target);
}

function jobFixture() {
  const now = new Date().toISOString();
  const base = {
    project_id: "demo-project",
    user_id: "demo-user",
    project_display_id: "P-COCO8",
    project_name: "真实道路场景",
    result: {},
    error_message: null,
    celery_task_id: "demo-celery-task",
    started_at: now,
    completed_at: null,
    created_at: now,
    updated_at: now,
  };
  return {
    items: [
      {
        ...base,
        id: "demo-preannotate-running",
        kind: "batch_predict",
        status: "running",
        progress_pct: 64,
        payload: { project_display_id: "P-COCO8", total_tasks: 25 },
      },
      {
        ...base,
        id: "demo-predictions-import",
        kind: "predictions_import",
        status: "running",
        progress_pct: 37,
        payload: { project_display_id: "P-VIDEO-DEV" },
      },
      {
        ...base,
        id: "demo-export-completed",
        kind: "export",
        status: "completed",
        progress_pct: 100,
        payload: { project_display_id: "P-COCO8", targets: ["coco", "native_json"] },
        result: {
          download_url: "https://download.example.invalid/P-COCO8_export.zip",
          file_count: 18,
          size_bytes: 2_621_440,
        },
        completed_at: now,
      },
    ],
    total: 3,
  };
}

async function installJobsFixture(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.removeItem("wb:jobsbell:filter");
    localStorage.removeItem("wb:jobsbell:dismissed");
  });
  await page.route("**/api/v1/async-jobs**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "GET" || url.pathname !== "/api/v1/async-jobs") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(jobFixture()),
    });
  });
}

export async function runJobsBellActive(page: Page): Promise<DrawWindow> {
  await installJobsFixture(page);
  await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible({ timeout: 10_000 });
  const bell = page.getByRole("button", { name: "后台任务" });
  await expect(page.getByTestId("jobs-bell-badge")).toHaveText("2");

  const drawStartMs = Date.now();
  await page.waitForTimeout(2_200);
  await moveTo(page, bell);
  await bell.click();

  const dialog = page.getByRole("dialog", { name: "后台任务" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("后台任务 (2 进行中)");
  await expect(dialog).toContainText("批量预标 · P-COCO8");
  await expect(dialog).toContainText("预测导入 · P-VIDEO-DEV");
  await expect(dialog).toContainText("数据导出 · P-COCO8 · COCO + 原生 JSON");
  const cancel = page.getByTestId("job-cancel-demo-preannotate-running");
  const download = page.getByTestId("job-download-demo-export-completed");
  await expect(cancel).toBeVisible();
  await expect(download).toBeVisible();
  await page.waitForTimeout(2_800);

  await moveTo(page, cancel);
  await page.waitForTimeout(2_700);
  await moveTo(page, download);
  await page.waitForTimeout(3_400);

  return { drawStartMs, drawEndMs: Date.now() };
}
