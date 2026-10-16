/**
 * 高清母版：从质检批次卡片进入审核工作台，精准退回一项问题任务并核对进度更新。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import { waitForRecordingWorkbenchLayout } from "./_workbench-layout";
import type { DrawWindow } from "./rotated-bbox";

export const REVIEW_REJECT_REASON = "车辆边界存在偏移，请重新贴合目标外沿。";

const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

async function moveTo(page: Page, locator: Locator): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[review-reject] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(page, pointerByPage.get(page) ?? { x: 90, y: 110 }, target, 320);
  pointerByPage.set(page, target);
}

export async function runReviewReject(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.image_demo;
  const batch = project.batches.review;
  const task = project.tasks.review;

  await page.goto("/review");
  await expect(page.getByRole("heading", { name: "质检审核" })).toBeVisible({
    timeout: 10_000,
  });
  const overview = page.locator("section");
  const batchCard = overview
    .getByRole("button")
    .filter({ hasText: batch.display_id })
    .filter({ hasText: "标注员" })
    .first();
  await expect(batchCard).toBeVisible({ timeout: 10_000 });
  await expect(batchCard).toContainText("待审");
  await expect(batchCard).toContainText("通过");
  const initialCardText = (await batchCard.textContent()) ?? "";
  const initialPending = Number(initialCardText.match(/待审\s*(\d+)\//)?.[1]);
  if (!Number.isFinite(initialPending) || initialPending < 2) {
    throw new Error(`[review-reject] 初始待审数不足以验证退回变化：${initialCardText}`);
  }

  const drawStartMs = Date.now();
  await page.waitForTimeout(2_200);
  await moveTo(page, batchCard);
  await batchCard.click();
  await expect(page.getByText(batch.display_id, { exact: true }).first()).toBeVisible({
    timeout: 8_000,
  });
  const taskRow = page
    .getByText(task.display_id, { exact: true })
    .locator('xpath=ancestor::div[.//button[normalize-space()="打开"]][1]');
  await expect(taskRow).toBeVisible({ timeout: 10_000 });
  await expect(taskRow).toContainText("待审核");
  await page.waitForTimeout(2_000);

  const openButton = taskRow.getByRole("button", { name: "打开" });
  await moveTo(page, openButton);
  await openButton.click();
  await page.waitForURL(new RegExp(`/projects/${project.id}/review\\?`), { timeout: 10_000 });
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await expect(page.getByTestId("review-reject")).toBeVisible();
  const annotationRow = page.locator('[data-testid^="box-list-item-"]').first();
  await expect(annotationRow).toBeVisible({ timeout: 10_000 });
  await moveTo(page, annotationRow);
  await annotationRow.click();
  await page.waitForTimeout(1_500);

  const finalMode = page.getByRole("button", { name: "仅最终" });
  await moveTo(page, finalMode);
  await finalMode.click();
  await page.waitForTimeout(1_300);
  const diffMode = page.getByRole("button", { name: "叠加", exact: true });
  await moveTo(page, diffMode);
  await diffMode.click();
  await page.waitForTimeout(1_300);

  const rejectButton = page.getByTestId("review-reject");
  await moveTo(page, rejectButton);
  await rejectButton.click();
  const dialog = page.getByRole("alertdialog", { name: /退回原因/ });
  await expect(dialog).toBeVisible({ timeout: 5_000 });
  const reasonType = dialog.getByRole("button", { name: "位置或尺寸不准", exact: true });
  await moveTo(page, reasonType);
  await reasonType.click();
  const commentDialog = page.getByRole("alertdialog", { name: "补充说明", exact: true });
  await expect(commentDialog).toBeVisible();
  const comment = commentDialog.getByRole("textbox", { name: "补充说明（可选）" });
  await moveTo(page, comment);
  await comment.pressSequentially(REVIEW_REJECT_REASON, { delay: 70 });
  await page.waitForTimeout(1_400);

  const confirm = commentDialog.getByRole("button", { name: "确认退回", exact: true });
  await moveTo(page, confirm);
  const rejected = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/tasks/${task.id}/review/reject`) &&
      response.ok(),
    { timeout: 15_000 },
  );
  await confirm.click();
  await rejected;
  await expect(page.getByText("任务已退回", { exact: true })).toBeVisible({ timeout: 8_000 });
  await page.waitForTimeout(1_300);

  const back = page.getByRole("button", { name: "返回", exact: true });
  await moveTo(page, back);
  await back.click();
  await page.waitForURL(/\/review\?/, { timeout: 10_000 });
  await expect(page.getByRole("button", { name: "返回全部批次" })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText(task.display_id, { exact: true })).toHaveCount(0);
  await page.waitForTimeout(1_400);

  const overviewButton = page.getByRole("button", { name: "返回全部批次" });
  await moveTo(page, overviewButton);
  await overviewButton.click();
  const updatedCard = page
    .locator("section")
    .getByRole("button")
    .filter({ hasText: batch.display_id })
    .filter({ hasText: "标注员" })
    .first();
  await expect(updatedCard).toBeVisible({ timeout: 10_000 });
  await expect
    .poll(async () => {
      const text = (await updatedCard.textContent()) ?? "";
      return Number(text.match(/待审\s*(\d+)\//)?.[1]);
    })
    .toBe(initialPending - 1);
  await moveTo(page, updatedCard);
  await page.waitForTimeout(3_000);

  return { drawStartMs, drawEndMs: Date.now() };
}
