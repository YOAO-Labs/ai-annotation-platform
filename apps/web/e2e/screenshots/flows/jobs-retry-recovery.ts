/**
 * 高清母版：失败预测的真实重试、完成确认与结果审阅。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import { waitForRecordingWorkbenchLayout } from "./_workbench-layout";
import type { DrawWindow } from "./rotated-bbox";

const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

async function moveTo(page: Page, locator: Locator): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[jobs-retry-recovery] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(page, pointerByPage.get(page) ?? { x: 90, y: 110 }, target, 260);
  pointerByPage.set(page, target);
}

export async function runJobsRetryRecovery(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.ocr_demo;
  const task = project.tasks.ocr;
  await page.goto("/ai-pre/jobs?status=failed");
  await expect(page.getByRole("heading", { name: "AI 任务历史" })).toBeVisible({
    timeout: 10_000,
  });
  const statusFilter = page.getByRole("combobox");
  await expect(statusFilter).toHaveValue("failed");
  const failedRow = page
    .locator("tbody tr")
    .filter({ hasText: project.display_id })
    .filter({ hasText: "失败" })
    .first();
  await expect(failedRow).toBeVisible({ timeout: 10_000 });
  const drawStartMs = Date.now();
  await page.waitForTimeout(2_000);

  await moveTo(page, failedRow);
  await failedRow.click();
  const dialog = page.getByRole("dialog", { name: "后台任务详情" });
  await expect(dialog).toBeVisible({ timeout: 10_000 });
  await expect(dialog.getByText("失败", { exact: true }).first()).toBeVisible();
  await expect(dialog).toContainText("RapidOCR 推理节点连接超时");
  await expect(dialog).toContainText("仅重新排队已记录的失败预测，已成功项保留。");
  await page.waitForTimeout(2_500);

  const retryButton = dialog.getByRole("button", { name: "重试失败项" });
  await moveTo(page, retryButton);
  const queued = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/v1\/async-jobs\/[^/]+\/retry-failed(?:\?|$)/.test(response.url()),
    { timeout: 15_000 },
  );
  await retryButton.click();
  const queuedResponse = await queued;
  if (queuedResponse.status() !== 202) {
    throw new Error(
      `[jobs-retry-recovery] 重试排队失败：HTTP ${queuedResponse.status()} ${await queuedResponse.text()}`,
    );
  }
  await expect(page.getByText("已排队重试 1 条失败项", { exact: true })).toBeVisible();

  await expect
    .poll(
      () =>
        page.evaluate(async (taskDisplayId) => {
          const token = localStorage.getItem("token");
          const response = await fetch(
            "/api/v1/async-jobs?kind=prediction_retry&status=completed&limit=20&offset=0",
            { headers: token ? { Authorization: `Bearer ${token}` } : {} },
          );
          if (!response.ok) return false;
          const body = (await response.json()) as {
            items?: Array<{
              payload?: { task_display_id?: string };
              result?: { success_count?: number };
            }>;
          };
          return Boolean(
            body.items?.some(
              (job) =>
                job.payload?.task_display_id === taskDisplayId && job.result?.success_count === 1,
            ),
          );
        }, task.display_id),
      { timeout: 45_000, intervals: [500, 800, 1_200] },
    )
    .toBe(true);
  await page.waitForTimeout(1_300);

  await moveTo(page, dialog.getByRole("button", { name: "关闭" }));
  await dialog.getByRole("button", { name: "关闭" }).click();
  await expect(dialog).toBeHidden();
  await moveTo(page, statusFilter);
  await statusFilter.selectOption("");
  await expect(statusFilter).toHaveValue("");

  const completedRow = page
    .locator("tbody tr")
    .filter({ hasText: task.display_id })
    .filter({ hasText: "retry" })
    .first();
  await expect(completedRow).toContainText("已完成", { timeout: 15_000 });
  await expect(completedRow).toContainText("0");
  await page.waitForTimeout(2_800);

  const workbenchButton = completedRow.getByTitle("去工作台");
  await expect(workbenchButton).toBeEnabled();
  await moveTo(page, workbenchButton);
  await workbenchButton.click();
  await page.waitForURL(new RegExp(`/projects/${project.id}/annotate\\?`), { timeout: 10_000 });
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await page.waitForFunction(
    () =>
      Number(
        document
          .querySelector('[data-testid="workbench-stage"]')
          ?.getAttribute("data-ai-box-count"),
      ) > 0,
    undefined,
    { timeout: 20_000 },
  );
  const candidate = page.locator('[data-testid^="box-list-item-pred-"]').first();
  await candidate.waitFor({ state: "visible", timeout: 10_000 });
  await candidate.scrollIntoViewIfNeeded();
  await moveTo(page, candidate);
  await candidate.click();
  await expect(candidate).toContainText(/\d+%/);
  await page.waitForTimeout(3_500);

  return { drawStartMs, drawEndMs: Date.now() };
}
