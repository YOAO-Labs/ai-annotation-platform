/**
 * 流程录制：快捷键面板（按 ? 打开）。
 *
 * 输出：outputs/flows/hotkey-cheatsheet.mp4 → docs-site/.../workbench/hotkey-cheatsheet.mp4
 *
 * 进 screenshot catalog 的 image_demo 图片工作台 → 按 `?` 打开「快捷键」面板
 * （HotkeyCheatSheet.tsx：用途分类导航 + 类型过滤 + 搜索，Settings 同款几何）→
 * 停留展示分类 → 在搜索框输入关键词演示跨分类过滤 → 清空 → Escape 关闭。
 * 不落任何标注，无需 afterAll 清理。
 *
 * 返回 { drawStartMs, drawEndMs }：交互段起止时间戳，供 finalize 裁掉开头(加载)与结尾。
 */
import type { Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { openImageAnnotate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

export async function runHotkeyCheatSheet(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  await openImageAnnotate(page, catalog);
  await page.waitForTimeout(1400);

  const drawStartMs = Date.now();

  // 按 ? 打开快捷键面板（全局 keydown，工作台默认无聚焦输入框）
  await page.keyboard.press("?");
  const dialog = page.getByTestId("workbench-hotkeys-dialog");
  await dialog.waitFor({ timeout: 4000 });
  // 搜索框自动聚焦；停留展示左侧用途分类（常用 / 绘制与工具 / …）
  await page.waitForTimeout(1800);

  // 搜索框逐字输入演示跨分类过滤
  const search = dialog.getByPlaceholder("搜索动作 / 按键 / 生效条件…");
  await search.click();
  for (const ch of "采纳") {
    await search.type(ch);
    await page.waitForTimeout(280);
  }
  await page.waitForTimeout(1400); // 展示过滤后的命中结果（含分类上下文）

  // 清空搜索，回到当前分类
  await search.fill("");
  await page.waitForTimeout(1400);

  // 再展示一次类型过滤：切到「全部类型」看跨工作台条目。
  await dialog.getByRole("radio", { name: "全部类型" }).click();
  await page.waitForTimeout(1400);
  await dialog.getByRole("radio", { name: "当前工作台（图片）" }).click();
  await page.waitForTimeout(900);

  await page.keyboard.press("Escape"); // 关闭面板
  await dialog.waitFor({ state: "hidden", timeout: 4_000 });
  await page.waitForTimeout(1400);

  return { drawStartMs, drawEndMs: Date.now() };
}
