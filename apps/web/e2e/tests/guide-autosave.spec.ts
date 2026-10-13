import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { test, expect, type SeedAPI } from "../fixtures/seed";

const API = `${process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010"}/api/v1`;

async function prepare(
  page: Page,
  request: APIRequestContext,
  seed: SeedAPI,
  content = "原有规则",
) {
  const data = await seed.owned();
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const url = `${API}/projects/${data.project_id}`;
  expect((await request.patch(url, { headers, data: { annotation_guide: content } })).ok()).toBe(
    true,
  );
  await seed.injectToken(page, data.admin_email);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/projects/${data.project_id}/settings?section=annotation-guide`);
  const editor = page.getByTestId("markdown-editor");
  const body = editor
    .locator('.mdxeditor-contenteditable-wrapper > [contenteditable="true"]')
    .first();
  await expect(body).toBeVisible({ timeout: 30_000 });
  return {
    ...data,
    editor,
    body,
    errors,
    async read(): Promise<string> {
      const response = await request.get(url, { headers });
      expect(response.ok()).toBe(true);
      return (await response.json()).annotation_guide;
    },
  };
}

async function commitChineseInput(page: Page, input: Locator, read: () => Promise<string>) {
  await input.press("End");
  const beforeComposition = await read();
  const cdp = await page.context().newCDPSession(page);
  try {
    await cdp.send("Input.imeSetComposition", {
      text: "guifan",
      selectionStart: 6,
      selectionEnd: 6,
    });
    // Wait past the autosave interval while the candidate is still active.
    await page.waitForTimeout(1_500);
    expect(await read()).toBe(beforeComposition);
    await expect(input).toBeFocused();
    await cdp.send("Input.insertText", { text: "规范" });
  } finally {
    await cdp.detach();
  }
}

test.describe("guide autosave", () => {
  test.setTimeout(60_000);

  test("rich text and Markdown source save while focus stays in the editor", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(page, request, seed);
    await fixture.body.fill("停止输入后自动保存的规则");
    await expect.poll(fixture.read, { timeout: 8_000 }).toContain("停止输入后自动保存的规则");
    await expect(fixture.body).toBeFocused();
    await expect(page.getByTestId("guide-save-status")).toHaveText("已保存");
    await commitChineseInput(page, fixture.body, fixture.read);
    await expect.poll(fixture.read).toContain("停止输入后自动保存的规则规范");

    await fixture.editor.getByRole("tab", { name: "源码", exact: true }).click();
    const source = fixture.editor.locator(".cm-content:visible");
    await source.fill("# 源码也自动保存\n\n保留这一条规则。");
    await expect.poll(fixture.read).toContain("# 源码也自动保存");
    await expect(source).toBeFocused();
    await expect(page.getByTestId("guide-save-status")).toHaveText("已保存");
    await page.reload();
    await expect(page.getByRole("heading", { name: "源码也自动保存" })).toBeVisible();
    expect(fixture.errors).toEqual([]);
  });

  test("a focused table cell saves without disturbing further typing, undo or Chinese composition", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(
      page,
      request,
      seed,
      "# 表格规则\n\n| 类别 | 规则 |\n| --- | ---: |\n| 车辆 | 原有规则 |\n",
    );
    const cell = fixture.editor
      .getByRole("table")
      .locator("tbody tr")
      .nth(1)
      .locator('td [contenteditable="true"]')
      .nth(1);
    await cell.click();
    await expect(cell).toBeFocused();
    const original = await fixture.read();
    await page.waitForTimeout(1_500);
    expect(await fixture.read()).toBe(original);
    await cell.fill("沿可见轮廓标注");
    await expect.poll(fixture.read, { timeout: 8_000 }).toContain("沿可见轮廓标注");
    await expect(cell).toBeFocused();
    await cell.press("End");
    await page.keyboard.insertText("，另记遮挡");
    await expect.poll(fixture.read).toContain("沿可见轮廓标注，另记遮挡");
    await expect(cell).toBeFocused();
    await cell.press("Control+z");
    await expect(cell).toHaveText("沿可见轮廓标注");
    await expect.poll(fixture.read).not.toContain("另记遮挡");
    await commitChineseInput(page, cell, fixture.read);
    await expect.poll(fixture.read).toContain("沿可见轮廓标注规范");
    await expect(cell).toBeFocused();
    await expect(page.getByTestId("guide-save-status")).toHaveText("已保存");
    await page.reload();
    await expect(page.getByRole("cell", { name: "沿可见轮廓标注规范" })).toHaveCSS(
      "text-align",
      "right",
    );
    expect(fixture.errors).toEqual([]);
  });

  test("a failed automatic save keeps the draft and waits for an explicit retry", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(page, request, seed);
    let writes = 0;
    let reject = true;
    await page.route(`**/api/v1/projects/${fixture.project_id}`, (route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      writes += 1;
      if (!reject) return route.continue();
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"detail":"automatic save unavailable"}',
      });
    });
    await fixture.body.fill("失败后保留的最新规则");
    await expect(page.getByTestId("guide-save-status")).toContainText("保存失败", {
      timeout: 8_000,
    });
    await expect(fixture.body).toBeFocused();
    await expect(fixture.body).toHaveText("失败后保留的最新规则");
    expect(await fixture.read()).toBe("原有规则");
    await page.waitForTimeout(1_500);
    expect(writes).toBe(1);
    reject = false;
    await page.getByRole("button", { name: "重试保存", exact: true }).click();
    await expect.poll(fixture.read).toContain("失败后保留的最新规则");
    await expect(page.getByTestId("guide-save-status")).toHaveText("已保存");
    expect(writes).toBe(2);
    expect(fixture.errors).toEqual([]);
  });

  test("switching settings sections before the debounce finishes flushes the latest draft", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(page, request, seed);
    await fixture.body.fill("立即切换页面也不能丢失的规则");
    await page.getByTestId("settings-tab-general").click();
    await expect(fixture.editor).toHaveCount(0);
    await expect.poll(fixture.read).toContain("立即切换页面也不能丢失的规则");
    await page.getByTestId("settings-tab-annotation-guide").click();
    await expect(fixture.body).toHaveText("立即切换页面也不能丢失的规则");
    expect(fixture.errors).toEqual([]);
  });

  test("a failed retirement save is restored when returning to the guide", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(page, request, seed);
    let reject = true;
    let failedRequestHandled = false;
    await page.route(`**/api/v1/projects/${fixture.project_id}`, async (route) => {
      if (route.request().method() !== "PATCH" || !reject) return route.continue();
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"detail":"test retirement save unavailable"}',
      });
      failedRequestHandled = true;
    });

    await fixture.body.fill("切页保存失败也不能丢失的规则");
    await page.getByTestId("settings-tab-general").click();
    await expect(fixture.editor).toHaveCount(0);
    await expect.poll(() => failedRequestHandled).toBe(true);
    expect(await fixture.read()).toBe("原有规则");

    await page.getByTestId("settings-tab-annotation-guide").click();
    const recoveredEditor = page.getByTestId("markdown-editor");
    const recoveredBody = recoveredEditor
      .locator('.mdxeditor-contenteditable-wrapper > [contenteditable="true"]')
      .first();
    await expect(recoveredBody).toHaveText("切页保存失败也不能丢失的规则");
    await expect(page.getByTestId("guide-save-status")).toContainText("保存失败");

    reject = false;
    await page.getByRole("button", { name: "重试保存", exact: true }).click();
    await expect.poll(fixture.read).toBe("切页保存失败也不能丢失的规则");
    await expect(page.getByTestId("guide-save-status")).toHaveText("已保存");
    expect(fixture.errors).toEqual([]);
  });

  test("leaving a table cell immediately saves its last edit to the original project", async ({
    page,
    request,
    seed,
  }) => {
    const fixture = await prepare(
      page,
      request,
      seed,
      "| 类别 | 规则 |\n| --- | --- |\n| 车辆 | 原有规则 |\n",
    );
    const cell = fixture.editor
      .getByRole("cell", { name: "原有规则", exact: true })
      .locator('[contenteditable="true"]');
    await cell.click();
    await expect(cell).toBeFocused();
    await cell.fill("切页前的最后一条规则");
    await page.getByTestId("settings-tab-general").click();
    await expect(fixture.editor).toHaveCount(0);
    await expect.poll(fixture.read).toContain("切页前的最后一条规则");
    await page.getByTestId("settings-tab-annotation-guide").click();
    await expect(page.getByRole("cell", { name: "切页前的最后一条规则" })).toBeVisible();
    expect(fixture.errors).toEqual([]);
  });
});
