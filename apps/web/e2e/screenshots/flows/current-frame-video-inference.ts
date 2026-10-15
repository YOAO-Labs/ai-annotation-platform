/**
 * 视频当前帧推理完整链路：定位目标帧 → 真实车辆检测 → 采纳 → 相邻帧核对。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import { dockAiPanelAtViewportRight, waitForRecordingWorkbenchLayout } from "./_workbench-layout";

export interface VideoFrameInferenceCleanupRecord {
  projectId: string;
  taskId: string;
  predictionId: string;
  annotationIds: string[];
}

async function seekFrameWithTimeline(page: Page, frameIndex: number): Promise<void> {
  const readout = await page.getByTestId("video-timeline-window-readout").first().textContent();
  const maxFrame = Number(readout?.match(/F0–(\d+)/)?.[1]);
  if (!Number.isInteger(maxFrame) || maxFrame <= 0 || frameIndex > maxFrame) {
    throw new Error(`[current-frame-video-inference] 无法从时间轴读取有效帧范围: ${readout ?? ""}`);
  }
  const slider = page.getByRole("slider", { name: "视频帧时间轴" }).first();
  await slider.fill(String(Math.round((frameIndex / maxFrame) * 10_000)));
  await expect(page.getByTestId("video-konva-stage")).toHaveAttribute(
    "data-video-frame-index",
    String(frameIndex),
    { timeout: 10_000 },
  );
}

export async function runCurrentFrameVideoInference(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onDispatched?: (record: VideoFrameInferenceCleanupRecord) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);

  const timeline = page.getByTestId("video-timeline-shell");
  const stage = page.getByTestId("video-konva-stage");
  await timeline.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-video-frame-index", "0", { timeout: 15_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await page.getByTestId("video-tool-btn-select").click();
  await page.waitForTimeout(1_200);

  const sourceFrame = 5;
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_500);
  await seekFrameWithTimeline(page, sourceFrame);
  await page.waitForTimeout(1_400);

  await page.getByTestId("workbench-ai-single").click();
  const panel = page.getByTestId("ai-prediction-popover");
  await panel.waitFor({ state: "visible", timeout: 5_000 });
  await dockAiPanelAtViewportRight(page, panel);

  await panel.getByTestId("ai-prediction-advanced-toggle").click();
  await expect(panel.getByTestId("ai-prediction-advanced-toggle")).toHaveAttribute(
    "aria-expanded",
    "true",
  );

  const backendSelect = panel.locator("label").filter({ hasText: "本次后端" }).locator("select");
  await backendSelect.waitFor({ state: "visible", timeout: 10_000 });
  const vehicleBackend = backendSelect.locator("option").filter({ hasText: "yolo-backend" });
  if ((await vehicleBackend.count()) !== 1) {
    throw new Error("[current-frame-video-inference] 视频项目未启用 yolo-backend");
  }
  const vehicleBackendId = await vehicleBackend.getAttribute("value");
  if (!vehicleBackendId) {
    throw new Error("[current-frame-video-inference] 专用车辆检测 backend 缺少 id");
  }
  await backendSelect.selectOption(vehicleBackendId);

  const modelSelect = panel.locator("label").filter({ hasText: "模型任务" }).locator("select");
  await modelSelect.waitFor({ state: "visible", timeout: 15_000 });
  await modelSelect.selectOption("detect");
  await expect(modelSelect).toHaveValue("detect");
  await panel.getByTitle("类别 [5] bus").click();
  await panel.getByTitle("类别 [7] truck").click();
  await expect(panel.getByText("已选 2", { exact: false })).toBeVisible();

  const runButton = panel.getByRole("button", { name: "运行当前题", exact: true });
  await expect(runButton).toBeEnabled({ timeout: 15_000 });
  await page.waitForTimeout(2_300);

  const responsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "POST" &&
        url.pathname ===
          `/api/v1/projects/${project.id}/ml-backends/${vehicleBackendId}/predict-frame`
      );
    },
    { timeout: 120_000 },
  );
  await runButton.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(
      `[current-frame-video-inference] 当前帧推理失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const body = (await response.json()) as {
    prediction_id?: string;
    candidate_count?: number;
    frame_index?: number;
  };
  if (!body.prediction_id || !body.candidate_count || body.candidate_count < 1) {
    throw new Error("[current-frame-video-inference] 专用车辆模型未返回可审阅候选");
  }
  if (body.frame_index !== sourceFrame) {
    throw new Error(
      `[current-frame-video-inference] 推理帧号错位: 期望 ${sourceFrame}，实际 ${String(body.frame_index)}`,
    );
  }
  const cleanupRecord: VideoFrameInferenceCleanupRecord = {
    projectId: project.id,
    taskId: task.id,
    predictionId: body.prediction_id,
    annotationIds: [],
  };
  onDispatched?.(cleanupRecord);

  await expect(panel.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "review", {
    timeout: 15_000,
  });
  const visibleCandidateCount = Number(
    (await panel.textContent())?.match(/(\d+)\s*个候选待审阅/)?.[1],
  );
  if (!Number.isInteger(visibleCandidateCount) || visibleCandidateCount < 1) {
    throw new Error("[current-frame-video-inference] 无法读取当前帧可见候选数");
  }
  await page.waitForTimeout(2_500);
  await panel.getByTitle("关闭当前题 AI").click();
  await panel.waitFor({ state: "hidden", timeout: 5_000 });

  const candidates = page.locator(`[data-testid^="box-list-item-pred-${body.prediction_id}-"]`);
  await candidates.first().waitFor({ state: "visible", timeout: 10_000 });
  const truckCandidate = candidates.filter({ hasText: /truck/i }).first();
  if ((await truckCandidate.count()) === 0) {
    const observed = await candidates.allTextContents();
    throw new Error(`[current-frame-video-inference] 未找到中间卡车候选: ${observed.join(" | ")}`);
  }
  await truckCandidate.scrollIntoViewIfNeeded();
  await truckCandidate.click();
  await expect(truckCandidate).toContainText(`F${sourceFrame}`);
  await expect(truckCandidate).toContainText(/\d+%/);
  await page.waitForTimeout(2_800);

  const accepted = page.waitForResponse(
    (candidateResponse) =>
      candidateResponse.request().method() === "POST" &&
      /\/predictions\/[^/]+\/accept(?:\?|$)/.test(candidateResponse.url()) &&
      candidateResponse.ok(),
    { timeout: 20_000 },
  );
  await page.getByTitle("采纳预测").last().click();
  const acceptedBody = (await (await accepted).json()) as Array<{ id?: string }>;
  cleanupRecord.annotationIds.push(
    ...acceptedBody.flatMap((annotation) =>
      typeof annotation.id === "string" ? [annotation.id] : [],
    ),
  );
  if (cleanupRecord.annotationIds.length === 0) {
    throw new Error("[current-frame-video-inference] 采纳响应缺少 annotation id");
  }

  const expectedAiCount = visibleCandidateCount - 1;
  const aiSection = page.getByTestId("section-header-ai");
  const manualSection = page.getByTestId("section-header-manual");
  await expect(aiSection).toContainText(String(expectedAiCount), { timeout: 10_000 });
  await expect(manualSection).toContainText("1", { timeout: 10_000 });
  const inspectorList = aiSection.locator("xpath=../../..");
  await inspectorList.evaluate((element) => element.scrollTo({ top: 0, behavior: "smooth" }));
  await page.waitForTimeout(2_500);

  await page.keyboard.press("ArrowRight");
  await expect(stage).toHaveAttribute("data-video-frame-index", String(sourceFrame + 1), {
    timeout: 10_000,
  });
  await expect(aiSection).toContainText("0", { timeout: 10_000 });
  await expect(manualSection).toContainText("0", { timeout: 10_000 });
  await page.waitForTimeout(2_300);

  await page.keyboard.press("ArrowLeft");
  await expect(stage).toHaveAttribute("data-video-frame-index", String(sourceFrame), {
    timeout: 10_000,
  });
  await expect(manualSection).toContainText("1", { timeout: 10_000 });
  await page.waitForTimeout(3_000);

  return { drawStartMs, drawEndMs: Date.now() };
}
