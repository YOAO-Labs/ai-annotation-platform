import type { Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { ScreenshotFixture } from "../catalog";

export type Role = "admin" | "annotator" | "reviewer";

export interface MatrixAxis {
  viewport: "desktop" | "mobile";
  theme: "light" | "dark";
  locale: "zh-CN" | "en-US";
}

export interface ScreenshotScene {
  name: string;
  /** Live backend leases require current browser time; deterministic scenes default to fixed. */
  clock?: "fixed" | "live";
  /** 单角色或多角色（多角色时取第一个登录） */
  role: Role | Role[];
  /** 场景消费的稳定 seed 资源；driver 会在导航前统一 fail-closed 校验。 */
  fixture?: ScreenshotFixture;
  /**
   * ML-only UI without backend/capability fixture requirements; excluded from service-free static selection.
   */
  requiresML?: true;
  route: (catalog: ScreenshotSeedCatalog) => string;
  /** 进页面后的准备步骤（开 modal / 切 tab / 键盘交互等） */
  prepare?: (page: Page, catalog: ScreenshotSeedCatalog) => Promise<void>;

  /**
   * 自动红框 / 编号注释（截图前用 SVG overlay 注入，截图后自动移除）。
   * 仅在 page 级截图生效（fullPage / viewport / clip）；locator 截图无效。
   */
  annotate?: Array<{
    selector: string;
    style?: "rect-red" | "rect-blue" | "arrow" | "numbered";
    label?: string;
  }>;

  /**
   * 网络状态模拟（使用 page.route 拦截 API 请求）。
   * - happy    : 正常（不拦截，默认）
   * - empty    : 目标页列表端点返回空结果
   * - error    : 目标页关键端点返回 500
   * - loading  : 所有 /api/v1/** 延迟 30s（截图前已禁动画，用于展示 skeleton）
   * - rate-limited : 返回 429
   */
  mockState?: "happy" | "empty" | "error" | "loading" | "rate-limited";

  /**
   * 截图模式（不填 = viewport 截图，等同 v0.8.7 行为）。
   * - fullPage : fullPage:true
   * - locator  : locator.screenshot()，可加 padding 扩展边距
   * - clip     : page.screenshot({ clip: rect })
   */
  capture?:
    | { kind: "fullPage" }
    | { kind: "locator"; selector: string; padding?: number }
    | { kind: "clip"; rect: { x: number; y: number; width: number; height: number } };

  /**
   * 额外 mask 选择器（叠加到 driver 全局默认 mask 之上）。
   * Playwright 会用紫色块遮盖匹配到的元素。
   */
  mask?: string[];

  /**
   * 矩阵维度声明（不填 = 只跑 desktop-light）。
   * driver 根据当前 Playwright project 名称决定是否跑此 scene。
   */
  matrix?: {
    viewports?: Array<"desktop" | "mobile">;
    themes?: Array<"light" | "dark">;
    locales?: Array<"zh-CN" | "en-US">;
    /** 写入无后缀 target 的主题；工作台文档以暗色主产物为准。 */
    primaryTheme?: "light" | "dark";
  };

  /**
   * 输出路径（相对仓库根 docs-site/user-guide/...）。
   * 矩阵非主轴时 driver 自动追加后缀（.light / .dark / .mobile 等）。
   */
  target: string | ((axis: MatrixAxis) => string);
}
