import { layoutCommand } from "../../helpers/workbench-layout";
import { panelCommand } from "../../fixtures/workbench-panel-actions";
/**
 * 工作台流程录制的用户偏好沙箱。
 *
 * 录制需要确定性布局，但工作台侧栏和设置都是账号级持久偏好。
 * 这里只改写 GET 响应，并在内存中响应 PATCH，确保页面内交互正常，
 * 同时不把任何录制设置写回真实用户。Playwright context 关闭后即无痕清理。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import {
  createWorkspacePreset,
  type WorkspacePresetId,
} from "../../../src/pages/Workbench/layout/workbenchLayoutPresets";
import type { WorkspaceSnapshot } from "../../../src/pages/Workbench/layout/workbenchLayoutSnapshot";
import {
  WORKSPACE_SCHEMA_VERSION,
  type WorkspaceContext,
} from "../../../src/pages/Workbench/layout/workbenchLayoutSnapshot";
import type {
  UserPreferences,
  WorkbenchLayoutPreferences,
  WorkbenchPreferences,
} from "../../../src/api/auth";

export type RecordingSidebarMode = "both" | "none";

/**
 * 把快照根分支里最右侧的可见停靠列（inspector/discussion 所在竖向分支）加宽到
 * 目标像素，等量收窄画布。录制中真实拖动分隔条会与偏好 PATCH 竞争回弹，这里
 * 直接在沙箱快照里给出宽列，避免依赖 sash 拖拽。
 */
function widenRightColumn(snapshot: WorkspaceSnapshot, targetWidth: number): void {
  const root = snapshot.layout.grid.root;
  if (root.type !== "branch") {
    throw new Error("[recording-layout] 快照根节点不是 branch，无法加宽右栏");
  }
  const visible = root.data.filter((node) => node.visible !== false);
  const rightColumn = visible[visible.length - 1];
  const canvas = visible.find((node) => node.type === "leaf" && node.data.views.includes("canvas"));
  if (!rightColumn || !canvas || rightColumn === canvas) {
    throw new Error("[recording-layout] 快照缺少画布或右栏停靠列");
  }
  const growth = targetWidth - (rightColumn.size ?? 0);
  if (growth <= 0) return;
  rightColumn.size = targetWidth;
  canvas.size = Math.max(1, (canvas.size ?? 0) - growth);
}

export interface RecordingWorkbenchOverrides {
  workspace?: { context: WorkspaceContext; preset: WorkspacePresetId };
  /** 覆盖快照里右栏停靠列的像素宽度（含 inspector/discussion 的竖向分支）。 */
  rightColumnWidth?: number;
  common?: Partial<WorkbenchPreferences["common"]>;
  image?: Partial<WorkbenchPreferences["image"]>;
  video?: Partial<WorkbenchPreferences["video"]>;
  pointcloud?: Partial<WorkbenchPreferences["pointcloud"]>;
  layout?: Partial<WorkbenchLayoutPreferences>;
  ui?: Partial<UserPreferences["ui"]>;
}

function embeddedLayout(
  layout: WorkbenchLayoutPreferences,
  open: boolean,
): WorkbenchLayoutPreferences {
  return {
    ...layout,
    leftOpen: open,
    rightOpen: open,
    floatingTaskQueue: { ...layout.floatingTaskQueue, detached: false },
    floatingClassPalette: { ...layout.floatingClassPalette, detached: false },
    floatingInspector: { ...layout.floatingInspector, detached: false },
    floatingDiscussion: { ...layout.floatingDiscussion, detached: false },
  };
}

function applyRecordingLayout(
  preferences: UserPreferences,
  mode: RecordingSidebarMode,
): UserPreferences {
  const open = mode === "both";
  return {
    ...preferences,
    workbench: {
      ...preferences.workbench,
      common: {
        ...preferences.workbench.common,
        leftWidthPct: 15,
        rightWidthPct: 15,
      },
      layout: {
        ...embeddedLayout(preferences.workbench.layout, open),
        // 选中标注的浮动信息卡默认展开在画布右上（约 340×440），会盖住母版
        // 关键内容。录制沙箱统一让它以收起胶囊起步：需要展示选中详情的流程
        // 仍可在窗口内点击胶囊展开。
        floatingSelection: {
          ...preferences.workbench.layout.floatingSelection,
          collapsed: true,
        },
      },
    },
  };
}

function mergeWorkbench(
  current: WorkbenchPreferences,
  patch: Partial<WorkbenchPreferences>,
): WorkbenchPreferences {
  return {
    ...current,
    ...patch,
    common: { ...current.common, ...(patch.common ?? {}) },
    image: { ...current.image, ...(patch.image ?? {}) },
    video: { ...current.video, ...(patch.video ?? {}) },
    pointcloud: { ...current.pointcloud, ...(patch.pointcloud ?? {}) },
    layout: { ...current.layout, ...(patch.layout ?? {}) },
  };
}

function mergePreferences(
  current: UserPreferences,
  patch: Partial<UserPreferences>,
): UserPreferences {
  return {
    ...current,
    ...patch,
    workbench: patch.workbench
      ? mergeWorkbench(current.workbench, patch.workbench)
      : current.workbench,
    ai: { ...current.ai, ...(patch.ai ?? {}) },
    ui: { ...current.ui, ...(patch.ui ?? {}) },
  };
}

export async function installRecordingWorkbenchLayout(
  page: Page,
  mode: RecordingSidebarMode,
  overrides: RecordingWorkbenchOverrides = {},
): Promise<void> {
  const original = await page.evaluate(async () => {
    const token = localStorage.getItem("token");
    if (!token) throw new Error("[recording-layout] localStorage 缺少 token");
    const response = await fetch("/api/v1/auth/me/preferences", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      throw new Error(`[recording-layout] 读取偏好失败: HTTP ${response.status}`);
    }
    return response.json() as Promise<UserPreferences>;
  });

  if (overrides.workspace && process.env.SCREENSHOT_RECORDING_PROFILE) {
    const browserTime = await page.evaluate(() => Date.now());
    expect(
      Math.abs(Date.now() - browserTime),
      "Live recording clock must match the host",
    ).toBeLessThan(5000);
  }
  const screenshotTheme = await page.evaluate(() => localStorage.getItem("anno.theme"));
  let sandbox = applyRecordingLayout(
    {
      ...original,
      workbench: {
        ...original.workbench,
        common: { ...original.workbench.common, ...(overrides.common ?? {}) },
        image: { ...original.workbench.image, ...(overrides.image ?? {}) },
        video: { ...original.workbench.video, ...(overrides.video ?? {}) },
        pointcloud: { ...original.workbench.pointcloud, ...(overrides.pointcloud ?? {}) },
        // Let the current workspace owner migrate these recording-only legacy
        // settings; saved Dockview contexts must not override the requested mode.
        layout: { ...original.workbench.layout, ...(overrides.layout ?? {}), workspace: undefined },
      },
      ui: { ...original.ui, ...(overrides.ui ?? {}) },
    },
    mode,
  );
  if (overrides.workspace) {
    const { context, preset } = overrides.workspace;
    const snapshot = createWorkspacePreset(preset, page.viewportSize() ?? undefined, context);
    if (overrides.rightColumnWidth) {
      widenRightColumn(snapshot, overrides.rightColumnWidth);
    }
    sandbox.workbench.layout.workspace = {
      engine: "dockview@8",
      contexts: {
        [context]: {
          schemaVersion: WORKSPACE_SCHEMA_VERSION,
          snapshot,
        },
      },
    };
  }
  if (screenshotTheme === "light" || screenshotTheme === "dark") {
    sandbox = {
      ...sandbox,
      ui: { ...sandbox.ui, theme: screenshotTheme },
    };
  }
  await page.route("**/api/v1/auth/me/preferences", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      await route.fulfill({ status: 200, json: sandbox });
      return;
    }
    if (method === "PATCH") {
      const patch = route.request().postDataJSON() as Partial<UserPreferences>;
      sandbox = overrides.workspace
        ? mergePreferences(sandbox, patch)
        : applyRecordingLayout(mergePreferences(sandbox, patch), mode);
      await route.fulfill({ status: 200, json: sandbox });
      return;
    }
    await route.continue();
  });

  // 工作台首帧优先读按账号分桶的 localStorage；导航前同步写入沙箱值，
  // 避免先显示真实账号布局、等 GET 回来后再跳到录制布局的闪动。
  await page.evaluate((preferences) => {
    const raw = localStorage.getItem("auth-storage");
    if (!raw) throw new Error("[recording-layout] localStorage 缺少 auth-storage");
    const auth = JSON.parse(raw) as {
      state?: {
        user?: {
          id?: string;
          preferences?: UserPreferences;
        };
      };
    };
    const user = auth.state?.user;
    if (!user?.id) throw new Error("[recording-layout] auth-storage 缺少 user.id");
    user.preferences = preferences;
    localStorage.setItem("auth-storage", JSON.stringify(auth));
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith(`workbench.${user.id}.workspace.`)) localStorage.removeItem(key);
    }
    for (const [context, envelope] of Object.entries(
      preferences.workbench.layout.workspace?.contexts ?? {},
    )) {
      localStorage.setItem(`workbench.${user.id}.workspace.${context}`, JSON.stringify(envelope));
    }
    localStorage.setItem(
      `workbench.${user.id}.leftOpen`,
      preferences.workbench.layout.leftOpen ? "1" : "0",
    );
    localStorage.setItem(
      `workbench.${user.id}.rightOpen`,
      preferences.workbench.layout.rightOpen ? "1" : "0",
    );
    localStorage.setItem(
      `workbench.${user.id}.floatingSelection`,
      JSON.stringify(preferences.workbench.layout.floatingSelection),
    );
  }, sandbox);
}

/** Verify the current Dockview panels and their actual recording geometry. */
export async function waitForRecordingWorkbenchLayout(
  page: Page,
  mode: RecordingSidebarMode,
): Promise<void> {
  await page.locator("[data-workbench-workspace]").waitFor({ state: "visible", timeout: 10_000 });
  await page.waitForFunction(
    (sidebarMode) => {
      const root = document.querySelector<HTMLElement>("[data-workbench-workspace]");
      const rect = (id: string) => {
        const panel = root?.querySelector<HTMLElement>(`[data-workbench-panel="${id}"]`);
        if (!panel || panel.getAttribute("aria-hidden") === "true") return null;
        const box = panel.getBoundingClientRect();
        return box.width > 0 && box.height > 0 ? box : null;
      };
      const workspace = root?.getBoundingClientRect();
      const canvas = rect("canvas");
      if (!workspace || !canvas) return false;
      const left = rect("task-queue");
      const right = rect("inspector");
      if (sidebarMode === "none") {
        return !left && !right && canvas.width >= workspace.width * 0.9;
      }
      return Boolean(
        left &&
        right &&
        left.width >= 180 &&
        left.width <= workspace.width * 0.25 &&
        right.width >= 180 &&
        right.width <= workspace.width * 0.25 &&
        left.right <= canvas.left + 2 &&
        right.left >= canvas.right - 2,
      );
    },
    mode,
    { timeout: 10_000 },
  );
}

/**
 * 把可拖动的当前题 AI 面板停到视口右侧安全边距内，避免录制时遮住中央主图。
 * 走真实 pointer drag，只影响隔离的 Playwright context，不改产品默认定位。
 */
export async function dockAiPanelAtViewportRight(page: Page, panel: Locator): Promise<void> {
  await panel.waitFor({ state: "visible", timeout: 5_000 });
  await panelCommand(page, "当前题 AI", "停靠到右侧");
  await panel.waitFor({ state: "visible", timeout: 5_000 });
}

/** The same user commands select presets during visible layout demonstrations. */
export const recordingLayoutCommand = layoutCommand;

export const recordingPanelCommand = panelCommand;

export async function waitForRecordingPanels(
  page: Page,
  visible: string[],
  hidden: string[] = [],
): Promise<void> {
  await expect(page.locator("[data-workbench-workspace]")).toBeVisible();
  for (const id of visible) {
    const panel = page.locator(`[data-workbench-panel="${id}"]`);
    await expect(panel).toHaveCount(1);
    await expect(panel).toHaveAttribute("aria-hidden", "false");
    await expect(panel).toBeVisible();
    const bounds = await panel.boundingBox();
    expect(bounds && bounds.width >= 100 && bounds.height >= 60).toBeTruthy();
  }
  for (const id of hidden) {
    await expect(page.locator(`[data-workbench-panel="${id}"]`)).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  }
}
