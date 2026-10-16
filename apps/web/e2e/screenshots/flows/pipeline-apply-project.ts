/**
 * 高清母版：公共两阶段编排 → 套用为项目默认 → 当前题真实执行 → 属性候选。
 *
 * 套用动作不会回填批跑编辑器，因此后半段进入工作台，通过“按项目编排”入口
 * 执行刚刚生成的私有默认副本。全程不采纳候选，避免混入候选审阅生命周期。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import { dockAiPanelAtViewportRight, waitForRecordingWorkbenchLayout } from "./_workbench-layout";
import type { DrawWindow } from "./rotated-bbox";

export interface PipelineApplyCleanupRecord {
  projectId: string;
  taskId: string;
  celeryTaskId?: string;
}

const TEMPLATE_NAME = "车辆检测 → 车型与颜色";
const pointerByPage = new WeakMap<Page, { x: number; y: number }>();
const VEHICLE_TYPES = [
  "car",
  "truck",
  "bus",
  "tanker",
  "slagcar",
  "fire_engine",
  "mixer",
  "ambulance",
  "police_car",
  "engineering_truck",
  "hazardous_goods_vehicle",
  "manned_sweeping_vehicle",
  "school_bus",
];
const VEHICLE_COLORS = [
  "black",
  "white",
  "gray",
  "red",
  "yellow",
  "green",
  "blue",
  "purple",
  "brown",
  "pink",
  "other",
];

async function moveTo(page: Page, locator: Locator, durationMs = 320): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[pipeline-apply-project] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(
    page,
    pointerByPage.get(page) ?? { x: 90, y: 120 },
    target,
    durationMs,
  );
  pointerByPage.set(page, target);
}

async function rowWithVehicleAttributes(page: Page): Promise<Locator> {
  const rows = page.locator('[data-testid^="box-list-item-pred-"]');
  const texts = await rows.allTextContents();
  const index = texts.findIndex((text) => {
    const normalized = text.toLowerCase();
    return VEHICLE_COLORS.some((color) =>
      VEHICLE_TYPES.some((vehicleType) => normalized.endsWith(`${color}${vehicleType}`)),
    );
  });
  if (index < 0) {
    throw new Error(
      `[pipeline-apply-project] 候选缺少车型/颜色二阶段结果：${JSON.stringify(texts)}`,
    );
  }
  return rows.nth(index);
}

export async function runPipelineApplyProject(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  templateId: string,
  onApplied: (pipelineId: string) => void,
  onDispatched: (record: PipelineApplyCleanupRecord) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.image_demo;
  const task = project.tasks.clean;
  const taskRoute = `/projects/${project.id}/annotate?task=${task.id}`;

  // 录制前预热静态资源与原图，正式镜头从完整项目列表开始。
  await page.goto(taskRoute);
  const warmStage = page.getByTestId("workbench-stage");
  await warmStage.waitFor({ state: "visible", timeout: 15_000 });
  await expect(warmStage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(warmStage).toHaveAttribute("data-ai-box-count", "0", { timeout: 10_000 });
  await waitForRecordingWorkbenchLayout(page, "both");

  await page.goto("/ai-pre");
  await expect(page.getByRole("heading", { name: "AI 预标" })).toBeVisible({ timeout: 15_000 });
  const projectCard = page.getByRole("button").filter({ hasText: project.name }).first();
  await projectCard.waitFor({ state: "visible", timeout: 15_000 });
  await page.waitForTimeout(800);
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_800);

  await moveTo(page, projectCard, 420);
  await projectCard.click();
  await expect(page.getByRole("heading", { name: project.name })).toBeVisible({ timeout: 15_000 });

  const librarySelect = page.getByLabel("命名编排库");
  await librarySelect.waitFor({ state: "visible", timeout: 15_000 });
  const templateOption = librarySelect.locator(`option[value="${templateId}"]`);
  await expect(templateOption).toHaveCount(1);
  await moveTo(page, librarySelect, 520);
  await librarySelect.selectOption(templateId);
  await expect(librarySelect).toHaveValue(templateId);
  await expect(templateOption).toContainText(`${TEMPLATE_NAME} · 公共 · 2 阶段`);
  await page.waitForTimeout(2_000);

  const applyButton = page.getByRole("button", { name: "套用为默认", exact: true });
  await moveTo(page, applyButton);
  await expect(applyButton).toBeEnabled();
  const appliedResponse = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "POST" &&
        url.pathname === `/api/v1/projects/${project.id}/pipelines/apply`
      );
    },
    { timeout: 20_000 },
  );
  await applyButton.click();
  const response = await appliedResponse;
  if (response.status() !== 201) {
    throw new Error(
      `[pipeline-apply-project] 套用失败：HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const applied = (await response.json()) as {
    id?: string;
    name?: string;
    scope?: string;
    project_id?: string | null;
    is_default?: boolean;
    stages?: Array<{ model_id?: string; parent_stage?: number | null }>;
  };
  if (!applied.id) throw new Error("[pipeline-apply-project] 套用响应缺少私有编排 ID");
  onApplied(applied.id);
  if (
    applied.name !== TEMPLATE_NAME ||
    applied.scope !== "private" ||
    applied.project_id !== project.id ||
    applied.is_default !== true ||
    applied.stages?.length !== 2 ||
    applied.stages[0]?.model_id !== "detect" ||
    applied.stages[1]?.model_id !== "vehicle-attr-classify" ||
    applied.stages[1]?.parent_stage !== 0
  ) {
    throw new Error(`[pipeline-apply-project] 私有副本语义不完整：${JSON.stringify(applied)}`);
  }
  await expect(page.getByText("已套用命名编排", { exact: true })).toBeVisible();
  await expect(page.getByText(`${TEMPLATE_NAME} · 2 阶段`, { exact: true }).last()).toBeVisible();
  await page.waitForTimeout(2_600);

  await page.goto(taskRoute);
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-ai-box-count", "0", { timeout: 10_000 });
  await waitForRecordingWorkbenchLayout(page, "both");
  await page.waitForTimeout(1_800);

  const aiButton = page.getByTestId("workbench-ai-single");
  await moveTo(page, aiButton, 520);
  await aiButton.click();
  const panel = page.getByTestId("ai-prediction-popover");
  await panel.waitFor({ state: "visible", timeout: 5_000 });
  await dockAiPanelAtViewportRight(page, panel);
  await panel.getByTestId("ai-prediction-advanced-toggle").click();
  const runButton = panel.getByTestId("ai-prediction-run-pipeline");
  await expect(runButton).toHaveText("运行当前题（项目编排 · 2 阶段）");
  await runButton.waitFor({ state: "visible", timeout: 15_000 });
  await expect(runButton).toBeEnabled();
  await page.waitForTimeout(2_100);

  const dispatchedResponse = page.waitForResponse(
    (candidate) => {
      const url = new URL(candidate.url());
      return (
        candidate.request().method() === "POST" &&
        url.pathname === `/api/v1/projects/${project.id}/preannotate`
      );
    },
    { timeout: 20_000 },
  );
  await moveTo(page, runButton);
  await runButton.click();
  const dispatched = await dispatchedResponse;
  if (!dispatched.ok()) {
    throw new Error(
      `[pipeline-apply-project] 推理派发失败：HTTP ${dispatched.status()} ${await dispatched.text()}`,
    );
  }
  const dispatchedBody = (await dispatched.json()) as { job_id?: string };
  if (!dispatchedBody.job_id) {
    throw new Error("[pipeline-apply-project] 推理响应缺少 job_id，无法无痕清理");
  }
  onDispatched({
    projectId: project.id,
    taskId: task.id,
    celeryTaskId: dispatchedBody.job_id,
  });

  await page.waitForFunction(
    () => {
      const workbench = document.querySelector('[data-testid="workbench-stage"]');
      const popover = document.querySelector('[data-testid="ai-prediction-popover"]');
      const candidateCount = Number(workbench?.getAttribute("data-ai-box-count") ?? "0");
      const run = popover?.querySelector<HTMLButtonElement>(
        '[data-testid="ai-prediction-run-pipeline"]',
      );
      return (
        Number.isInteger(candidateCount) && candidateCount > 0 && Boolean(run && !run.disabled)
      );
    },
    undefined,
    { timeout: 120_000 },
  );
  await page.waitForTimeout(2_500);

  await panel.getByTitle("关闭当前题 AI").click();
  await panel.waitFor({ state: "hidden", timeout: 5_000 });
  const aiSection = page.getByTestId("section-header-ai");
  if ((await aiSection.getAttribute("aria-expanded")) === "false") await aiSection.click();
  await expect(aiSection).toContainText(/[1-9]\d*/);
  const attributedRow = await rowWithVehicleAttributes(page);
  await attributedRow.scrollIntoViewIfNeeded();
  await moveTo(page, attributedRow, 480);
  await attributedRow.click();
  await expect(attributedRow).toHaveClass(/border-brand/);
  await page.waitForTimeout(4_200);

  return { drawStartMs, drawEndMs: Date.now() };
}
