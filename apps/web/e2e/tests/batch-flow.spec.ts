/**
 * v0.8.5 · batch lifecycle E2E：smoke + 多角色串联。
 *
 * - smoke: super_admin 登录 → 项目设置页 200
 * - 多角色串联: annotator 提交（advance_task）→ reviewer 看到 /review 列表 →
 *   admin 在项目设置 → batches tab 看到批次 section（settings-tab-batches）。
 *
 * 走 _test_seed.advance_task 跳过 UI 提交链路，spec 重点在多角色登录跳转 +
 * 状态推进 + 关键页面元素可见。
 */
import { test, expect } from "../fixtures/seed";

test.describe("batch lifecycle", () => {
  test("super_admin 登录 → 项目设置页 200（smoke）", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${data.project_id}/settings`);
    await expect(page).toHaveURL(new RegExp(`/projects/${data.project_id}/settings`));
    await page.waitForLoadState("networkidle");
  });

  test("annotator 提交 → reviewer 待审 → admin 设置页 batches tab", async ({ page, seed }) => {
    const data = await seed.owned();

    // 1. annotator 提交一个 task；另一个推到 review，reviewer 双绑定
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

    // 2. reviewer 登录 → /review 路由可达
    await seed.injectToken(page, data.reviewer_email);
    await page.goto("/review");
    await expect(page).toHaveURL(/\/review/);
    await page.waitForLoadState("networkidle");

    // 3. admin 登录 → 项目设置 → 切 batches tab
    await page.context().clearCookies();
    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${data.project_id}/settings`);
    await page.waitForLoadState("networkidle");
    const batchesTab = page.getByTestId("settings-tab-batches");
    await expect(batchesTab).toBeVisible({ timeout: 10_000 });
    await batchesTab.click();
    // 切到 batches section 后页面无崩溃；URL 不变
    await expect(page).toHaveURL(new RegExp(`/projects/${data.project_id}/settings`));
  });
});
