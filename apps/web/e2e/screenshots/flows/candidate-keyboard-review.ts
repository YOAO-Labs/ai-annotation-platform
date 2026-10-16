/**
 * 流程录制：Tab 进入 AI 待审候选，A/D 决策后自动推进到下一项。
 */
import { createHash } from "node:crypto";
import type { CandidateReviewCleanupRecord } from "./candidate-review-lifecycle";
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";

export interface CandidateReviewWindow extends DrawWindow {
  autoAdvanceStartMs: number;
}

interface LivePrediction {
  id: string;
  ml_backend_id: string | null;
  model_version: string | null;
  source: string | null;
  result: Array<{
    type: string;
    shape_index: number;
    confidence: number;
    class_name: string;
    geometry: { points?: number[][]; polygons?: Array<{ points: number[][] }> };
  }>;
}

/** Generate review candidates through the production API and worker, without fixture geometry. */
export async function prepareLiveCandidateReview(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  token: string,
  cleanup: CandidateReviewCleanupRecord,
) {
  const project = catalog.projects.image_demo;
  const backend = project.ml_backend;
  if (!backend?.capabilities.models?.some((model) => model.id === "sam3-segmentation")) {
    throw new Error("[candidate-keyboard-review] 需要真实 SAM3 文本分割模型");
  }
  const api = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  const headers = { Authorization: `Bearer ${token}` };
  const request = {
    ml_backend_id: backend.id,
    task_ids: [cleanup.taskId],
    execution_scope: "workbench" as const,
    model_id: "sam3-segmentation",
    prompt: "car",
    output_mode: "mask",
    predict_mode: "overwrite",
    params: { score_threshold: 0.95, simplify_tolerance: 1.0 },
  };
  const dispatched = await page.request.post(`${api}/api/v1/projects/${project.id}/preannotate`, {
    headers,
    data: request,
  });
  expect(dispatched.ok(), await dispatched.text()).toBeTruthy();
  const job = (await dispatched.json()) as { job_id: string };
  expect(job.job_id).toBeTruthy();
  cleanup.celeryTaskId = job.job_id;
  let predictions: LivePrediction[] = [];
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `${api}/api/v1/tasks/${cleanup.taskId}/predictions`,
          { headers },
        );
        expect(response.ok()).toBeTruthy();
        predictions = (await response.json()) as LivePrediction[];
        cleanup.predictionIds = predictions.map((prediction) => prediction.id);
        return predictions.flatMap((prediction) => prediction.result).length;
      },
      { timeout: 180_000, intervals: [1_000, 2_000] },
    )
    .toBeGreaterThanOrEqual(3);
  for (const prediction of predictions) {
    expect(prediction.ml_backend_id).toBe(backend.id);
    expect(prediction.source).toBe("ml_backend");
    expect(prediction.model_version).toBeTruthy();
    for (const shape of prediction.result) {
      expect(["polygonlabels", "multipolygonlabels"]).toContain(shape.type);
      expect(shape.class_name).toBe("car");
      expect(Number.isInteger(shape.shape_index)).toBeTruthy();
      expect(shape.confidence).toBeGreaterThan(0);
      expect(shape.confidence).toBeLessThanOrEqual(1);
    }
  }
  return {
    candidateIds: predictions.flatMap((prediction) =>
      prediction.result.map((shape) => `${prediction.id}-${shape.shape_index}`),
    ),
    evidence: {
      endpoint: "POST /api/v1/projects/{project_id}/preannotate",
      job_id: job.job_id,
      request,
      predictions,
      result_sha256: createHash("sha256").update(JSON.stringify(predictions)).digest("hex"),
    },
  };
}

async function waitForSelectedCandidate(
  page: Page,
  candidateIds: string[],
  exclude?: string,
): Promise<string> {
  const testIds = candidateIds.map((id) => `box-list-item-pred-${id}`);
  await page.waitForFunction(
    ({ ids, excluded }) =>
      ids.some((testId) => {
        if (testId === excluded) return false;
        const row = document.querySelector(`[data-testid="${testId}"]`);
        return row?.className.includes("border-brand") ?? false;
      }),
    { ids: testIds, excluded: exclude ? `box-list-item-pred-${exclude}` : null },
  );
  return await page.evaluate(
    ({ ids, excluded }) => {
      const selected = ids.find((testId) => {
        if (testId === excluded) return false;
        const row = document.querySelector(`[data-testid="${testId}"]`);
        return row?.className.includes("border-brand") ?? false;
      });
      if (!selected) throw new Error("未找到选中的 AI 候选");
      return selected.replace(/^box-list-item-pred-/, "");
    },
    { ids: testIds, excluded: exclude ? `box-list-item-pred-${exclude}` : null },
  );
}

export async function runCandidateKeyboardReview(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  candidateIds: string[],
  cleanup: CandidateReviewCleanupRecord,
): Promise<CandidateReviewWindow> {
  if (candidateIds.length < 3) {
    throw new Error("[candidate-keyboard-review] 至少需要 3 条候选");
  }
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  const rows = candidateIds.map((id) => page.getByTestId(`box-list-item-pred-${id}`));

  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 10_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 10_000 });
  await rows[0].waitFor({ state: "visible", timeout: 10_000 });
  const section = page.getByTestId("section-header-ai");
  if ((await section.getAttribute("aria-expanded")) === "false") {
    await section.click();
    await rows[0].waitFor({ state: "visible", timeout: 5_000 });
  }
  await page.getByTestId("tool-btn-select").click();
  await rows[0].click();
  await waitForSelectedCandidate(page, [candidateIds[0]]);
  // Keep the pointer off inspector rows while demonstrating keyboard review.
  await page.mouse.move(0, 0);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.waitForTimeout(1_000);

  const drawStartMs = Date.now();
  await page.waitForTimeout(1_500);
  await page.keyboard.press("Tab");
  const firstDecisionId = await waitForSelectedCandidate(page, candidateIds, candidateIds[0]);
  await page.waitForTimeout(1_300);

  async function decide(key: "A" | "D", candidateId: string) {
    const split = candidateId.lastIndexOf("-");
    const predictionId = candidateId.slice(0, split);
    const index = candidateId.slice(split + 1);
    const action = key === "A" ? "accept" : "reject";
    const pending = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "POST" &&
        url.pathname.endsWith(`/predictions/${predictionId}/${action}`) &&
        url.searchParams.get("shape_index") === index
      );
    });
    await page.keyboard.press(key);
    const response = await pending;
    expect(response.ok()).toBeTruthy();
    if (key === "A") {
      const annotations = (await response.json()) as Array<{ id: string }>;
      expect(annotations).toHaveLength(1);
      cleanup.annotationIds.push(annotations[0].id);
    }
  }
  const autoAdvanceStartMs = Date.now();
  await decide("A", firstDecisionId);
  await page
    .getByTestId(`box-list-item-pred-${firstDecisionId}`)
    .waitFor({ state: "hidden", timeout: 10_000 });
  const remainingAfterAccept = candidateIds.filter((id) => id !== firstDecisionId);
  const secondDecisionId = await waitForSelectedCandidate(page, remainingAfterAccept);
  await page.waitForTimeout(1_500);

  await decide("D", secondDecisionId);
  await page
    .getByTestId(`box-list-item-pred-${secondDecisionId}`)
    .waitFor({ state: "hidden", timeout: 10_000 });
  const finalDecisionId = await waitForSelectedCandidate(
    page,
    remainingAfterAccept.filter((id) => id !== secondDecisionId),
  );
  await page.getByTestId(`box-list-item-pred-${finalDecisionId}`).waitFor({ state: "visible" });
  await page.waitForTimeout(1_500);
  await decide("A", finalDecisionId);
  await page
    .getByTestId(`box-list-item-pred-${finalDecisionId}`)
    .waitFor({ state: "hidden", timeout: 10_000 });
  const manualHeader = page.getByTestId("section-header-manual");
  await manualHeader.scrollIntoViewIfNeeded();
  await page.waitForFunction(() => {
    const text = document.querySelector('[data-testid="section-header-manual"]')?.textContent ?? "";
    return /[1-9]\d*/.test(text);
  });
  await expect(stage).toHaveAttribute("data-user-box-count", "2");
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateIds.length - 3));
  await page.waitForTimeout(3_200);
  await page.reload();
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "2");
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateIds.length - 3));
  // Keep the verified saved review state readable after the real reload.
  await page.waitForTimeout(1_200);
  const drawEndMs = Date.now();
  return { drawStartMs, autoAdvanceStartMs, drawEndMs };
}
