/** Review real model shapes without replacing their prediction or shape identities. */
import { expect, type Page } from "@playwright/test";
import type { AnnotationResponse, PredictionResponse } from "../../../src/types";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import { recordingPanelCommand, waitForRecordingPanels } from "./_workbench-layout";

export interface CandidateReviewCleanupRecord {
  projectId: string;
  taskId: string;
  predictionIds: string[];
  celeryTaskId?: string;
  annotationIds: string[];
}

function candidateIdentity(candidateId: string) {
  const split = candidateId.lastIndexOf("-");
  const predictionId = candidateId.slice(0, split);
  const shapeIndex = Number(candidateId.slice(split + 1));
  if (
    split < 1 ||
    !/^\d+$/.test(candidateId.slice(split + 1)) ||
    !Number.isSafeInteger(shapeIndex)
  ) {
    throw new Error(`[candidate-review-lifecycle] Invalid candidate identity: ${candidateId}`);
  }
  return { predictionId, shapeIndex };
}

async function selectedCandidateId(page: Page, candidateIds: string[]): Promise<string> {
  const testIds = candidateIds.map((id) => `box-list-item-pred-${id}`);
  await page.waitForFunction(
    (ids) =>
      ids.some((testId) => {
        const row = document.querySelector(`[data-testid="${testId}"]`);
        return row?.className.includes("border-brand") ?? false;
      }),
    testIds,
  );
  return page.evaluate((ids) => {
    const selected = ids.find((testId) => {
      const row = document.querySelector(`[data-testid="${testId}"]`);
      return row?.className.includes("border-brand") ?? false;
    });
    if (!selected) throw new Error("未找到选中的 AI 候选");
    return selected.replace(/^box-list-item-pred-/, "");
  }, testIds);
}

/** The caller prepares candidates with prepareLiveCandidateReview and installs the ai-review preset. */
export async function runCandidateReviewLifecycle(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  candidateIds: string[],
  cleanup: CandidateReviewCleanupRecord,
) {
  if (candidateIds.length < 3 || new Set(candidateIds).size !== candidateIds.length) {
    throw new Error("[candidate-review-lifecycle] 至少需要 3 个不同的真实模型候选");
  }
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  if (cleanup.projectId !== project.id || cleanup.taskId !== task.id) {
    throw new Error("[candidate-review-lifecycle] 清理范围与截图 catalog 不一致");
  }
  for (const id of candidateIds) {
    expect(cleanup.predictionIds).toContain(candidateIdentity(id).predictionId);
  }

  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateIds.length), {
    timeout: 15_000,
  });
  await expect(stage).toHaveAttribute("data-user-box-count", "0");
  await waitForRecordingPanels(page, ["canvas", "task-queue", "ai-task"]);
  if (
    (await page.locator('[data-workbench-panel="discussion"]').getAttribute("aria-hidden")) !==
    "true"
  ) {
    await recordingPanelCommand(page, "讨论", "隐藏面板");
  }
  await page
    .getByRole("tab")
    .filter({ has: page.getByRole("button", { name: "隐藏标注详情", exact: true }) })
    .click();
  await waitForRecordingPanels(page, ["canvas", "inspector"], ["ai-task", "discussion"]);

  const section = page.getByTestId("section-header-ai");
  if ((await section.getAttribute("aria-expanded")) === "false") await section.click();
  await page.getByTestId("tool-btn-select").click();
  const allowed = new Set(candidateIds);
  // Read the displayed order, allowing the inspector to virtualize additional candidates.
  await expect
    .poll(async () => page.locator('[data-testid^="box-list-item-pred-"]').count())
    .toBeGreaterThanOrEqual(3);
  const orderedIds = (
    await page
      .locator('[data-testid^="box-list-item-pred-"]')
      .evaluateAll((rows) =>
        rows.map((row) =>
          (row.getAttribute("data-testid") ?? "").replace(/^box-list-item-pred-/, ""),
        ),
      )
  ).filter((id) => allowed.has(id));
  expect(orderedIds.length).toBeGreaterThanOrEqual(3);
  const row = (id: string) => page.getByTestId(`box-list-item-pred-${id}`);

  const token = await page.evaluate(() => localStorage.getItem("token"));
  if (!token) throw new Error("[candidate-review-lifecycle] 缺少登录凭据");
  const api = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  const headers = { Authorization: `Bearer ${token}` };
  async function readState() {
    const responses = await Promise.all([
      page.request.get(`${api}/api/v1/tasks/${task.id}/predictions`, { headers }),
      page.request.get(`${api}/api/v1/tasks/${task.id}/annotations`, { headers }),
    ]);
    for (const response of responses) expect(response.ok(), await response.text()).toBeTruthy();
    const predictions = (await responses[0].json()) as PredictionResponse[];
    const annotations = (await responses[1].json()) as AnnotationResponse[];
    const visiblePredictionIds = predictions.flatMap((prediction) =>
      prediction.result.map((shape) => {
        expect(Number.isInteger(shape.shape_index)).toBeTruthy();
        return `${prediction.id}-${shape.shape_index}`;
      }),
    );
    // Accepted shapes remain in Prediction; annotation lineage removes them from the pending set.
    const acceptedIds = new Set(
      annotations
        .filter((annotation) => annotation.parent_prediction_id)
        .map((annotation) => {
          const shapeIndex = annotation.attributes?._shape_index;
          expect(Number.isInteger(shapeIndex)).toBeTruthy();
          return `${annotation.parent_prediction_id}-${shapeIndex}`;
        }),
    );
    return {
      predictions,
      annotations,
      visiblePredictionIds,
      pendingIds: visiblePredictionIds.filter((id) => !acceptedIds.has(id)),
    };
  }
  const initial = await readState();
  expect(initial.pendingIds.slice().sort()).toEqual(candidateIds.slice().sort());
  expect(initial.annotations).toHaveLength(0);

  await page.waitForTimeout(1_000);
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_500);
  const skippedId = orderedIds[0];
  await row(skippedId).click();
  await expect(row(skippedId)).toHaveClass(/border-brand/);
  await page.mouse.move(0, 0);
  await page.waitForTimeout(2_200);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Tab");
  const acceptedId = await selectedCandidateId(
    page,
    candidateIds.filter((id) => id !== skippedId),
  );
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateIds.length));
  const afterSkip = await readState();
  expect(afterSkip.pendingIds.slice().sort()).toEqual(candidateIds.slice().sort());
  expect(afterSkip.annotations).toHaveLength(0);
  await page.waitForTimeout(2_200);

  async function decide(action: "accept" | "reject", candidateId: string) {
    const { predictionId, shapeIndex } = candidateIdentity(candidateId);
    const pending = page.waitForResponse(
      (response) => {
        const url = new URL(response.url());
        return (
          response.request().method() === "POST" &&
          url.pathname === `/api/v1/tasks/${task.id}/predictions/${predictionId}/${action}` &&
          url.searchParams.get("shape_index") === String(shapeIndex)
        );
      },
      { timeout: 20_000 },
    );
    await page.keyboard.press(action === "accept" ? "A" : "D");
    const response = await pending;
    expect(response.ok(), `${action} HTTP ${response.status()}`).toBeTruthy();
    if (action === "reject") {
      expect(response.status()).toBe(204);
      return;
    }
    const annotations = (await response.json()) as AnnotationResponse[];
    cleanup.annotationIds.push(
      ...annotations.flatMap((annotation) =>
        typeof annotation.id === "string" ? [annotation.id] : [],
      ),
    );
    expect(annotations).toHaveLength(1);
    expect(annotations[0]).toMatchObject({
      id: expect.any(String),
      task_id: task.id,
      source: "prediction_based",
      parent_prediction_id: predictionId,
      attributes: { _shape_index: shapeIndex },
    });
    const original = initial.predictions
      .find((prediction) => prediction.id === predictionId)!
      .result.find((shape) => shape.shape_index === shapeIndex)!;
    expect(annotations[0].geometry).toEqual(original.geometry);
    expect(annotations[0].class_name).toBe(original.class_name);
    return annotations[0];
  }
  const acceptedAnnotation = (await decide("accept", acceptedId))!;
  await row(acceptedId).waitFor({ state: "hidden", timeout: 10_000 });
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateIds.length - 1));
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  const rejectedId = await selectedCandidateId(
    page,
    candidateIds.filter((id) => id !== acceptedId),
  );
  expect(rejectedId, "采纳后应自动前进到另一条未决候选").not.toBe(skippedId);
  await page.waitForTimeout(2_200);

  await decide("reject", rejectedId);
  await row(rejectedId).waitFor({ state: "hidden", timeout: 10_000 });
  const remainingIds = candidateIds.filter((id) => id !== acceptedId && id !== rejectedId);
  const finalSelectedId = await selectedCandidateId(page, remainingIds);
  await expect(stage).toHaveAttribute("data-ai-box-count", String(remainingIds.length));
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await page.waitForTimeout(2_200);

  const final = await readState();
  expect(final.visiblePredictionIds.slice().sort()).toEqual(
    candidateIds.filter((id) => id !== rejectedId).sort(),
  );
  expect(final.pendingIds.slice().sort()).toEqual(remainingIds.slice().sort());
  expect(final.pendingIds).toContain(skippedId);
  expect(final.annotations.map((annotation) => annotation.id)).toEqual([acceptedAnnotation.id]);
  expect(final.annotations[0]).toMatchObject({
    parent_prediction_id: acceptedAnnotation.parent_prediction_id,
    attributes: { _shape_index: candidateIdentity(acceptedId).shapeIndex },
    geometry: acceptedAnnotation.geometry,
  });
  await page.waitForTimeout(3_200);
  const drawEndMs = Date.now();

  // Reload is outside the published clip, and proves decisions survive a fresh workbench mount.
  await page.reload();
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-ai-box-count", String(remainingIds.length));
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  const reloaded = await readState();
  expect(reloaded.pendingIds.slice().sort()).toEqual(remainingIds.slice().sort());
  expect(reloaded.annotations.map((annotation) => annotation.id)).toEqual([acceptedAnnotation.id]);

  return {
    ...({ drawStartMs, drawEndMs } satisfies DrawWindow),
    reviewEvidence: {
      initial_candidate_ids: candidateIds,
      skipped_candidate_id: skippedId,
      accepted_candidate_id: acceptedId,
      rejected_candidate_id: rejectedId,
      final_selected_candidate_id: finalSelectedId,
      remaining_candidate_ids: final.pendingIds,
      visible_prediction_candidate_ids: final.visiblePredictionIds,
      accepted_annotation: final.annotations[0],
      reload_verified: true,
    },
  };
}
