import type { ScreenshotScene } from "./_types";

// 导出截图。导出入口在项目行「更多操作(⋮)」菜单 → 「导出标注数据」→ ExportModal。
// 旧 scene 走 /settings 的 settings-tab-export（已不存在）故截成基本信息页。
// export/progress 已移除：导出在 v0.10+ 异步化（点导出 → toast「导出已入队」+ 右上角
// 任务铃 JobsBell 跟踪下载），无独立「进度条」UI 可截。
export const EXPORT_SCENES: ScreenshotScene[] = [
  {
    name: "export/format-select",
    role: "admin",
    fixture: { project: "image_demo" },
    route: () => "/projects",
    prepare: async (page, catalog) => {
      await page.waitForLoadState("networkidle");
      const projectRow = page.locator("tr", {
        hasText: catalog.projects.image_demo.display_id,
      });
      await projectRow.waitFor({ state: "visible" });
      const moreBtn = projectRow.getByTitle("更多操作");
      await moreBtn.click();
      const exportItem = page.getByRole("menuitem", { name: "导出标注数据" });
      await exportItem.click();
      const dialog = page.getByRole("dialog");
      await dialog.waitFor({ timeout: 3000 });
      await dialog.getByText("COCO", { exact: true }).waitFor();
      await dialog.getByText("YOLO", { exact: true }).waitFor();
    },
    capture: { kind: "locator", selector: '[role="dialog"]', padding: 0 },
    target: "docs-site/user-guide/images/export/format-select.png",
  },
];
