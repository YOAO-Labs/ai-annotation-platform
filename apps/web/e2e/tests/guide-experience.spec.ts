import { readFileSync } from "node:fs";
import type { Locator } from "@playwright/test";
import { test, expect } from "../fixtures/seed";

const API = `${process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010"}/api/v1`;
const PNG = readFileSync(new URL("../fixtures/markdown-example.png", import.meta.url));
const GUIDE = [
  "# 道路目标标注指引",
  "",
  "请先核对类别，再沿可见轮廓标注；遮挡部分按项目规则记录。",
  "",
  "![边界示例](https://guide-example.invalid/boundary.png)",
  "",
  "## 类别与边界",
  "",
  "| 类别 | 边界规则 |",
  "| --- | --- |",
  "| 车辆 | 保留可见轮廓 |",
  "",
  ...Array.from(
    { length: 18 },
    (_, index) => `### 复核规则 ${index + 1}\n\n确认类别、边界、遮挡与属性。\n`,
  ),
  "## 完成检查",
  "",
  "确认没有遗漏后，再提交标注。",
].join("\n");

function richText(editor: Locator) {
  return editor.locator('.mdxeditor-contenteditable-wrapper > [contenteditable="true"]').first();
}

test("empty Markdown prompts differ from authored text in both themes", async ({
  page,
  seed,
  request,
}, info) => {
  test.setTimeout(60_000);
  const data = await seed.owned();
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const changed = await request.patch(`${API}/projects/${data.project_id}`, {
    headers,
    data: { annotation_guide: "" },
  });
  expect(changed.ok()).toBe(true);
  await seed.injectToken(page, data.admin_email);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/projects/${data.project_id}/settings?section=annotation-guide`);
  const editor = page.getByTestId("markdown-editor");
  const body = richText(editor);
  const placeholder = editor
    .locator(".mdxeditor-root-contenteditable > :not(.mdxeditor-contenteditable-wrapper)")
    .first();
  await expect(body).toBeVisible({ timeout: 30_000 });
  await expect(body).toHaveText("");
  for (const theme of ["light", "dark"]) {
    await page.evaluate(
      (value) => document.documentElement.setAttribute("data-theme", value),
      theme,
    );
    await expect(placeholder).toBeVisible();
    const bodyColor = await body.evaluate((element) => getComputedStyle(element).color);
    const hintColor = await placeholder.evaluate((element) => getComputedStyle(element).color);
    expect(hintColor).not.toBe(bodyColor);
    expect(await placeholder.textContent()).not.toContain("\\n");
    await page.screenshot({ path: info.outputPath(`guide-placeholder-${theme}.png`) });
  }
  await body.fill("已经填写的标注规则");
  await expect(placeholder).toBeHidden();
  await expect(body).toHaveText("已经填写的标注规则");
  expect(errors).toEqual([]);
});

test("workbench guide opens from the topbar into a settings-sized reader and preserves work context", async ({
  page,
  seed,
  request,
}, info) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  expect(
    (
      await request.patch(`${API}/projects/${data.project_id}`, {
        headers,
        data: { annotation_guide: GUIDE },
      })
    ).ok(),
  ).toBe(true);
  await seed.injectToken(page, data.admin_email);
  await page.route("https://guide-example.invalid/boundary.png", (route) =>
    route.fulfill({
      contentType: "image/png",
      body: PNG,
    }),
  );
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  const trigger = page.getByRole("button", { name: "标注指引", exact: true });
  const settings = page.getByRole("button", { name: "工作台设置", exact: true });
  const boxTool = page.getByTestId("tool-btn-box");
  await expect(boxTool).toBeVisible({ timeout: 30_000 });
  await boxTool.click();
  await expect(trigger).toBeVisible();
  await expect(page.getByTestId("wb-guide-unread")).toBeVisible();
  expect(
    Math.abs((await trigger.boundingBox())!.y - (await settings.boundingBox())!.y),
  ).toBeLessThan(10);
  await settings.click();
  const settingsDialog = page.getByTestId("workbench-settings-dialog");
  await expect
    .poll(async () => (await settingsDialog.boundingBox())?.width ?? 0)
    .toBeGreaterThanOrEqual(1118);
  const settingsBounds = (await settingsDialog.boundingBox())!;
  await settingsDialog.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(settingsDialog).toHaveCount(0);
  const dialog = page.getByRole("dialog", { name: "标注指引", exact: true });
  await expect(dialog).toHaveCount(0);
  let guideWrites = 0;
  page.on("request", (req) => {
    if (
      req.method() === "PATCH" &&
      req.url().endsWith("/auth/me/preferences") &&
      req.postData()?.includes('"guide_read":true')
    )
      guideWrites += 1;
  });
  await trigger.click();
  await expect(dialog).toBeVisible();
  await expect(dialog).toBeFocused();
  await expect
    .poll(async () => (await dialog.boundingBox())?.width ?? 0)
    .toBeGreaterThanOrEqual(settingsBounds.width - 1);
  const bounds = (await dialog.boundingBox())!;
  expect(Math.abs(bounds.x - settingsBounds.x)).toBeLessThan(2);
  expect(Math.abs(bounds.y - settingsBounds.y)).toBeLessThan(2);
  expect(Math.abs(bounds.width - settingsBounds.width)).toBeLessThan(2);
  expect(Math.abs(bounds.height - settingsBounds.height)).toBeLessThan(2);
  await expect(dialog.getByRole("heading", { name: "道路目标标注指引" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "确认已阅读", exact: true })).toBeInViewport();
  await page.screenshot({ path: info.outputPath("workbench-guide-light.png") });
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  const cardColor = await dialog.evaluate((element) => {
    const probe = document.createElement("span");
    probe.style.backgroundColor = getComputedStyle(element).getPropertyValue("--sc-card");
    return probe.style.backgroundColor;
  });
  await expect(dialog).toHaveCSS("background-color", cardColor);
  await page.screenshot({ path: info.outputPath("workbench-guide-dark.png") });
  // A nested image dialog must sit above the reader, and its first Esc must
  // close only the image, leaving the guide open.
  await dialog.getByRole("button", { name: /放大.*边界示例/ }).click();
  const imageDialog = page.getByRole("dialog", { name: "边界示例", exact: true });
  await expect(imageDialog).toBeVisible();
  const imageDialogId = await imageDialog.getAttribute("id");
  await expect
    .poll(() =>
      imageDialog.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        return element.contains(
          document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2),
        );
      }),
    )
    .toBe(true);
  await page.keyboard.press("Escape");
  // Wait for the closing layer's actual removal; a closing Radix dialog can
  // be aria-hidden before its exit animation releases the Escape handler.
  await expect(page.locator(`[id="${imageDialogId}"]`)).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await dialog.focus();
  await page.keyboard.press("v");
  await page.keyboard.press("Delete");
  await page.keyboard.press("n");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(boxTool).toHaveAttribute("aria-pressed", "true");
  expect(new URL(page.url()).searchParams.get("task")).toBe(data.task_ids[0]);
  expect(guideWrites).toBe(0);
  await trigger.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(12, 450);
  await expect(page.getByTestId("wb-guide-dialog")).toHaveCount(0);
  await expect(trigger).toBeFocused();
  expect(guideWrites).toBe(0);
  await trigger.click();
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/me/preferences") &&
      response.request().method() === "PATCH" &&
      response.request().postData()?.includes('"guide_read":true') === true,
  );
  await dialog.getByRole("button", { name: "确认已阅读", exact: true }).click();
  expect((await saved).ok()).toBe(true);
  await expect(dialog.getByRole("button", { name: "已确认阅读", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "关闭指引", exact: true }).click();
  await page.reload();
  await expect(trigger).toBeVisible();
  await expect(page.getByTestId("wb-guide-unread")).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
  await trigger.click();
  await expect(dialog.getByRole("button", { name: "已确认阅读", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "关闭指引", exact: true }).click();
  await page.setViewportSize({ width: 900, height: 812 });
  await expect(trigger).toBeVisible();
  await trigger.click();
  await expect
    .poll(async () => (await dialog.boundingBox())?.width ?? 0)
    .toBeGreaterThanOrEqual(835);
  const narrow = (await dialog.boundingBox())!;
  expect(narrow.width).toBeGreaterThanOrEqual(835);
  expect(narrow.width).toBeLessThanOrEqual(837);
  await expect(dialog.getByRole("button", { name: "已确认阅读", exact: true })).toBeInViewport();
  await page.screenshot({ path: info.outputPath("workbench-guide-narrow.png") });
  await dialog.getByRole("button", { name: "关闭指引", exact: true }).click();
  await expect(trigger).toBeFocused();
  // The existing app shell blocks workbench interaction below 768 px.
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("heading", { name: "请切换到桌面端" })).toBeVisible();
  await expect(dialog).toHaveCount(0);
  expect(errors).toEqual([]);
});
