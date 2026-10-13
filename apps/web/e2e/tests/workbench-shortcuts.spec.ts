import { test, expect } from "../fixtures/seed";

// v0.24 · 快捷键面板与账号级改绑的验收覆盖：
// - 对齐设置的几何、单一内容滚动区、分类导航 + 类型过滤 + 搜索 + 空态
// - 事件隔离：面板打开时画布快捷键停摆，搜索输入不触发后台命令
// - 录制改绑 → 新键生效 / 旧键失效 / 角标同步 / 恢复默认
// - 冲突拒绝（Fixed 占用按键时不允许确认）
test("快捷键面板：几何、搜索、改绑、冲突与焦点恢复", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const data = await seed.owned();
  await seed.injectToken(page, data.admin_email);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);

  const trigger = page.getByRole("button", { name: "快捷键", exact: true });
  const boxTool = page.getByTestId("tool-btn-box");
  await expect(boxTool).toBeVisible();

  // 打开前 B 切换到矩形框工具（默认绑定仍生效）
  await boxTool.click();
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");

  // ? 键打开面板
  await page.keyboard.press("Shift+Slash");
  const dialog = page.getByTestId("workbench-hotkeys-dialog");
  await expect(dialog).toBeVisible();

  // 几何对齐工作台设置（桌面 1120px 宽、单一内容滚动区）；等开窗动画结束再测量
  await expect(dialog).toBeVisible();
  await page.waitForTimeout(300);
  const width = (await dialog.boundingBox())!.width;
  expect(Math.abs(width - 1120)).toBeLessThanOrEqual(2);
  const search = dialog.getByRole("textbox", { name: "搜索快捷键" });
  await expect(search).toBeFocused();
  const tabs = dialog.getByRole("tablist", { name: "快捷键分类" });
  await expect(tabs.getByRole("tab")).toHaveText([
    "常用",
    "绘制与工具",
    "选择与编辑",
    "画布与视角",
    "AI 与审核",
    "播放与轨迹",
    "任务与系统",
    "鼠标操作",
  ]);

  // 面板内按键不触达画布：搜索框里输入 b 不切换工具
  await search.fill("b");
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");
  // 搜索命中动作名，清空后回到分类
  await expect(dialog.getByText("矩形框工具", { exact: true })).toBeVisible();

  // 改绑矩形框工具主组合：Shift+G
  await search.fill("");
  await tabs.getByRole("tab", { name: "绘制与工具" }).click();
  const boxRow = dialog.locator('[data-hotkey-command="image.tool.box"]');
  await boxRow.scrollIntoViewIfNeeded();
  await boxRow.locator("button").first().click();
  await expect(boxRow.getByText("按下新组合…")).toBeVisible();
  // 冲突候选（J 命中 Fixed 的循环人工标注）不允许确认
  await page.keyboard.press("j");
  await expect(boxRow.getByText(/与「循环人工标注」冲突/)).toBeVisible();
  await expect(boxRow.getByRole("button", { name: "确认" })).toHaveCount(0);
  // Esc 取消录制，面板保持打开
  await page.keyboard.press("Escape");
  await expect(boxRow.getByText("按下新组合…")).toHaveCount(0);

  // 录制无冲突的组合并确认
  await boxRow.locator("button").first().click();
  await page.keyboard.press("Shift+g");
  const shortcutSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/me/preferences") && response.request().method() === "PATCH",
  );
  await boxRow.getByRole("button", { name: "确认" }).click();
  expect((await shortcutSaved).ok()).toBe(true);
  await expect(boxRow.getByText("已自定义")).toBeVisible();

  // 生效验证：先切回选择工具（默认键仍生效），旧 B 失效、新 Shift+G 切换矩形框，角标同步
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  // 入口焦点恢复：面板由「?」在矩形框按钮聚焦时打开，焦点应回到打开前的元素
  await expect(boxTool).toBeFocused();
  // 焦点从入口移开，避免布局控件拦截画布快捷键
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("v");
  await expect(boxTool).toHaveAttribute("aria-pressed", "false");
  await page.keyboard.press("b");
  await expect(boxTool).toHaveAttribute("aria-pressed", "false");
  await page.keyboard.press("Shift+g");
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");
  await expect(boxTool).toContainText("Shift+G");

  // 恢复默认后 B 重新生效
  await trigger.click();
  await expect(dialog).toBeVisible();
  await tabs.getByRole("tab", { name: "绘制与工具" }).click();
  const resetSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/me/preferences") && response.request().method() === "PATCH",
  );
  await boxRow.getByRole("button", { name: "恢复默认" }).click();
  expect((await resetSaved).ok()).toBe(true);
  await page.keyboard.press("Escape");
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("b");
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");
  await expect(boxTool).toContainText("B");

  // 外部点击关闭 + 页面无错误
  await trigger.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(12, 450);
  await expect(dialog).toBeHidden();
  expect(errors).toEqual([]);
});
