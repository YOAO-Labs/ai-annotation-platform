import { expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { installScreenshotEnvironment } from "../environment";
import { recordingLayoutCommand, waitForRecordingPanels } from "./_workbench-layout";
import {
  WORKSPACE_SCHEMA_VERSION,
  type WorkspaceSnapshot,
} from "../../../src/pages/Workbench/layout/workbenchLayoutSnapshot";

type Preferences = {
  [key: string]: unknown;
  ui?: { theme?: string };
  workbench?: {
    layout?: {
      workspace?: {
        engine?: string;
        contexts?: Record<string, { schemaVersion?: number; snapshot?: unknown }>;
      };
    };
  };
};

type PreferenceWrite = {
  contextKeys: string[];
  schemaVersion?: number;
  method: "PATCH";
};

type SnapshotFacts = {
  grid: { width: number; height: number; orientation: string; root: unknown };
  groups: unknown[];
  floatingGroups: unknown[];
  returns: unknown;
  visibilityIntent: unknown;
  cameraPresentation: unknown;
};

async function token(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[workspace-layout-persistence] 缺少登录令牌");
  return value;
}

async function userId(page: Page): Promise<string> {
  const value = await page.evaluate(() => {
    const raw = localStorage.getItem("auth-storage");
    if (!raw) return null;
    const user = (JSON.parse(raw) as { state?: { user?: { id?: string } } }).state?.user;
    return user?.id ?? null;
  });
  if (!value) throw new Error("[workspace-layout-persistence] 缺少用户 ID");
  return value;
}

async function readPreferences(page: Page): Promise<Preferences> {
  const response = await page.request.get("/api/v1/auth/me/preferences", {
    headers: { Authorization: `Bearer ${await token(page)}` },
  });
  if (!response.ok()) {
    throw new Error(
      `[workspace-layout-persistence] 读取偏好失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as Preferences;
}

function savedContext(
  preferences: Preferences,
  context: string,
): { schemaVersion?: number; snapshot?: WorkspaceSnapshot } {
  return preferences.workbench?.layout?.workspace?.contexts?.[context] ?? {};
}

function snapshotFacts(snapshot: WorkspaceSnapshot | undefined): SnapshotFacts {
  if (!snapshot) throw new Error("[workspace-layout-persistence] 偏好缺少布局快照");
  const grid = snapshot.layout.grid;
  const groups: unknown[] = [];
  const walk = (node: WorkspaceSnapshot["layout"]["grid"]["root"]) => {
    if (node.type === "leaf") {
      groups.push({
        type: node.type,
        id: node.data.id,
        views: node.data.views,
        activeView: node.data.activeView,
        visible: node.visible,
        size: node.size,
      });
      return;
    }
    groups.push({ type: node.type, visible: node.visible, size: node.size });
    node.data.forEach(walk);
  };
  walk(grid.root);
  const floatingGroups = (snapshot.layout.floatingGroups ?? []).map((group) => ({
    id: group.data.id,
    views: group.data.views,
    activeView: group.data.activeView,
    position: group.position,
  }));
  return {
    grid: {
      width: grid.width,
      height: grid.height,
      orientation: grid.orientation,
      root: grid.root,
    },
    groups,
    floatingGroups,
    returns: snapshot.returns,
    visibilityIntent: snapshot.visibilityIntent,
    cameraPresentation: snapshot.cameraPresentation,
  };
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

async function waitForDesktop(page: Page, context: string): Promise<void> {
  await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute("data-compact", "false");
  await expect(page.getByRole("button", { name: "布局", exact: true })).toBeEnabled({
    timeout: 20_000,
  });
  await waitForRecordingPanels(page, ["canvas"]);
  const url = new URL(page.url());
  expect(
    url.searchParams.get("task"),
    `[workspace-layout-persistence] ${context} task`,
  ).toBeTruthy();
}

async function waitForImage(page: Page): Promise<void> {
  await page.getByTestId("workbench-stage").waitFor({ state: "visible", timeout: 20_000 });
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
    timeout: 20_000,
  });
}

async function waitForVideo(page: Page): Promise<void> {
  await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 20_000 });
  await page.getByTestId("video-konva-stage").waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForFunction(() => {
    const video = document.querySelector("video");
    return video instanceof HTMLVideoElement && video.readyState >= 2 && video.videoWidth > 0;
  });
}

/**
 * Exercise the server-owned Dockview envelope through a real PATCH/GET roundtrip.
 * The portable recorder keeps setup/navigation outside the returned content window.
 */
export async function runWorkspaceLayoutPersistence(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{
  drawStartMs: number;
  drawEndMs: number;
  evidence: {
    patchWrites: PreferenceWrite[];
    imageSchemaVersion: number;
    videoSchemaVersion: number;
    compactWritesBefore: number;
    compactWritesAfter: number;
    restoredImageSchemaVersion: number;
  };
}> {
  const imageTask = catalog.projects.image_demo.tasks.annotating;
  const videoTask = catalog.projects.video_demo.tasks.tracking;
  const patchWrites: PreferenceWrite[] = [];
  const failedPreferenceResponses: string[] = [];
  let originalPreferences: Preferences | null = null;
  let originalUserId: string | null = null;
  let result:
    | {
        drawStartMs: number;
        drawEndMs: number;
        evidence: {
          patchWrites: PreferenceWrite[];
          imageSchemaVersion: number;
          videoSchemaVersion: number;
          compactWritesBefore: number;
          compactWritesAfter: number;
          restoredImageSchemaVersion: number;
        };
      }
    | undefined;
  let actionError: unknown;
  let cleanupError: unknown;
  const onRequest = (request: import("@playwright/test").Request) => {
    const url = new URL(request.url());
    if (request.method() !== "PATCH" || url.pathname !== "/api/v1/auth/me/preferences") return;
    const payload = request.postDataJSON() as Preferences;
    const contexts = payload.workbench?.layout?.workspace?.contexts ?? {};
    const contextKeys = Object.keys(contexts);
    if (contextKeys.length === 0) return;
    const [context] = contextKeys;
    patchWrites.push({
      method: "PATCH",
      contextKeys,
      schemaVersion: context ? contexts[context]?.schemaVersion : undefined,
    });
  };
  const onResponse = (response: import("@playwright/test").Response) => {
    const url = new URL(response.url());
    if (url.pathname === "/api/v1/auth/me/preferences" && response.status() >= 400) {
      failedPreferenceResponses.push(`${response.status()} ${url.pathname}`);
    }
  };
  page.on("request", onRequest);
  page.on("response", onResponse);
  try {
    await installScreenshotEnvironment(page, { clock: "live" });
    await page.setViewportSize({ width: 1440, height: 810 });
    originalUserId = await userId(page);
    originalPreferences = await readPreferences(page);
    await page.goto(`/projects/${catalog.projects.image_demo.id}/annotate?task=${imageTask.id}`);
    await waitForImage(page);
    await waitForDesktop(page, "annotate:image");
    const dayThemeToggle = page.getByRole("button", { name: "当前日间，切到夜间" });
    if (await dayThemeToggle.count()) {
      const themePatch = page.waitForResponse(
        (response) =>
          response.request().method() === "PATCH" &&
          new URL(response.url()).pathname === "/api/v1/auth/me/preferences" &&
          response.ok(),
        { timeout: 15_000 },
      );
      await dayThemeToggle.click();
      await themePatch;
    }
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect
      .poll(async () => (await readPreferences(page)).ui?.theme, { timeout: 15_000 })
      .toBe("dark");

    const drawStartMs = Date.now();
    await page.waitForTimeout(900);
    await recordingLayoutCommand(page, "专注画布布局");
    await waitForRecordingPanels(
      page,
      ["canvas"],
      ["task-queue", "class-palette", "inspector", "discussion"],
    );
    await page.waitForTimeout(1_800);
    await expect
      .poll(
        async () =>
          (await readPreferences(page)).workbench?.layout?.workspace?.contexts?.["annotate:image"]
            ?.schemaVersion,
      )
      .toBe(WORKSPACE_SCHEMA_VERSION);
    await recordingLayoutCommand(page, "恢复画布布局");
    await waitForRecordingPanels(page, [
      "canvas",
      "task-queue",
      "class-palette",
      "inspector",
      "discussion",
    ]);
    await page.waitForTimeout(3_000); // Read the image layout before switching to video.

    await page.goto(`/projects/${catalog.projects.video_demo.id}/annotate?task=${videoTask.id}`);
    await waitForVideo(page);
    await waitForDesktop(page, "annotate:video");
    await page.waitForTimeout(850);
    await recordingLayoutCommand(page, "视频追踪布局");
    // The video preset keeps task-queue and video-tracker as the visible tab
    // in their Dockview groups; class-palette and inspector are alternate
    // views and therefore do not render as independent panel elements.
    await waitForRecordingPanels(page, ["canvas", "task-queue", "video-tracker"]);
    await page.waitForTimeout(3_000); // Read the distinct persisted video layout.
    const afterVideo = await readPreferences(page);
    const imageContext = savedContext(afterVideo, "annotate:image");
    const videoContext = savedContext(afterVideo, "annotate:video");
    expect(imageContext.schemaVersion).toBe(WORKSPACE_SCHEMA_VERSION);
    expect(videoContext.schemaVersion).toBe(WORKSPACE_SCHEMA_VERSION);
    const imageFacts = snapshotFacts(imageContext.snapshot);
    const videoFacts = snapshotFacts(videoContext.snapshot);
    expect(imageFacts.groups).not.toEqual(videoFacts.groups);

    await page.goto(`/projects/${catalog.projects.image_demo.id}/annotate?task=${imageTask.id}`);
    await waitForImage(page);
    await waitForDesktop(page, "annotate:image return");
    await expect(page.locator('[data-workbench-panel="task-queue"]')).toHaveAttribute(
      "aria-hidden",
      "false",
    );
    await page.reload();
    await waitForImage(page);
    await waitForDesktop(page, "annotate:image reload");
    const afterReload = await readPreferences(page);
    const imageAfterReload = savedContext(afterReload, "annotate:image");
    const videoAfterReload = savedContext(afterReload, "annotate:video");
    expect(imageAfterReload.schemaVersion).toBe(WORKSPACE_SCHEMA_VERSION);
    expect(videoAfterReload.schemaVersion).toBe(WORKSPACE_SCHEMA_VERSION);
    expect(snapshotFacts(imageAfterReload.snapshot)).toEqual(imageFacts);
    expect(snapshotFacts(videoAfterReload.snapshot)).toEqual(videoFacts);

    const compactWritesBefore = patchWrites.length;
    const beforeCompact = await readPreferences(page);
    await page.setViewportSize({ width: 1024, height: 810 });
    await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute(
      "data-compact",
      "true",
    );
    await page.waitForTimeout(1_400);
    await page.setViewportSize({ width: 1440, height: 810 });
    await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute(
      "data-compact",
      "false",
    );
    await waitForImage(page);
    await expect(page.locator('[data-workbench-panel="task-queue"]')).toHaveAttribute(
      "aria-hidden",
      "false",
    );
    await page.waitForTimeout(1_700);
    const compactWritesAfter = patchWrites.length;
    expect(compactWritesAfter).toBe(compactWritesBefore);
    expect(failedPreferenceResponses).toEqual([]);
    const restored = await readPreferences(page);
    expect(savedContext(restored, "annotate:image").schemaVersion).toBe(WORKSPACE_SCHEMA_VERSION);
    expect(savedContext(restored, "annotate:video").schemaVersion).toBe(WORKSPACE_SCHEMA_VERSION);
    expect(snapshotFacts(savedContext(restored, "annotate:image").snapshot)).toEqual(
      snapshotFacts(savedContext(beforeCompact, "annotate:image").snapshot),
    );
    await page.waitForTimeout(3_500); // Show the restored desktop layout after the compact view.
    result = {
      drawStartMs,
      drawEndMs: Date.now(),
      evidence: {
        patchWrites,
        imageSchemaVersion: imageContext.schemaVersion!,
        videoSchemaVersion: videoContext.schemaVersion!,
        compactWritesBefore,
        compactWritesAfter,
        restoredImageSchemaVersion: savedContext(restored, "annotate:image").schemaVersion!,
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    if (originalPreferences && originalUserId) {
      try {
        // Leave the workbench before touching the row. This prevents a
        // debounced layout writer from racing the cleanup after the test.
        await page.goto("/", { waitUntil: "domcontentloaded", timeout: 20_000 });
        await page.waitForTimeout(500);
        // Named preset revisions are read-only through the public PATCH API.
        // Restore the exact isolated seed snapshot through its existing owner guard.
        writeStoredPreferences(originalUserId, originalPreferences);
        const restored = await readPreferences(page);
        if (
          stableJson(restored.workbench?.layout) !==
          stableJson(originalPreferences.workbench?.layout)
        ) {
          cleanupError = new Error(
            "[workspace-layout-persistence] 恢复后 GET 的 workbench.layout 与原始偏好不一致",
          );
        }
      } catch (error) {
        try {
          writeStoredPreferences(originalUserId, originalPreferences);
        } catch (restoreError) {
          cleanupError = new Error(
            `[workspace-layout-persistence] 恢复 API 失败且隔离数据库回退也失败: ${String(
              restoreError,
            )}`,
          );
        }
        cleanupError ??= error;
      }
    }
    page.off("request", onRequest);
    page.off("response", onResponse);
  }
  if (actionError) throw actionError;
  if (cleanupError) throw cleanupError;
  if (!result) throw new Error("[workspace-layout-persistence] 未生成验收结果");
  return result;
}
