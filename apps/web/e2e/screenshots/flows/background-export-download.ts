/**
 * 高清母版：多格式导出入队、后台进度、产物摘要与真实 ZIP 下载。
 */
import { execFileSync } from "child_process";
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

async function moveTo(page: Page, locator: Locator): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[background-export-download] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(page, pointerByPage.get(page) ?? { x: 90, y: 110 }, target, 260);
  pointerByPage.set(page, target);
}

export async function runBackgroundExportDownload(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  runIsolatedExport: (jobId: string) => void,
  recordJobId: (jobId: string) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.image_demo;
  await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible({ timeout: 10_000 });
  const row = page.locator("tr", { hasText: project.name });
  await expect(row).toBeVisible({ timeout: 10_000 });
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_200);

  const menuButton = row.getByTitle("更多操作");
  await moveTo(page, menuButton);
  await menuButton.click();
  const exportItem = page.getByRole("menuitem", { name: "导出标注数据" });
  await expect(exportItem).toBeVisible();
  await moveTo(page, exportItem);
  await exportItem.click();

  const dialog = page.getByRole("dialog", { name: "导出标注数据" });
  await expect(dialog).toBeVisible({ timeout: 10_000 });
  const coco = dialog.getByRole("button", { name: /^COCO/ });
  await expect(coco).toHaveAttribute("aria-pressed", "true");
  const aapJson = dialog.getByRole("button", { name: /^AAP JSON/ });
  await moveTo(page, aapJson);
  await aapJson.click();
  await expect(aapJson).toHaveAttribute("aria-pressed", "true");
  await expect(dialog).toContainText("已选 2 个目标 → 打包为 1 个 zip");
  await page.waitForTimeout(2_000);

  const startButton = dialog.getByRole("button", { name: "开始导出" });
  await moveTo(page, startButton);
  const queued = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/projects/${project.id}/export`,
    { timeout: 20_000 },
  );
  await startButton.click();
  const queuedResponse = await queued;
  if (queuedResponse.status() !== 202) {
    throw new Error(
      `[background-export-download] 导出入队失败：HTTP ${queuedResponse.status()} ${await queuedResponse.text()}`,
    );
  }
  const body = (await queuedResponse.json()) as { job_id?: string };
  if (!body.job_id) throw new Error("[background-export-download] 导出入队未返回 job_id");
  recordJobId(body.job_id);
  const queuedToast = page.getByText("导出已入队", { exact: true });
  await expect(queuedToast).toBeVisible();
  await expect(dialog).toBeHidden();
  await expect(queuedToast).toBeHidden({ timeout: 8_000 });

  const jobsButton = page.getByRole("button", { name: "后台任务" });
  await moveTo(page, jobsButton);
  await jobsButton.click();
  const jobsDialog = page.getByRole("dialog", { name: "后台任务" });
  await expect(jobsDialog).toBeVisible();
  const jobRow = page.getByTestId(`job-row-${body.job_id}`);
  await expect(jobRow).toBeVisible({ timeout: 10_000 });
  await expect(jobRow).toContainText(`${project.name} · COCO + AAP JSON`);
  await expect(jobRow).toContainText("等待中");
  await page.waitForTimeout(1_200);

  runIsolatedExport(body.job_id);
  await expect(jobRow.getByTestId("job-status-running")).toBeVisible({ timeout: 2_000 });
  await expect(jobRow.getByTestId("job-status-completed")).toBeVisible({ timeout: 10_000 });
  await expect(jobRow).toContainText(/ZIP · \d+ 个文件 ·/);
  await page.waitForTimeout(2_000);

  const downloadLink = page.getByTestId(`job-download-${body.job_id}`);
  await moveTo(page, downloadLink);
  const downloadPromise = page.waitForEvent("download", { timeout: 20_000 });
  await downloadLink.click();
  const download = await downloadPromise;
  const downloadPath = await download.path();
  if (!downloadPath) throw new Error("[background-export-download] 浏览器未保存 ZIP 下载");
  const zipEntries = execFileSync("unzip", ["-Z1", downloadPath], { encoding: "utf8" });
  expect(zipEntries).toContain("coco/");
  expect(zipEntries).toContain("aap_json/");
  expect(download.suggestedFilename()).toMatch(new RegExp(`^${project.display_id}_.+\\.zip$`));
  await page.waitForTimeout(2_800);

  return { drawStartMs, drawEndMs: Date.now() };
}
