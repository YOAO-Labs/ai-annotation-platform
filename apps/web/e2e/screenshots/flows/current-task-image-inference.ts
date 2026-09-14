/**
 * 当前题图片推理完整链路：已保存项目编排 → 真实 OCR → 候选审阅 → 单项采纳。
 */
import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import type { OcrCleanupRecord } from "./ocr-inference";
import { recordingPanelCommand, waitForRecordingPanels } from "./_workbench-layout";

export async function runCurrentTaskImageInference(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onDispatched?: (record: OcrCleanupRecord) => void,
  onEvidence?: (evidence: unknown) => void,
): Promise<DrawWindow> {
  const invalidCommentRequests: string[] = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (/\/annotations\/pred-[^/]+\/(?:comments|history)/.test(pathname)) {
      invalidCommentRequests.push(pathname);
    }
  });
  const project = catalog.projects.ocr_demo;
  const backend = project.ml_backend;
  const hasE2e = (backend?.capabilities.models ?? []).some((model) => model.id === "ocr-e2e");
  if (!backend?.name.toLowerCase().includes("rapidocr") || !hasE2e) {
    throw new Error(
      "[current-task-image-inference] P-OCR 未绑定含 ocr-e2e 的真实 RapidOCR backend",
    );
  }

  const task = project.tasks.ocr;
  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "0", { timeout: 10_000 });
  await waitForRecordingPanels(page, ["canvas", "task-queue", "ai-task"]);
  await recordingPanelCommand(page, "讨论", "隐藏面板");
  await page.waitForTimeout(1_000);

  const drawStartMs = Date.now();
  await page.waitForTimeout(2_400);

  const panel = page.getByTestId("ai-prediction-popover");
  await panel.waitFor({ state: "visible", timeout: 5_000 });
  const pipelineButton = panel.getByRole("button", {
    name: "运行当前题（按项目编排 · 1 阶段）",
    exact: true,
  });
  await pipelineButton.waitFor({ state: "visible", timeout: 10_000 });
  await expect(pipelineButton).toBeEnabled();
  await page.waitForTimeout(2_300);

  const responsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "POST" &&
        url.pathname === `/api/v1/projects/${project.id}/preannotate`
      );
    },
    { timeout: 15_000 },
  );
  await pipelineButton.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`[current-task-image-inference] 推理派发失败: HTTP ${response.status()}`);
  }
  const body = (await response.json()) as { job_id?: string };
  if (!body.job_id) {
    throw new Error("[current-task-image-inference] 推理响应缺少 job_id，无法无痕清理");
  }
  const cleanupRecord: OcrCleanupRecord = {
    projectId: project.id,
    taskId: task.id,
    celeryTaskId: body.job_id,
    annotationIds: [],
  };
  onDispatched?.(cleanupRecord);

  await page.waitForFunction(
    () => {
      const popover = document.querySelector('[data-testid="ai-prediction-popover"]');
      if (!popover || !/[1-9]\d*\s*待审/.test(popover.textContent ?? "")) return false;
      return Array.from(popover.querySelectorAll("button")).some(
        (button) => button.textContent?.includes("运行当前题") && !button.disabled,
      );
    },
    undefined,
    { timeout: 120_000 },
  );
  const candidateCount = Number(await stage.getAttribute("data-ai-box-count"));
  if (!Number.isInteger(candidateCount) || candidateCount < 1) {
    throw new Error("[current-task-image-inference] 真实编排未生成可审阅候选");
  }
  await page.waitForTimeout(3_000);

  const predictions = await page.evaluate(async (taskId) => {
    const response = await fetch(`/api/v1/tasks/${taskId}/predictions`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok) throw new Error(`Prediction evidence: HTTP ${response.status}`);
    return response.json() as Promise<
      Array<{
        id: string;
        ml_backend_id: string;
        source: string;
        model_version: string;
        result: Array<{ shape_index: number; confidence: number; attributes?: { text?: string } }>;
      }>
    >;
  }, task.id);
  expect(predictions.length).toBeGreaterThan(0);
  for (const prediction of predictions) {
    expect(prediction.source).toBe("ml_backend");
    expect(prediction.ml_backend_id).toBe(backend.id);
    expect(prediction.model_version).toBeTruthy();
  }
  onEvidence?.({
    job_id: body.job_id,
    model_id: "ocr-e2e",
    predictions,
    result_sha256: createHash("sha256").update(JSON.stringify(predictions)).digest("hex"),
  });
  await page.getByRole("tab", { name: "标注详情", exact: true }).click();
  await waitForRecordingPanels(page, ["canvas", "inspector"], ["ai-task"]);
  const target = predictions
    .flatMap((prediction) =>
      prediction.result.map((shape) => ({
        predictionId: prediction.id,
        ...shape,
      })),
    )
    .find((shape) => shape.attributes?.text?.replace(/\s/g, "").includes("大桶装"));
  expect(target, "Real OCR should recognize the prominent product heading").toBeDefined();
  const candidate = page.getByTestId(
    `box-list-item-pred-${target!.predictionId}-${target!.shape_index}`,
  );
  await candidate.waitFor({ state: "visible", timeout: 10_000 });
  await candidate.scrollIntoViewIfNeeded();
  await candidate.click();
  await expect(candidate).toContainText(/\d+%/);
  await page.waitForTimeout(2_800);

  const candidateId = (await candidate.getAttribute("data-testid"))!.replace(
    "box-list-item-pred-",
    "",
  );
  const split = candidateId.lastIndexOf("-");
  const predictionId = candidateId.slice(0, split);
  const shapeIndex = Number(candidateId.slice(split + 1));
  expect(
    predictions
      .find((prediction) => prediction.id === predictionId)
      ?.result.some((shape) => shape.shape_index === shapeIndex),
  ).toBeTruthy();
  const accepted = page.waitForResponse(
    (candidateResponse) =>
      candidateResponse.request().method() === "POST" &&
      new URL(candidateResponse.url()).pathname.endsWith(`/predictions/${predictionId}/accept`) &&
      new URL(candidateResponse.url()).searchParams.get("shape_index") === String(shapeIndex) &&
      candidateResponse.ok(),
    { timeout: 20_000 },
  );
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("A");
  const acceptedResponse = await accepted;
  const acceptedBody = (await acceptedResponse.json()) as Array<{ id?: string }>;
  cleanupRecord.annotationIds.push(
    ...acceptedBody.flatMap((annotation) =>
      typeof annotation.id === "string" ? [annotation.id] : [],
    ),
  );
  if (cleanupRecord.annotationIds.length === 0) {
    throw new Error("[current-task-image-inference] 采纳响应缺少 annotation id，无法无痕清理");
  }

  await expect(stage).toHaveAttribute("data-user-box-count", "1", { timeout: 10_000 });
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateCount - 1), {
    timeout: 10_000,
  });
  const manualSection = page.getByTestId("section-header-manual");
  await manualSection.scrollIntoViewIfNeeded();
  await manualSection.waitFor({ state: "visible", timeout: 10_000 });
  await expect(manualSection).toContainText("1");
  await page.waitForTimeout(4_000);

  const drawEndMs = Date.now();
  await page.reload();
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateCount - 1));
  expect(invalidCommentRequests).toEqual([]);
  return { drawStartMs, drawEndMs };
}
