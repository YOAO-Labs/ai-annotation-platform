import { openContextToolbar } from "../../fixtures/context-toolbar";
import { waitForScreenshotImage } from "../environment";
import type { ScreenshotScene } from "./_types";

const DARK_WORKBENCH_MATRIX: NonNullable<ScreenshotScene["matrix"]> = {
  themes: ["dark"],
  primaryTheme: "dark",
};

const imageTaskRoute = (catalog: Parameters<ScreenshotScene["route"]>[0], task: string) => {
  const project = catalog.projects.image_demo;
  return `/projects/${project.id}/annotate?task=${project.tasks[task].id}`;
};

// 工作台布局 + AI 工具体系截图。
// 工具激活：ToolDock 按钮 testid 为 `tool-btn-{id}`；AI 工具(smart-point/smart-box/exemplar)
// 激活后画布顶部居中浮 InteractiveToolBar(testid=interactive-toolbar, v0.18.25 取代旧 AIToolDrawer)；
// mask 工具激活后画布上方浮 MaskToolbar(testid=mask-toolbar)，两者互斥。
// AI 工具能否激活取决于 catalog 声明的 backend capability。

export const WORKBENCH_AI_SCENES: ScreenshotScene[] = [
  {
    name: "workbench/layout-overview",
    clock: "live",
    role: "annotator",
    fixture: { project: "image_demo", task: "annotating" },
    route: (catalog) => imageTaskRoute(catalog, "annotating"),
    prepare: async (page) => {
      await waitForScreenshotImage(page);
    },
    // 四区全貌用视口截图（不设 capture）
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/workbench/layout-overview.png",
  },
  {
    name: "mask-brush/toolbar-overview",
    role: "annotator",
    fixture: { project: "image_demo", task: "annotating" },
    route: (catalog) => imageTaskRoute(catalog, "annotating"),
    prepare: async (page) => {
      await page.waitForSelector('[data-testid="workbench-stage"]', { timeout: 5000 });
      await page.getByTestId("tool-btn-mask").click();
      // v0.18.25+ ContextToolbar：激活后先是胶囊，需展开常用工具并点「更多设置」
      // 才会挂载完整 MaskToolbar（testid=mask-toolbar 的弹层）。
      await page.getByTestId("mask-tool-capsule").waitFor({ state: "visible", timeout: 5000 });
      await page.getByTestId("mask-settings-trigger").click();
      await page.getByLabel("更多 Mask 工具").click();
      await page.waitForSelector('[data-testid="mask-toolbar"]', { timeout: 5000 });
      for (const name of ["笔刷", "橡皮"]) {
        const button = page.getByTestId("mask-toolbar").locator(`[aria-label="${name}"]`);
        await button.waitFor({ state: "visible" });
        if (!(await button.isEnabled())) throw new Error(`Mask toolbar: ${name} is disabled`);
      }
      await page.waitForTimeout(200);
    },
    capture: { kind: "locator", selector: '[data-testid="mask-toolbar"]', padding: 24 },
    annotate: [
      {
        selector: '[data-testid="mask-toolbar"] [aria-label="笔刷"]',
        style: "rect-red",
        label: "笔刷",
      },
      {
        selector: '[data-testid="mask-toolbar"] [aria-label="橡皮"]',
        style: "rect-red",
        label: "橡皮",
      },
      { selector: '[data-testid="mask-radius-slider"]', style: "numbered" },
    ],
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/mask-brush/toolbar-overview.png",
  },
  {
    name: "sam/smart-point-toolbar",
    role: "annotator",
    fixture: {
      project: "image_demo",
      task: "annotating",
      backend: "image_interactive",
      capabilities: ["prompt:point", "output:polygon"],
    },
    route: (catalog) => imageTaskRoute(catalog, "annotating"),
    prepare: async (page) => {
      await page.getByTestId("workbench-stage").waitFor({ timeout: 10_000 });
      const button = page.getByTestId("tool-btn-smart-point");
      await button.waitFor({ state: "visible" });
      if (!(await button.isEnabled()))
        throw new Error("sam/smart-point-toolbar: smart-point 被禁用");
      await button.click();
      await openContextToolbar(page, "interactive");
      await page.waitForTimeout(200);
    },
    capture: { kind: "locator", selector: '[data-testid="interactive-toolbar"]', padding: 8 },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/sam/smart-point-toolbar.png",
  },
  {
    name: "sam/interactive-toolbar",
    clock: "live",
    role: "annotator",
    fixture: {
      project: "image_demo",
      task: "annotating",
      backend: "image_interactive",
      capabilities: ["prompt:interactive_box", "output:polygon"],
    },
    route: (catalog) => imageTaskRoute(catalog, "annotating"),
    prepare: async (page) => {
      await waitForScreenshotImage(page);
      const button = page.getByTestId("tool-btn-smart-box");
      await button.waitFor({ state: "visible" });
      if (!(await button.isEnabled())) throw new Error("sam/interactive-toolbar: smart-box 被禁用");
      await button.click();
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar").waitFor({ state: "visible" });
    },
    capture: { kind: "locator", selector: '[data-testid="interactive-toolbar"]', padding: 8 },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/sam/interactive-toolbar.png",
  },
  {
    name: "sam/magic-box-toolbar",
    role: "annotator",
    fixture: {
      project: "image_demo",
      task: "annotating",
      backend: "image_interactive",
      capabilities: ["prompt:interactive_box", "output:bbox"],
    },
    route: (catalog) => imageTaskRoute(catalog, "annotating"),
    prepare: async (page) => {
      await page.getByTestId("workbench-stage").waitFor({ timeout: 10_000 });
      const button = page.getByTestId("tool-btn-magic-box");
      await button.waitFor({ state: "visible" });
      if (!(await button.isEnabled())) throw new Error("sam/magic-box-toolbar: magic-box 被禁用");
      await button.click();
      await openContextToolbar(page, "interactive");
      await page.waitForTimeout(200);
    },
    capture: { kind: "locator", selector: '[data-testid="interactive-toolbar"]', padding: 8 },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/sam/magic-box-toolbar.png",
  },
  {
    name: "sam/exemplar-output-mode",
    role: "annotator",
    fixture: {
      project: "image_demo",
      task: "annotating",
      backend: "image_interactive",
      capabilities: ["prompt:exemplar", "output:polygon"],
    },
    route: (catalog) => imageTaskRoute(catalog, "annotating"),
    prepare: async (page) => {
      await page.waitForSelector('[data-testid="workbench-stage"]', { timeout: 5000 });
      const btn = page.getByTestId("tool-btn-exemplar");
      await btn.waitFor({ state: "visible" });
      if (!(await btn.isEnabled())) throw new Error("sam/exemplar-output-mode: exemplar 被禁用");
      await btn.click({ timeout: 4000 });
      await openContextToolbar(page, "interactive");
      await page.waitForSelector('[data-testid="exemplar-output-mode"]', { timeout: 3000 });
      await page.waitForTimeout(200);
    },
    // padding 需容纳标注标签（绘制在框右上角外侧），否则「输出形态」在右缘被裁掉。
    capture: { kind: "locator", selector: '[data-testid="interactive-toolbar"]', padding: 48 },
    annotate: [
      { selector: '[data-testid="exemplar-output-mode"]', style: "rect-red", label: "输出形态" },
    ],
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/sam/exemplar-output-mode.png",
  },
  {
    name: "sam/ai-inspector-panel",
    role: "admin",
    // Topbar「AI 单题」→ AIPredictionPopover（悬浮 AI 面板：置信度阈值滑块 + 单图预标）
    fixture: {
      project: "image_demo",
      task: "predicted",
      backend: "image_interactive",
      capabilities: ["task:interactive_seg"],
    },
    route: (catalog) => imageTaskRoute(catalog, "predicted"),
    prepare: async (page) => {
      await page.waitForSelector('[data-testid="workbench-stage"]', { timeout: 5000 });
      await page.waitForTimeout(300);
      const aiBtn = page.getByTestId("workbench-ai-single");
      await aiBtn.click({ timeout: 4000 });
      await page.waitForSelector('[data-testid="ai-prediction-popover"]', { timeout: 3000 });
      await page.waitForTimeout(300);
    },
    capture: { kind: "locator", selector: '[data-testid="ai-prediction-popover"]', padding: 8 },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/sam/ai-inspector-panel.png",
  },
];
