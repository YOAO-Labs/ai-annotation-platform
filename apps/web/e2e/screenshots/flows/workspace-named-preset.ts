/**
 * 流程录制：命名布局预设「边界精修」的保存、套用与撤销，以及刷新后的账号持久化。
 *
 * 输出：营销母版 workbench/named-preset（见 apps/web/scripts/derive-doc-media.mjs）
 *
 * 与 workspace-layout-persistence 一致，本流程不使用偏好沙箱：先在一个已保存且选中的对象上做出
 * 自定义面板排列，通过布局快捷菜单把它另存为「边界精修」预设（真实 PATCH），再切到标准标注布局、
 * 套回该预设，最后用提示条里的「撤销」回到上一步排列，全程保持同一任务与同一选中对象。
 * 刷新后重新读回账号偏好并打开布局菜单，证明预设真实落库。流程结束把该账号的原始偏好整份写回。
 *
 * 返回 { drawStartMs, drawEndMs } 供 finalize 裁剪；evidence 记录真实偏好读写结果。
 */
import { expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { installScreenshotEnvironment } from "../environment";
import {
  committedAnnotationFromPayload,
  hidePredictions,
  isAnnotationCommitRequest,
  mediaBbox,
  movePointerAtRefreshRate,
  openImageAnnotate,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import {
  recordingLayoutCommand,
  recordingPanelCommand,
  waitForRecordingPanels,
} from "./_workbench-layout";

const PRESET_NAME = "边界精修";

type Preferences = {
  [key: string]: unknown;
  ui?: { theme?: string };
  workbench?: {
    layout?: {
      workspace?: {
        engine?: string;
        contexts?: Record<string, { schemaVersion?: number; snapshot?: unknown }>;
        namedPresets?: Record<string, { name?: string; context?: string; snapshot?: unknown }>;
      };
    };
  };
};

type NamedPresetWrite = {
  contextKeys: string[];
  namedPresetNames: string[];
  method: "PATCH";
};

export interface WorkspaceNamedPresetEvidence {
  presetName: string;
  presetId: string;
  namedPresetWrites: NamedPresetWrite[];
  /** 保存后从真实 GET 读到的预设名称。 */
  storedPresetNamesAfterSave: string[];
  /** 刷新后从真实 GET 读到的预设名称。 */
  storedPresetNamesAfterReload: string[];
  /** 刷新后同一任务仍在、选中对象仍被同一账号偏好包围。 */
  reloadTask: string;
  panelsAfterCustom: string[];
  panelsAfterStandard: string[];
  panelsAfterApply: string[];
  panelsAfterUndo: string[];
  selectionPreserved: boolean;
  annotationId: string;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

async function token(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[workspace-named-preset] 缺少登录令牌");
  return value;
}

async function userId(page: Page): Promise<string> {
  const value = await page.evaluate(() => {
    const raw = localStorage.getItem("auth-storage");
    if (!raw) return null;
    const user = (JSON.parse(raw) as { state?: { user?: { id?: string } } }).state?.user;
    return user?.id ?? null;
  });
  if (!value) throw new Error("[workspace-named-preset] 缺少用户 ID");
  return value;
}

async function readPreferences(page: Page): Promise<Preferences> {
  const response = await page.request.get("/api/v1/auth/me/preferences", {
    headers: { Authorization: `Bearer ${await token(page)}` },
  });
  if (!response.ok()) {
    throw new Error(
      `[workspace-named-preset] 读取偏好失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as Preferences;
}

function storedPresetNames(preferences: Preferences): string[] {
  return Object.entries(preferences.workbench?.layout?.workspace?.namedPresets ?? {})
    .map(([id, preset]) => ({ id, name: preset?.name ?? "" }))
    .filter((entry) => entry.name.length > 0)
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right, "zh-Hans-CN"));
}

function writeStoredPreferences(user: string, preferences: unknown): void {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../../..");
  const temporaryDirectory = mkdtempSync(path.join(tmpdir(), "aap-screenshot-preferences-"));
  const preferencesFile = path.join(temporaryDirectory, "preferences.json");
  writeFileSync(preferencesFile, JSON.stringify(preferences), "utf-8");
  try {
    execFileSync(
      path.join(root, "apps/api/.venv/bin/python"),
      [
        path.join(root, "apps/api/scripts/restore_screenshot_user_preferences.py"),
        "--user-id",
        user,
        "--preferences-file",
        preferencesFile,
      ],
      {
        cwd: path.join(root, "apps/api"),
        env: process.env,
        stdio: "pipe",
      },
    );
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
}

function clearStoredPreferences(user: string): void {
  writeStoredPreferences(user, {});
}

async function waitForImage(page: Page): Promise<void> {
  await page.getByTestId("workbench-stage").waitFor({ state: "visible", timeout: 20_000 });
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
    timeout: 20_000,
  });
}

async function waitForDesktop(page: Page): Promise<void> {
  await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute("data-compact", "false");
  await expect(page.getByRole("button", { name: "布局", exact: true })).toBeEnabled({
    timeout: 20_000,
  });
}

/** 当前真实可见的面板集合，用于记录每次排列切换后的结果。 */
async function visiblePanels(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll("[data-workbench-panel]"))
      .filter((panel) => panel.getAttribute("aria-hidden") !== "true")
      .map((panel) => panel.getAttribute("data-workbench-panel") ?? "")
      .sort(),
  );
}

async function openQuickMenu(page: Page) {
  await page.getByRole("button", { name: "布局", exact: true }).click();
  const menu = page.getByRole("dialog", { name: "布局快捷设置", exact: true });
  await expect(menu).toBeVisible({ timeout: 10_000 });
  return menu;
}

/**
 * 用真实界面画一个已保存的矩形并选中它。
 *
 * 本流程不使用偏好沙箱，因此 afterBoxCreate 取自账号真实偏好：弹层出现时人工选 car，
 * 否则直接等真实提交响应（例如账号偏好是复用当前类别）。
 */
async function createSelectedBbox(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ id: string; taskId: string }> {
  const task = catalog.projects.image_demo.tasks.annotating;
  const anchor = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");
  const stage = page.getByTestId("workbench-stage");
  await page.getByTestId("tool-btn-box").click();
  const { start, end } = mediaBbox(await renderedMediaBounds(stage), anchor.bbox);
  const committed = page.waitForResponse(
    (response) =>
      isAnnotationCommitRequest(response.request().method(), response.url()) && response.ok(),
    { timeout: 20_000 },
  );
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, start, end, 650);
  await page.mouse.up();
  const picker = page.getByTestId("class-picker-popover");
  const pickerVisible = await picker
    .waitFor({ state: "visible", timeout: 1_500 })
    .then(() => true)
    .catch(() => false);
  if (pickerVisible) {
    await picker.getByText(anchor.label, { exact: true }).last().click();
  }
  const payload = committedAnnotationFromPayload(
    (await (await committed).json()) as Record<string, unknown>,
  );
  await expect(stage).toHaveAttribute("data-pending-drawing", "false");
  const id = payload.id;
  if (typeof id !== "string") throw new Error("[workspace-named-preset] 画框未返回已保存 ID");
  return { id, taskId: task.id };
}

/**
 * 标准布局把任务队列与类别面板放回两个停靠组时无需处理；预设恢复「合并为标签」
 * 的排列时，active tab 是最后拖入的任务队列，会把 onlyWhenVisible 的类别面板从
 * DOM 卸载。点一次「类别面板」标签页保证两个面板同时可读，再由调用方断言。
 */
async function activatePaletteTab(page: import("@playwright/test").Page): Promise<void> {
  const paletteTab = page.getByRole("tab", { name: "类别面板", exact: true });
  if (await paletteTab.isVisible()) await paletteTab.click();
}

export async function runWorkspaceNamedPreset(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ drawStartMs: number; drawEndMs: number; evidence: WorkspaceNamedPresetEvidence }> {
  const imageTask = catalog.projects.image_demo.tasks.annotating;
  const namedPresetWrites: NamedPresetWrite[] = [];
  let originalPreferences: Preferences | null = null;
  let originalUserId: string | null = null;
  let result:
    | { drawStartMs: number; drawEndMs: number; evidence: WorkspaceNamedPresetEvidence }
    | undefined;
  let actionError: unknown;
  let cleanupError: unknown;
  const onRequest = (request: import("@playwright/test").Request) => {
    const url = new URL(request.url());
    if (request.method() !== "PATCH" || url.pathname !== "/api/v1/auth/me/preferences") return;
    const payload = request.postDataJSON() as Preferences;
    const workspace = payload.workbench?.layout?.workspace;
    const namedPresets = workspace?.namedPresets;
    if (!workspace || !namedPresets) return;
    namedPresetWrites.push({
      method: "PATCH",
      contextKeys: Object.keys(workspace.contexts ?? {}),
      namedPresetNames: Object.values(namedPresets)
        .map((preset) => preset?.name ?? "")
        .sort((left, right) => left.localeCompare(right, "zh-Hans-CN")),
    });
  };
  page.on("request", onRequest);
  try {
    await installScreenshotEnvironment(page, { clock: "live" });
    await page.setViewportSize({ width: 1440, height: 810 });
    originalUserId = await userId(page);
    originalPreferences = await readPreferences(page);
    await openImageAnnotate(page, catalog);
    await waitForImage(page);
    await waitForDesktop(page);
    // 起手排列走真实界面命令，不预写偏好。
    await recordingLayoutCommand(page, "标准标注布局");
    await waitForRecordingPanels(page, [
      "canvas",
      "task-queue",
      "class-palette",
      "inspector",
      "discussion",
    ]);
    await hidePredictions(page);

    // 准备（不进素材）：一个真实保存并被选中的对象。
    const created = await createSelectedBbox(page, catalog);
    await page.getByTestId("tool-btn-select").click();
    const selectedItem = page.getByTestId(`box-list-item-${created.id}`);
    await expect(selectedItem).toBeVisible({ timeout: 10_000 });
    // 提交后应用会自动选中新对象并展开浮动信息卡；只有未选中时才点列表项。
    if (!((await selectedItem.getAttribute("class")) ?? "").includes("border-brand")) {
      await selectedItem.click();
      await expect(selectedItem).toHaveClass(/border-brand/);
    }
    // 展开的浮动信息卡（z-popover-elevated）会盖住后续所有列表点击：
    // 与 workspace-layout-basics 一致，先收起浮窗再继续。
    const selectionCardCollapse = page.getByRole("button", { name: "收起浮窗", exact: true });
    if (await selectionCardCollapse.isVisible()) await selectionCardCollapse.click();
    await page.waitForTimeout(800);

    const drawStartMs = Date.now();

    // ── 自定义面板排列：任务队列并入类别面板标签，讨论面板隐藏 ──
    await page.waitForTimeout(1_600);
    await recordingPanelCommand(page, "任务队列", "与类别面板合并为标签");
    // task-queue/class-palette 是 onlyWhenVisible renderer：合并后 active tab 是
    // 「任务队列」，非 active 的 class-palette 会从 DOM 卸载（count 0）而非隐藏。
    // 先点回「类别面板」标签让两个面板都在 DOM 中且可读；真实窗口下首次点击
    // 偶发被合并动画吞掉，重试一次。
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const paletteTab = page.getByRole("tab", { name: "类别面板", exact: true });
      if (await paletteTab.isVisible()) await paletteTab.click();
      await page.waitForTimeout(400);
      if ((await page.locator('[data-workbench-panel="task-queue"]').count()) === 0) break;
    }
    await expect(page.locator('[data-workbench-panel="class-palette"]')).toBeVisible();
    await page.waitForTimeout(1_300);
    await recordingPanelCommand(page, "讨论", "隐藏面板");
    await expect(page.locator('[data-workbench-panel="discussion"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await expect(selectedItem).toHaveClass(/border-brand/);
    const panelsAfterCustom = await visiblePanels(page);
    await page.waitForTimeout(1_200);

    // ── 把当前排列另存为「边界精修」（真实账号写入）──
    let menu = await openQuickMenu(page);
    const nameInput = menu.getByLabel("快速保存预设名称");
    const savePreset = menu.getByRole("button", {
      name: "保存当前布局为预设",
      exact: true,
    });
    await expect(nameInput).toBeEnabled({ timeout: 15_000 });
    await nameInput.click();
    for (const character of PRESET_NAME) {
      await nameInput.type(character);
      await page.waitForTimeout(180);
    }
    const presetPatch = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        new URL(response.url()).pathname === "/api/v1/auth/me/preferences" &&
        response.ok(),
      { timeout: 20_000 },
    );
    await expect(savePreset).toBeEnabled({ timeout: 10_000 });
    await savePreset.click();
    await presetPatch;
    await expect(
      page
        .locator("[data-sonner-toast]")
        .filter({ hasText: `已保存布局预设「${PRESET_NAME}」` })
        .first(),
    ).toBeVisible({ timeout: 10_000 });
    const afterSave = await readPreferences(page);
    const storedPresetNamesAfterSave = storedPresetNames(afterSave);
    expect(storedPresetNamesAfterSave, "预设必须真实写入账号偏好").toContain(PRESET_NAME);
    const presetId = Object.entries(
      afterSave.workbench?.layout?.workspace?.namedPresets ?? {},
    ).find(([, preset]) => preset?.name === PRESET_NAME)?.[0];
    expect(presetId, "保存后必须能读回预设 ID").toBeTruthy();
    await expect(menu).toBeHidden({ timeout: 10_000 });
    await page.waitForTimeout(2_000);

    // ── 切到标准标注布局，再套回「边界精修」──
    await recordingLayoutCommand(page, "标准标注布局");
    await activatePaletteTab(page);
    await waitForRecordingPanels(page, [
      "canvas",
      "task-queue",
      "class-palette",
      "inspector",
      "discussion",
    ]);
    await expect(selectedItem).toHaveClass(/border-brand/);
    const panelsAfterStandard = await visiblePanels(page);
    await page.waitForTimeout(1_400);

    await page.waitForTimeout(600);
    menu = await openQuickMenu(page);
    const applyPreset = menu.getByRole("button", {
      name: `应用预设 ${PRESET_NAME}`,
      exact: true,
    });
    await expect(applyPreset).toBeEnabled({ timeout: 15_000 });
    await applyPreset.click();
    await activatePaletteTab(page);
    await expect(page.locator('[data-workbench-panel="task-queue"]')).toHaveCount(0);
    await expect(page.locator('[data-workbench-panel="discussion"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await expect(page.locator('[data-workbench-panel="class-palette"]')).toBeVisible();
    await expect(selectedItem).toHaveClass(/border-brand/);
    const panelsAfterApply = await visiblePanels(page);
    await page.waitForTimeout(2_200);

    // ── 撤销这次应用：回到上一步排列，仍保持同一任务与选中对象 ──
    const undoToast = page
      .locator("[data-sonner-toast][data-removed='false']")
      .filter({ hasText: `已应用「${PRESET_NAME}」布局` })
      .first();
    await undoToast.getByRole("button", { name: "撤销", exact: true }).click();
    await activatePaletteTab(page);
    await waitForRecordingPanels(page, [
      "canvas",
      "task-queue",
      "class-palette",
      "inspector",
      "discussion",
    ]);
    await expect(selectedItem).toHaveClass(/border-brand/);
    const panelsAfterUndo = await visiblePanels(page);
    expect(panelsAfterUndo).toEqual(panelsAfterStandard);
    await page.waitForTimeout(700);
    const selectionPreserved = ((await selectedItem.getAttribute("class")) ?? "").includes(
      "border-brand",
    );
    expect(selectionPreserved, "撤销后必须仍是同一个选中对象").toBe(true);
    await page.waitForTimeout(1_300);

    // ── 刷新后读回真实偏好并重新打开布局菜单：预设仍在账号里 ──
    // 刷新前先回标准布局：撤销后的自定义排列含隐藏/合并面板，刷新恢复
    // 浮动信息卡时偶发把画布挤出视口；标准排列与 persistence 流程的刷新路径一致。
    await recordingLayoutCommand(page, "标准标注布局");
    await activatePaletteTab(page);
    await waitForRecordingPanels(page, [
      "canvas",
      "task-queue",
      "class-palette",
      "inspector",
      "discussion",
    ]);
    await page.reload();
    await waitForImage(page);
    await waitForDesktop(page);
    const reloadTask = new URL(page.url()).searchParams.get("task");
    expect(reloadTask, "刷新后仍在同一任务").toBe(imageTask.id);
    const afterReload = await readPreferences(page);
    const storedPresetNamesAfterReload = storedPresetNames(afterReload);
    expect(storedPresetNamesAfterReload, "刷新后预设必须仍随账号保存").toContain(PRESET_NAME);
    menu = await openQuickMenu(page);
    await expect(
      menu.getByRole("button", { name: `应用预设 ${PRESET_NAME}`, exact: true }),
    ).toBeVisible({ timeout: 10_000 });
    await expect(menu).toContainText(`${storedPresetNamesAfterReload.length} / 5`);
    await page.waitForTimeout(1_600);

    const drawEndMs = Date.now();
    result = {
      drawStartMs,
      drawEndMs,
      evidence: {
        presetName: PRESET_NAME,
        presetId: presetId!,
        namedPresetWrites,
        storedPresetNamesAfterSave,
        storedPresetNamesAfterReload,
        reloadTask: reloadTask!,
        panelsAfterCustom,
        panelsAfterStandard,
        panelsAfterApply,
        panelsAfterUndo,
        selectionPreserved,
        annotationId: created.id,
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    if (originalPreferences && originalUserId) {
      try {
        // 离开工作台后再改行，避免防抖布局写回与清理竞争。
        await page.goto("/", { waitUntil: "domcontentloaded", timeout: 20_000 });
        await page.waitForTimeout(500);
        // 公开 PATCH 只做深合并，没有删除操作；先清空隔离行，再写入原始文档。
        clearStoredPreferences(originalUserId);
        // namedPresetsRevision 是引擎管理的只读键：PATCH 不带 namedPresets 时携带它
        // 会被 422 (named_presets_revision_read_only) 拒绝，服务端在带 namedPresets 时
        // 也会自行丢弃该键。恢复写回时一律剔除，revision 由服务端按存储行重建。
        const restorePayload = { ...originalPreferences };
        delete restorePayload.namedPresetsRevision;
        const response = await page.request.patch("/api/v1/auth/me/preferences", {
          headers: { Authorization: `Bearer ${await token(page)}` },
          data: restorePayload,
        });
        if (!response.ok()) {
          writeStoredPreferences(originalUserId, originalPreferences);
          cleanupError = new Error(
            `[workspace-named-preset] 恢复原始布局偏好失败: HTTP ${response.status()} ${await response.text()}`,
          );
        } else {
          const restored = await readPreferences(page);
          if (
            stableJson(restored.workbench?.layout?.workspace?.namedPresets) !==
            stableJson(originalPreferences.workbench?.layout?.workspace?.namedPresets)
          ) {
            writeStoredPreferences(originalUserId, originalPreferences);
            cleanupError = new Error(
              "[workspace-named-preset] 恢复后 GET 的命名预设与原始偏好不一致",
            );
          }
        }
      } catch (error) {
        try {
          writeStoredPreferences(originalUserId, originalPreferences);
        } catch (restoreError) {
          cleanupError = new Error(
            `[workspace-named-preset] 恢复 API 失败且隔离数据库回退也失败: ${String(restoreError)}`,
          );
        }
        cleanupError ??= error;
      }
    }
    page.off("request", onRequest);
  }
  if (actionError) throw actionError;
  if (cleanupError) throw cleanupError;
  if (!result) throw new Error("[workspace-named-preset] 未生成验收结果");
  return result;
}
