/**
 * 流程录制：真实 RapidOCR 当前题推理。
 *
 * 开启当前题 AI → 显式选择 ocr-e2e → 运行真实后端 → 等待多边形+文本候选出现。
 * 不采纳候选；持久化 prediction / job 由 flows.spec 按本次 id 精确清理，
 * 平台不可变审计记录按安全设计保留。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import { dockAiPanelAtViewportRight, waitForRecordingWorkbenchLayout } from "./_workbench-layout";

export interface OcrCleanupRecord {
  projectId: string;
  taskId: string;
  celeryTaskId: string;
  annotationIds: string[];
}

export interface OcrInferenceRecording extends DrawWindow, OcrCleanupRecord {}

export async function runOcrInference(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onDispatched?: (record: OcrCleanupRecord) => void,
): Promise<OcrInferenceRecording> {
  const project = catalog.projects.ocr_demo;
  const backend = project.ml_backend;
  const models = backend?.capabilities.models ?? [];
  const hasE2e = models.some((model) => model.id === "ocr-e2e");
  if (!backend?.name.toLowerCase().includes("rapidocr") || !hasE2e) {
    throw new Error("[ocr-inference] P-OCR 未绑定含 ocr-e2e 的真实 RapidOCR backend");
  }

  const task = project.tasks.ocr;
  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await page.waitForTimeout(1_000);
  const drawStartMs = Date.now();
  // 第一镜保留已完成加载的 OCR 原图，随后才进入模型配置，不把登录/骨架屏算进素材。
  await page.waitForTimeout(1_800);

  await page.getByTestId("workbench-ai-single").click();
  const panel = page.getByTestId("ai-prediction-popover");
  await panel.waitFor({ state: "visible", timeout: 5_000 });
  await dockAiPanelAtViewportRight(page, panel);
  await panel.getByTestId("ai-prediction-advanced-toggle").click();
  await expect(panel.getByTestId("ai-prediction-advanced")).toBeVisible();

  const modelField = panel.locator("label").filter({ hasText: "模型任务" });
  const modelSelect = modelField.locator("select");
  if (await modelSelect.count()) {
    await modelSelect.selectOption("ocr-e2e");
  } else if (
    !(await panel.getByText("当前模型").locator("..").textContent())?.includes("端到端 OCR")
  ) {
    throw new Error("[ocr-inference] 当前题 AI 未提供 ocr-e2e 模型");
  }
  // 第二镜让“RapidOCR / 端到端 OCR”的真实配置保持可读。
  await page.waitForTimeout(1_500);

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

  await panel.getByRole("button", { name: "运行当前题", exact: true }).click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`[ocr-inference] 推理派发失败: HTTP ${response.status()}`);
  }
  const body = (await response.json()) as { job_id?: string };
  if (!body.job_id) {
    throw new Error("[ocr-inference] 推理响应缺少 job_id，无法无痕清理");
  }
  const cleanupRecord = {
    projectId: project.id,
    taskId: task.id,
    celeryTaskId: body.job_id,
    annotationIds: [],
  };
  onDispatched?.(cleanupRecord);

  // 真正完成以 WS 触发的 predictions 重拉和非零待审数为准，
  // POST 200 只代表 Celery 已派发。RapidOCR 首次冷加载给足 120s。
  await page.waitForFunction(
    () => {
      const popover = document.querySelector('[data-testid="ai-prediction-popover"]');
      const stage = document.querySelector('[data-testid="workbench-stage"]');
      if (
        popover
          ?.querySelector('[data-testid="ai-prediction-phase"]')
          ?.getAttribute("data-phase") !== "review" ||
        Number(stage?.getAttribute("data-ai-box-count") ?? "0") < 1
      )
        return false;
      const run = popover.querySelector<HTMLButtonElement>(
        '[data-testid="ai-prediction-run-next"]',
      );
      return Boolean(run && !run.disabled);
    },
    undefined,
    { timeout: 120_000 },
  );
  // 第三镜先在 AI 面板核对识别文本和待审计数，再关闭面板展示完整文字框。
  await page.waitForTimeout(2_200);
  await panel.getByTitle("关闭当前题 AI").click();
  await panel.waitFor({ state: "hidden", timeout: 5_000 });
  await page.waitForTimeout(2_200);

  return {
    drawStartMs,
    drawEndMs: Date.now(),
    ...cleanupRecord,
  };
}
