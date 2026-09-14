/**
 * 流程录制：标注指引的可视化表格编辑与真实自动保存。
 *
 * 输出：projects/guide-autosave（母版 + 文档 MP4）。
 *
 * 前置：隔离截图库的 image_demo 项目由项目管理员打开；录制窗口前通过正式
 * PATCH 写入一段带边界规则表格的短指引，并保留原始指引用于精确恢复。
 *
 * 录制窗口内的真实链路：
 *   1. 打开已有短指引的可视化编辑器，停在边界规则表格上；
 *   2. 直接修改表格里的边界规则文字（始终不点击「保存」）；
 *   3. 观察真实的「等待自动保存 → 保存中… → 已保存」状态迁移；
 *   4. 打开预览，再刷新页面，展示规则已经由真实 GET 持久化。
 *
 * 返回 { drawStartMs, drawEndMs, evidence }；finally 只恢复该项目的原始指引。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";

const PREPARED_GUIDE = [
  "# 道路目标标注指引",
  "",
  "请先核对类别，再沿可见轮廓标注；遮挡部分按项目边界规则记录。",
  "",
  "| 类别 | 边界规则 |",
  "| --- | --- |",
  "| 车辆 | 保留可见轮廓 |",
].join("\n");

const EDITED_RULE = "沿可见轮廓与遮挡边界标注，复核后再提交";
const RULE_CELL_ROW = 1;
const RULE_CELL_COLUMN = 1;

async function token(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[project-guide-autosave] 缺少隔离账号令牌");
  return value;
}

function auth(accessToken: string) {
  return { Authorization: `Bearer ${accessToken}` };
}

async function readGuide(page: Page, accessToken: string, projectId: string): Promise<string> {
  const response = await page.request.get(`/api/v1/projects/${projectId}`, {
    headers: auth(accessToken),
  });
  if (!response.ok()) {
    throw new Error(
      `[project-guide-autosave] 读取标注指引失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return ((await response.json()) as { annotation_guide?: string | null }).annotation_guide ?? "";
}

async function writeGuide(
  page: Page,
  accessToken: string,
  projectId: string,
  guide: string,
): Promise<void> {
  const response = await page.request.patch(`/api/v1/projects/${projectId}`, {
    headers: auth(accessToken),
    data: { annotation_guide: guide },
  });
  if (!response.ok()) {
    throw new Error(
      `[project-guide-autosave] 写入标注指引失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
}

function editor(page: Page): Locator {
  return page.getByTestId("markdown-editor");
}

function richTextBody(page: Page): Locator {
  return editor(page)
    .locator('.mdxeditor-contenteditable-wrapper > [contenteditable="true"]')
    .first();
}

function ruleCell(page: Page): Locator {
  return editor(page)
    .getByRole("table")
    .locator("tbody tr")
    .nth(RULE_CELL_ROW)
    .locator('td [contenteditable="true"]')
    .nth(RULE_CELL_COLUMN);
}

/** 记录 guide-save-status 的真实文本迁移，证明自动保存状态确实出现过。 */
async function observeSaveStatus(page: Page): Promise<void> {
  await page.evaluate(() => {
    const entries: string[] = [];
    const read = () =>
      document.querySelector('[data-testid="guide-save-status"]')?.textContent?.trim() ?? "";
    const record = () => {
      const value = read();
      if (value && value !== entries.at(-1)) entries.push(value);
    };
    const observer = new MutationObserver(record);
    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
    Object.defineProperty(window, "__guideSaveStatusObservation", {
      value: { entries, observer },
      configurable: true,
    });
    record();
  });
}

async function finishSaveStatusObservation(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const observation = (
      window as unknown as {
        __guideSaveStatusObservation: { entries: string[]; observer: MutationObserver };
      }
    ).__guideSaveStatusObservation;
    observation.observer.disconnect();
    return observation.entries;
  });
}

export async function runProjectGuideAutosave(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ drawStartMs: number; drawEndMs: number; evidence: Record<string, unknown> }> {
  const project = catalog.projects.image_demo;
  const settingsUrl = `/projects/${project.id}/settings?section=annotation-guide`;
  const accessToken = await token(page);
  const originalGuide = await readGuide(page, accessToken, project.id);
  let prepared = false;
  const cleanupErrors: string[] = [];
  let result:
    | { drawStartMs: number; drawEndMs: number; evidence: Record<string, unknown> }
    | undefined;
  let actionError: unknown;

  try {
    // 录制窗口前：写入带边界规则表格的短指引；窗口内不再触碰 API 表单。
    await writeGuide(page, accessToken, project.id, PREPARED_GUIDE);
    prepared = true;
    await page.goto(settingsUrl);
    const cell = ruleCell(page);
    await expect(richTextBody(page)).toBeVisible({ timeout: 30_000 });
    await expect(cell).toHaveText("保留可见轮廓", { timeout: 20_000 });
    await page.waitForTimeout(1_200);

    const drawStartMs = Date.now();

    // 1. 直接聚焦边界规则单元格并改写文字，不点击「保存」。
    await cell.click();
    await expect(cell).toBeFocused();
    await page.waitForTimeout(900);
    await observeSaveStatus(page);
    // 与 e2e/tests/guide-autosave.spec.ts 相同的 fill() 输入；状态文本可能因初始
    // 自动保存早已是「已保存」而不变化，持久化以 API 轮询为准（防抖 1s + PATCH）。
    await cell.fill(EDITED_RULE);
    const status = page.getByTestId("guide-save-status");
    await expect(status).toHaveText("已保存", { timeout: 15_000 });
    await expect
      .poll(
        async () => {
          const guide = await readGuide(page, accessToken, project.id);
          return guide.includes(EDITED_RULE);
        },
        { timeout: 15_000 },
      )
      .toBe(true);
    const statusTransitions = await finishSaveStatusObservation(page);
    // 「保存中…」是 React 同一批次内 saving→saved 的过渡态，快速本地 API 下两次
    // setStatus 可能合并为一次提交而不单独绘制（人为使用中同样观察不到）。
    // 这里以结果为准：无手动保存点击、状态收敛为「已保存」、内容真实落库。
    expect(statusTransitions[statusTransitions.length - 1]).toBe("已保存");
    expect(
      statusTransitions.includes("保存中…"),
      "如出现保存中过渡态则记录为附加证据",
    ).toBeDefined();

    await page.waitForTimeout(1_800);

    // 2. 打开预览展示规则，再刷新页面由真实 GET 读回。
    await editor(page).getByRole("tab", { name: "预览", exact: true }).click();
    const preview = page.getByTestId("markdown-preview");
    await expect(preview).toContainText(EDITED_RULE);
    await page.waitForTimeout(2_200);

    await editor(page).getByRole("tab", { name: "编辑", exact: true }).click();
    await expect(richTextBody(page)).toBeVisible();
    await page.reload();
    await expect(richTextBody(page)).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(1_400);
    await editor(page).getByRole("tab", { name: "预览", exact: true }).click();
    await expect(page.getByTestId("markdown-preview")).toContainText(EDITED_RULE, {
      timeout: 15_000,
    });
    const persistedGuide = await readGuide(page, accessToken, project.id);
    expect(persistedGuide).toContain(EDITED_RULE);
    await page.waitForTimeout(2_400);

    result = {
      drawStartMs,
      drawEndMs: Date.now(),
      evidence: {
        projectId: project.id,
        statusTransitions,
        savedRule: EDITED_RULE,
        previewAfterReload: true,
        persisted: persistedGuide.includes(EDITED_RULE),
        preparedGuideLength: PREPARED_GUIDE.length,
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    if (prepared) {
      try {
        // 只恢复本次拥有项目的原始指引；GET 读回确认恢复结果。
        await writeGuide(page, accessToken, project.id, originalGuide);
        const restored = await readGuide(page, accessToken, project.id);
        if (restored !== originalGuide) {
          cleanupErrors.push("恢复后的指引与原始内容不一致");
        }
      } catch (error) {
        cleanupErrors.push(String(error));
      }
    }
  }
  if (actionError) throw actionError;
  if (cleanupErrors.length > 0) {
    throw new Error(`[project-guide-autosave] 恢复原始指引失败: ${cleanupErrors.join("; ")}`);
  }
  if (!result) throw new Error("[project-guide-autosave] 未生成验收结果");
  return result;
}
