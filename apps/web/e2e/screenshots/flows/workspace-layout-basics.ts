/**
 * Teach layout commands while preserving the live image canvas and discussion draft.
 * Preferences are sandboxed by the caller; this story makes no reload/persistence claim.
 * The caller registers the created annotation immediately and deletes it in finally.
 */
import { expect, type Page, type Request } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  commitPendingAnnotationClass,
  hidePredictions,
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
  type RecordingWorkbenchOverrides,
} from "./_workbench-layout";

export const workspaceLayoutBasicsLayout: RecordingWorkbenchOverrides = {
  workspace: { context: "annotate:image", preset: "standard" },
  layout: { discussionCollapsed: false, manualSectionCollapsed: false },
};

export async function runWorkspaceLayoutBasics(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (annotation: { taskId: string; annotationId: string }) => void,
): Promise<{ drawStartMs: number; drawEndMs: number }> {
  const task = catalog.projects.image_demo.tasks.annotating;
  const panel = (id: string) => page.locator(`[data-workbench-panel="${id}"]`);
  const discussion = panel("discussion");
  const inspector = panel("inspector");
  const stage = page.getByTestId("workbench-stage");
  await openImageAnnotate(page, catalog);
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await waitForRecordingPanels(page, [
    "canvas",
    "task-queue",
    "class-palette",
    "inspector",
    "discussion",
  ]);
  await hidePredictions(page);

  // Prepare a real, selected annotation before the visible layout story begins.
  await page.getByTestId("tool-btn-box").click();
  const anchor = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");
  const { start, end } = mediaBbox(await renderedMediaBounds(stage), anchor.bbox);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, start, end, 650);
  await page.mouse.up();
  const created = await commitPendingAnnotationClass(page, {
    label: anchor.label,
    taskId: task.id,
    onCreated: (annotationId) => onCreated({ taskId: task.id, annotationId }),
  });
  expect(typeof created.id, "The selected annotation must have a persisted ID").toBe("string");
  expect(created.geometry).toMatchObject({ type: "bbox" });
  const selected = page.getByTestId(`box-list-item-${created.id}`);
  await page.getByTestId("tool-btn-select").click();
  await selected.click();
  await expect(selected).toHaveClass(/border-brand/);
  const selectionCardCollapse = page.getByRole("button", { name: "收起浮窗", exact: true });
  if (await selectionCardCollapse.isVisible()) await selectionCardCollapse.click();
  await discussion.getByRole("tab", { name: "评论", exact: true }).click();
  const editor = discussion.locator('[contenteditable="true"]');
  await expect(editor).toBeVisible();
  const draft = "请复核这辆车的边界，布局调整后继续讨论。";
  await editor.fill(draft);

  const canvas = panel("canvas");
  const media = stage.locator(".konvajs-content > canvas").first();
  await expect(media).toBeVisible();
  const originals = await Promise.all(
    [canvas, stage, media, editor].map((locator) => locator.elementHandle()),
  );
  const imageIdentity = await stage.getAttribute("data-image-identity");
  const userBoxCount = await stage.getAttribute("data-user-box-count");
  expect(imageIdentity).toBeTruthy();
  const sameState = async () => {
    expect(new URL(page.url()).searchParams.get("task")).toBe(task.id);
    await expect(canvas).toHaveCount(1);
    await expect(stage).toBeVisible();
    await expect(stage).toHaveAttribute("data-image-identity", imageIdentity!);
    await expect(stage).toHaveAttribute("data-user-box-count", userBoxCount!);
    await expect(stage).toHaveAttribute("data-pending-drawing", "false");
    await expect(page.getByTestId("tool-btn-select")).toHaveAttribute("aria-pressed", "true");
    await expect(selected).toHaveClass(/border-brand/);
    await expect(editor).toHaveText(draft);
    for (const [index, locator] of [canvas, stage, media, editor].entries()) {
      expect(await locator.evaluate((node, original) => node === original, originals[index])).toBe(
        true,
      );
    }
  };
  const commentWrites: string[] = [];
  const observeCommentWrite = (request: Request) => {
    if (request.method() === "POST" && /\/comments(?:\/|$)/.test(new URL(request.url()).pathname)) {
      commentWrites.push(request.url());
    }
  };
  page.on("request", observeCommentWrite);
  try {
    const drawStartMs = Date.now();
    await page.waitForTimeout(1000);
    await recordingLayoutCommand(page, "标准标注布局");
    await sameState();
    await page.waitForTimeout(1400);

    // Find the real vertical sash from current panel geometry, then widen the right column.
    const oldInspector = await inspector.boundingBox();
    if (!oldInspector) throw new Error("[workspace-layout-basics] Inspector has no bounds");
    const sash = await page
      .locator("[data-workbench-workspace] .dv-sash")
      .evaluateAll((elements, boundary) => {
        const rect = elements
          .map((element) => element.getBoundingClientRect())
          .find(
            (candidate) =>
              candidate.height > candidate.width &&
              candidate.width > 0 &&
              Math.abs(candidate.left + candidate.width / 2 - boundary) < 5,
          );
        return rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null;
      }, oldInspector.x);
    if (!sash) throw new Error("[workspace-layout-basics] Inspector divider is missing");
    await page.mouse.move(sash.x, sash.y);
    await page.waitForTimeout(450);
    await page.mouse.down();
    await movePointerAtRefreshRate(page, sash, { x: sash.x - 80, y: sash.y }, 800);
    await page.mouse.up();
    await expect
      .poll(async () => (await inspector.boundingBox())?.width)
      .toBeGreaterThan(oldInspector.width + 60);
    await sameState();
    await page.waitForTimeout(1500);

    await recordingPanelCommand(page, "讨论", "与标注详情合并为标签");
    const discussionTab = page.getByRole("tab").filter({
      has: page.getByRole("button", { name: "隐藏讨论", exact: true }),
    });
    const inspectorTab = page.getByRole("tab").filter({
      has: page.getByRole("button", { name: "隐藏标注详情", exact: true }),
    });
    await expect(discussionTab).toHaveCount(1);
    await expect(inspectorTab).toHaveCount(1);
    await waitForRecordingPanels(page, ["discussion"], ["inspector"]);
    await sameState();
    await page.waitForTimeout(1400);
    await inspectorTab.click();
    await waitForRecordingPanels(page, ["inspector"], ["discussion"]);
    await sameState();
    await page.waitForTimeout(1100);
    await discussionTab.click();
    await waitForRecordingPanels(page, ["discussion"], ["inspector"]);
    await sameState();
    await page.waitForTimeout(1300);

    // The title-bar X hides only the active discussion tab; inspector remains available.
    await page.getByRole("button", { name: "隐藏讨论", exact: true }).click();
    await waitForRecordingPanels(page, ["inspector"], ["discussion"]);
    await sameState();
    await page.waitForTimeout(1200);
    await recordingLayoutCommand(page, "讨论");
    await waitForRecordingPanels(page, ["discussion"], ["inspector"]);
    await sameState();
    await page.waitForTimeout(1700);

    const restoredWidth = (await canvas.boundingBox())!.width;
    await recordingLayoutCommand(page, "专注画布布局");
    await waitForRecordingPanels(
      page,
      ["canvas"],
      ["task-queue", "class-palette", "inspector", "discussion"],
    );
    await expect
      .poll(async () => (await canvas.boundingBox())?.width)
      .toBeGreaterThan(restoredWidth + 100);
    await sameState();
    await page.waitForTimeout(1800);
    await recordingLayoutCommand(page, "恢复画布");
    await waitForRecordingPanels(
      page,
      ["canvas", "task-queue", "class-palette", "discussion"],
      ["inspector"],
    );
    await expect
      .poll(async () => Math.abs((await canvas.boundingBox())!.width - restoredWidth))
      .toBeLessThanOrEqual(2);
    await sameState();
    await expect(editor).toBeVisible();
    expect(commentWrites, "The discussion must remain an unsent draft").toEqual([]);
    await page.waitForTimeout(2200);
    return { drawStartMs, drawEndMs: Date.now() };
  } finally {
    page.off("request", observeCommentWrite);
    await Promise.all(originals.map((handle) => handle?.dispose()));
  }
}
