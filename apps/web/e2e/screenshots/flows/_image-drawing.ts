import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { commitPendingAnnotationClass, hidePredictions, openImageAnnotate } from "./_canvas";
import { recordingPanelCommand, waitForRecordingPanels } from "./_workbench-layout";

export interface ImageDrawingOptions {
  onCreated?: (id: string) => void;
}

export async function commitImageDrawing(
  page: Page,
  options: Parameters<typeof commitPendingAnnotationClass>[1],
) {
  await expect(page.getByTestId("class-picker-popover")).toBeVisible();
  // Keep class confirmation visible long enough to teach the save interaction.
  await page.waitForTimeout(1000);
  return commitPendingAnnotationClass(page, options);
}

export async function prepareImageDrawing(page: Page, catalog: ScreenshotSeedCatalog) {
  await openImageAnnotate(page, catalog);
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true");
  await waitForRecordingPanels(page, ["canvas", "task-queue", "class-palette", "inspector"]);
  await recordingPanelCommand(page, "讨论", "隐藏面板");
  await hidePredictions(page);
  await expect(stage).toHaveAttribute("data-ai-box-count", "0");
}

/** Read the saved geometry again, then check the same annotation after a real reload. */
export async function verifySavedImageDrawing(
  page: Page,
  taskId: string,
  annotationId: string,
  geometryTypes: string[],
) {
  const saved = await page.evaluate(
    async ({ taskId, annotationId }) => {
      const response = await fetch(`/api/v1/tasks/${taskId}/annotations`, {
        headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
      });
      if (!response.ok) throw new Error(`Saved drawing: HTTP ${response.status}`);
      const annotations = (await response.json()) as Array<{
        id: string;
        geometry: { type: string; angle?: number };
      }>;
      return annotations.find((annotation) => annotation.id === annotationId);
    },
    { taskId, annotationId },
  );
  expect(saved, "The drawn annotation must be persisted").toBeDefined();
  expect(geometryTypes).toContain(saved!.geometry.type);
  await page.reload();
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");
  await expect(page.getByTestId(`box-list-item-${annotationId}`)).toBeVisible();
  return saved!;
}
