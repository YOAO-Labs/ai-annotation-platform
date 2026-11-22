import { test, expect } from "../fixtures/seed";

test.describe("batch lifecycle", () => {
  test("annotator 提交 → reviewer 待审 → admin 设置页 batches tab", async ({ page, seed }) => {
    const data = await seed.owned();

    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "submitted",
      annotatorEmail: data.annotator_email,
    });
    await seed.advanceTask({
      taskId: data.task_ids[1],
      toStatus: "review",
      annotatorEmail: data.annotator_email,
      reviewerEmail: data.reviewer_email,
    });

    await seed.injectToken(page, data.reviewer_email);
    await page.goto("/review");
    await expect(page).toHaveURL(/\/review/);
    await page.waitForLoadState("networkidle");

    await page.context().clearCookies();
    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${data.project_id}/settings`);
    await page.waitForLoadState("networkidle");
    const batchesTab = page.getByTestId("settings-tab-batches");
    await expect(batchesTab).toBeVisible({ timeout: 10_000 });
    await batchesTab.click();
    await expect(page).toHaveURL(new RegExp(`/projects/${data.project_id}/settings`));
  });
});
