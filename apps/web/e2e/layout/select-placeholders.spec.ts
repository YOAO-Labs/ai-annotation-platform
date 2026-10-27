import { expect, test, type Locator } from "@playwright/test";

const fixture = "/e2e/fixtures/select-placeholders.html";
const color = (element: Locator) => element.evaluate((node) => getComputedStyle(node).color);

test.beforeEach(async ({ page }) => {
  // This fixture uses actual controls and styles without API/auth/seed data.
  await page.route("**/api/**", (route) => route.abort());
  await page.goto(fixture);
});

test("native prompts are muted and absent from the selectable choices", async ({ page }) => {
  const select = page.getByRole("combobox", { name: "候选模型", exact: true });
  const muted = await color(page.getByTestId("muted"));
  const foreground = await color(page.getByTestId("foreground"));
  expect(muted).not.toBe(foreground);
  await expect(select).toHaveCSS("color", muted);
  const prompt = select.locator('option[value=""]');
  await expect(prompt).toHaveJSProperty("disabled", true);
  await expect(prompt).toBeHidden();
  await expect(select.locator('option[value="candidate-a"]')).toHaveCSS("color", foreground);
  await expect(select.locator('option[value="unavailable"]')).toHaveCSS("color", muted);
  await select.focus();
  await page.keyboard.press("ArrowDown");
  await expect(select).toHaveValue("candidate-a");
  await page.keyboard.press("Home");
  await expect(select).toHaveValue("candidate-a");
  await expect(select).toHaveCSS("color", foreground);
  await page.getByRole("button", { name: "重置模型" }).click();
  await expect(select).toHaveValue("");
  await expect(select).toHaveCSS("color", muted);
  await expect(page.getByRole("combobox", { name: "保留的执行记录" })).toHaveCSS("color", muted);
  await expect(page.getByRole("combobox", { name: "当前无可用部署" })).toBeDisabled();
  await expect(page.getByRole("combobox", { name: "当前无可用部署" })).toHaveCSS("color", muted);
});

test("real empty filter values and explicit binding reset remain usable", async ({ page }) => {
  const foreground = await color(page.getByTestId("foreground"));
  const muted = await color(page.getByTestId("muted"));
  const filter = page.getByRole("combobox", { name: "状态筛选" });
  await expect(filter).toHaveCSS("color", foreground);
  await expect(filter.locator('option[value=""]')).toHaveJSProperty("disabled", false);
  await filter.selectOption("ready");
  await filter.selectOption("");
  await expect(filter).toHaveValue("");
  const binding = page.getByLabel("原生类别 0 · car", { exact: true });
  await expect(binding).toHaveCSS("color", foreground);
  await page.getByRole("button", { name: "清除原生类别 0 · car 的绑定" }).click();
  await expect(binding).toHaveValue("");
  await expect(binding).toHaveCSS("color", muted);
  await expect(page.getByLabel("提交次数")).toHaveText("0");
  await binding.selectOption("行人");
  await expect(binding).toHaveCSS("color", foreground);
  await expect(page.getByLabel("绑定结果")).toContainText('"0":"行人"');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("Radix placeholders remain outside the listbox and restore normal value color", async ({
  page,
}) => {
  const select = page.getByRole("combobox", { name: "模型配置" });
  await expect(select).toHaveCSS("color", await color(page.getByTestId("muted")));
  await select.click();
  const listbox = page.getByRole("listbox");
  await expect(listbox.getByRole("option")).toHaveCount(2);
  await expect(listbox.getByText("选择模型配置")).toHaveCount(0);
  await page.getByRole("option", { name: "配置 A" }).click();
  await expect(select).toHaveText("配置 A");
  await expect(select).toHaveCSS("color", await color(page.getByTestId("foreground")));
});
