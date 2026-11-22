import { test, expect } from "../fixtures/seed";

test.describe("auth", () => {
  test("健康检查接口可用", async ({ request }) => {
    const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
    const res = await request.get(`${apiBase}/health/db`);
    expect(res.ok()).toBeTruthy();
  });

  test("正确凭证 → 跳角色默认首页", async ({ page, seed }) => {
    const data = await seed.owned();
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
