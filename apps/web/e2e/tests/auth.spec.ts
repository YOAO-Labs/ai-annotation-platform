/**
 * v0.8.3 · auth E2E：登录页 → dashboard、错密码、未登录访问受保护路由跳登录。
 */
import { test, expect } from "../fixtures/seed";

test.describe("auth", () => {
  test("健康检查接口可用", async ({ request }) => {
    // smoke：只验 API 启动 + 主存储通。/health 顶层会同时检查 celery，
    // CI 没起 celery worker → 整体 degraded(503)，与本测试意图无关。
    const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
    const res = await request.get(`${apiBase}/health/db`);
    expect(res.ok()).toBeTruthy();
  });

  test("正确凭证 → 跳角色默认首页", async ({ page, seed }) => {
    const data = await seed.owned();
    // seed admin 为 super_admin,Issue #123 后默认落「平台概览」/overview。
    await seed.loginViaUI(page, data.admin_email, "Test1234", /\/overview/);
    await expect(page).toHaveURL(/\/overview/);
  });

  test("错密码 → 仍在登录页 + 错误提示", async ({ page, seed }) => {
    const data = await seed.owned();
    await page.goto("/login");
    await page.getByPlaceholder("输入账号或邮箱").fill(data.admin_email);
    await page.getByPlaceholder("••••••••").fill("WrongPwd123");
    await page.getByRole("button", { name: "登录" }).click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByText(/错误|失败/)).toBeVisible({ timeout: 5_000 });
  });

  test("未登录访问 /dashboard → 跳 /login", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login/);
  });
});
