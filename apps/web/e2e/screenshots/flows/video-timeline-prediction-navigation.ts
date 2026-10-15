/**
 * 流程录制：预先为多个视频帧运行真实车辆检测，再沿预测密度轨逐帧核对候选。
 */
import { expect, type Locator, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { VideoFrameInferenceCleanupRecord } from "./current-frame-video-inference";
import type { DrawWindow } from "./rotated-bbox";

const PREDICTION_FRAMES = [0, 6, 12, 18, 24] as const;

async function seekFrame(page: Page, frameIndex: number): Promise<void> {
  const slider = page.getByRole("slider", { name: "视频帧时间轴" }).first();
  await slider.fill(String(Math.round((frameIndex / 71) * 10_000)));
  await expect(page.getByTestId("video-konva-stage")).toHaveAttribute(
    "data-video-frame-index",
    String(frameIndex),
    { timeout: 10_000 },
  );
}

async function selectVehicleModel(panel: Locator, configureClasses: boolean): Promise<string> {
  const advanced = panel.getByTestId("ai-prediction-advanced-toggle");
  if ((await advanced.getAttribute("aria-expanded")) !== "true") await advanced.click();
  await expect(advanced).toHaveAttribute("aria-expanded", "true");
  const backendSelect = panel
    .locator("label")
    .filter({ hasText: /(?:本次|下一轮)后端/ })
    .locator("select");
  await backendSelect.waitFor({ state: "visible", timeout: 10_000 });
  const yolo = backendSelect.locator("option").filter({ hasText: "yolo-backend" });
  if ((await yolo.count()) !== 1) {
    throw new Error("[video-prediction-navigation] 视频项目未启用 yolo-backend");
  }
  const backendId = await yolo.getAttribute("value");
  if (!backendId) throw new Error("[video-prediction-navigation] yolo-backend 缺少 id");
  await backendSelect.selectOption(backendId);

  const modelSelect = panel.locator("label").filter({ hasText: "模型任务" }).locator("select");
  await modelSelect.waitFor({ state: "visible", timeout: 15_000 });
  await modelSelect.selectOption("detect");
  await expect(modelSelect).toHaveValue("detect");
  if (configureClasses) {
    await panel.getByTitle("类别 [5] bus").click();
    await panel.getByTitle("类别 [7] truck").click();
  }
  await expect(panel.getByText("已选 2", { exact: false })).toBeVisible();
  return backendId;
}

async function runFramePrediction(
  page: Page,
  panel: Locator,
  projectId: string,
  backendId: string,
  frameIndex: number,
): Promise<{ predictionId: string; candidateCount: number }> {
  const responsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "POST" &&
        url.pathname === `/api/v1/projects/${projectId}/ml-backends/${backendId}/predict-frame`
      );
    },
    { timeout: 120_000 },
  );
  const phase = await panel.getByTestId("ai-prediction-phase").getAttribute("data-phase");
  const run =
    phase === "idle"
      ? panel.getByTestId("ai-prediction-primary-action")
      : panel.getByTestId("ai-prediction-run-next");
  await expect(run).toBeEnabled({ timeout: 15_000 });
  await run.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(
      `[video-prediction-navigation] F${frameIndex} 推理失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const body = (await response.json()) as {
    prediction_id?: string;
    candidate_count?: number;
    frame_index?: number;
  };
  if (
    !body.prediction_id ||
    !body.candidate_count ||
    body.candidate_count < 2 ||
    body.frame_index !== frameIndex
  ) {
    throw new Error(
      `[video-prediction-navigation] F${frameIndex} 未产生至少两个对齐车辆候选: ${JSON.stringify(body)}`,
    );
  }
  await expect(panel.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "review", {
    timeout: 15_000,
  });
  return { predictionId: body.prediction_id, candidateCount: body.candidate_count };
}

async function navigatePredictionFrames(page: Page): Promise<DrawWindow> {
  const stage = page.getByTestId("video-konva-stage");
  const drawStartMs = Date.now();
  await page.waitForTimeout(2_000);

  await page.getByTestId("video-timeline-toggle").click();
  const predictionLane = page.getByTestId("video-timeline-lane-predictions");
  await predictionLane.waitFor({ state: "visible", timeout: 5_000 });
  const predictionLaneBox = await predictionLane.boundingBox();
  if (!predictionLaneBox) throw new Error("[video-prediction-navigation] 预测密度轨缺少布局尺寸");
  await page.mouse.move(
    predictionLaneBox.x + predictionLaneBox.width * 0.55,
    predictionLaneBox.y + predictionLaneBox.height * 0.5,
  );
  await page.waitForTimeout(2_200);

  const next = page.getByTestId("video-seek-next-predicted");
  for (const frameIndex of PREDICTION_FRAMES.slice(1)) {
    await next.hover();
    await next.click();
    await expect(stage).toHaveAttribute("data-video-frame-index", String(frameIndex), {
      timeout: 10_000,
    });
    await page.waitForTimeout(frameIndex === 12 ? 1_500 : 1_100);
  }

  const previous = page.getByTestId("video-seek-prev-predicted");
  for (const frameIndex of [18, 12]) {
    await previous.hover();
    await previous.click();
    await expect(stage).toHaveAttribute("data-video-frame-index", String(frameIndex), {
      timeout: 10_000,
    });
    await page.waitForTimeout(1_200);
  }
  await page.waitForTimeout(2_000);
  return { drawStartMs, drawEndMs: Date.now() };
}

export async function runVideoTimelinePredictionNavigation(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onDispatched?: (record: VideoFrameInferenceCleanupRecord) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("video-konva-stage");
  await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-video-frame-index", "0", { timeout: 15_000 });
  await page.getByTestId("video-tool-btn-select").click();

  const serverErrors: string[] = [];
  const collectServerError = (response: Response) => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
  };
  page.on("response", collectServerError);
  try {
    let backendId: string | null = null;
    let cleanupRegistered = false;
    for (const frameIndex of PREDICTION_FRAMES) {
      await seekFrame(page, frameIndex);
      await page.getByTestId("workbench-ai-single").click();
      const panel = page.getByTestId("ai-prediction-popover");
      await panel.waitFor({ state: "visible", timeout: 5_000 });
      backendId = await selectVehicleModel(panel, backendId === null);

      const result = await runFramePrediction(page, panel, project.id, backendId, frameIndex);
      if (!cleanupRegistered) {
        onDispatched?.({
          projectId: project.id,
          taskId: task.id,
          predictionId: result.predictionId,
          annotationIds: [],
        });
        cleanupRegistered = true;
      }
      await panel.getByTitle("关闭当前题 AI").click();
      await panel.waitFor({ state: "hidden", timeout: 5_000 });
    }

    await seekFrame(page, 0);
    const window = await navigatePredictionFrames(page);
    if (serverErrors.length > 0) {
      throw new Error(
        `[video-prediction-navigation] 完整链路出现服务端错误: ${serverErrors.join(", ")}`,
      );
    }
    return window;
  } finally {
    page.off("response", collectServerError);
  }
}
