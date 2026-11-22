import { test, expect } from "../fixtures/seed";

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

  await boxTool.click();
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");

  await page.keyboard.press("Shift+Slash");
  const dialog = page.getByTestId("workbench-hotkeys-dialog");
  await expect(dialog).toBeVisible();

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

  await search.fill("b");
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByText("矩形框工具", { exact: true })).toBeVisible();

  await search.fill("");
  await tabs.getByRole("tab", { name: "绘制与工具" }).click();
  const boxRow = dialog.locator('[data-hotkey-command="image.tool.box"]');
  await boxRow.scrollIntoViewIfNeeded();
  await boxRow.locator("button").first().click();
  await expect(boxRow.getByText("按下新组合…")).toBeVisible();
  await page.keyboard.press("j");
  await expect(boxRow.getByText(/与「循环人工标注」冲突/)).toBeVisible();
  await expect(boxRow.getByRole("button", { name: "确认" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(boxRow.getByText("按下新组合…")).toHaveCount(0);

  await boxRow.locator("button").first().click();
  await page.keyboard.press("Shift+g");
  const shortcutSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/me/preferences") && response.request().method() === "PATCH",
  );
  await boxRow.getByRole("button", { name: "确认" }).click();
  expect((await shortcutSaved).ok()).toBe(true);
  await expect(boxRow.getByText("已自定义")).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(boxTool).toBeFocused();
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("v");
  await expect(boxTool).toHaveAttribute("aria-pressed", "false");
  await page.keyboard.press("b");
  await expect(boxTool).toHaveAttribute("aria-pressed", "false");
  await page.keyboard.press("Shift+g");
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");
  await expect(boxTool).toContainText("Shift+G");

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

  await trigger.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(12, 450);
  await expect(dialog).toBeHidden();
  expect(errors).toEqual([]);
});
