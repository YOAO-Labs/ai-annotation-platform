import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 二次推理属性完整链路：已确认文字区域 → 真实裁剪 OCR → AI 属性 → 人工校正。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";
import { waitForRecordingWorkbenchLayout } from "./_workbench-layout";

export interface SecondaryInferenceCleanupRecord {
  projectId: string;
  taskId: string;
  annotationIds: string[];
}

interface CreatedAnnotation {
  id?: string;
}

export async function runSecondaryInferenceAttribute(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated?: (record: SecondaryInferenceCleanupRecord) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.ocr_demo;
  const task = project.tasks.ocr;
  const backend = project.ml_backend;
  const models = backend?.capabilities.models ?? [];
  const hasCropRecognition = models.some(
    (model) =>
      model.id === "ocr-rec" && (model.supported_inputs as string[] | undefined)?.includes("crop"),
  );
  if (!backend?.name.toLowerCase().includes("rapidocr") || !hasCropRecognition) {
    throw new Error(
      `[secondary-inference-attribute] ${project.display_id} 未绑定含 crop/ocr-rec 的真实 RapidOCR backend`,
    );
  }

  const created = await page.evaluate(
    async ({ taskId }) => {
      const token = localStorage.getItem("token");
      if (!token) throw new Error("二次推理父标注准备失败：缺少 token");
      const response = await fetch(`/api/v1/tasks/${taskId}/annotations`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          annotation_type: "polygon",
          tool_unit_id: "region",
          class_name: "text",
          geometry: {
            type: "polygon",
            points: [
              [0.16, 0.325],
              [0.84, 0.325],
              [0.84, 0.405],
              [0.16, 0.405],
            ],
          },
          attributes: {},
        }),
      });
      if (!response.ok) {
        throw new Error(`二次推理父标注准备失败：HTTP ${response.status} ${await response.text()}`);
      }
      return response.json() as Promise<CreatedAnnotation>;
    },
    { taskId: task.id },
  );
  if (!created.id) {
    throw new Error("[secondary-inference-attribute] 父标注响应缺少 id，无法无痕清理");
  }
  const cleanupRecord: SecondaryInferenceCleanupRecord = {
    projectId: project.id,
    taskId: task.id,
    annotationIds: [created.id],
  };
  onCreated?.(cleanupRecord);

  await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "1", { timeout: 10_000 });
  await waitForRecordingWorkbenchLayout(page, "both");

  const annotationRow = page.getByTestId(`box-list-item-${created.id}`);
  await annotationRow.waitFor({ state: "visible", timeout: 10_000 });
  const drawStartMs = Date.now();
  await page.waitForTimeout(2_200);

  await annotationRow.click();
  await openContextToolbar(page, "secondary");
  const bar = page.getByTestId("secondary-inference-bar");
  await bar.waitFor({ state: "visible", timeout: 15_000 });
  const capabilitySelect = page.getByTestId("secondary-cap-select");
  const cropOption = capabilitySelect.locator("option").filter({
    hasText: "RapidOCR · 文本识别（原子）",
  });
  if ((await cropOption.count()) !== 1) {
    throw new Error("[secondary-inference-attribute] 二次推理面板未提供 RapidOCR 文本识别原子模型");
  }
  const cropOptionValue = await cropOption.getAttribute("value");
  if (!cropOptionValue) {
    throw new Error("[secondary-inference-attribute] ocr-rec 能力缺少选择值");
  }
  await capabilitySelect.selectOption(cropOptionValue);
  await expect(capabilitySelect).toHaveValue(`${backend.id}:ocr-rec`);
  await page.waitForTimeout(2_600);

  const inferenceResponsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "POST" &&
        url.pathname === `/api/v1/tasks/${task.id}/annotations/${created.id}/secondary-inference`
      );
    },
    { timeout: 120_000 },
  );
  await page.getByTestId("secondary-run").click();
  await expect(page.getByTestId("secondary-run")).toContainText("运行中", { timeout: 5_000 });
  const inferenceResponse = await inferenceResponsePromise;
  if (!inferenceResponse.ok()) {
    throw new Error(
      `[secondary-inference-attribute] 二次推理失败：HTTP ${inferenceResponse.status()} ${await inferenceResponse.text()}`,
    );
  }

  const textOrigin = page.getByTestId("attr-ai-origin-text");
  const orientationOrigin = page.getByTestId("attr-ai-origin-orientation");
  const languageOrigin = page.getByTestId("attr-ai-origin-language");
  await expect(textOrigin).toBeVisible({ timeout: 15_000 });
  await expect(orientationOrigin).toBeVisible();
  await expect(languageOrigin).toBeVisible();
  const textInput = page
    .locator("label")
    .filter({ hasText: "识别文本" })
    .locator('input[type="text"]');
  await expect(textInput).not.toHaveValue("");
  await page.waitForTimeout(2_500);
  await expect(textOrigin).toHaveAttribute("title", /模型 ocr-rec/);
  await textOrigin.hover();
  await page.waitForTimeout(2_400);

  const updateResponsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === "PATCH" &&
        url.pathname === `/api/v1/tasks/${task.id}/annotations/${created.id}`
      );
    },
    { timeout: 15_000 },
  );
  await textInput.fill("强力去污，符合国标");
  await textInput.press("Tab");
  const updateResponse = await updateResponsePromise;
  if (!updateResponse.ok()) {
    throw new Error(
      `[secondary-inference-attribute] 人工修正保存失败：HTTP ${updateResponse.status()}`,
    );
  }
  await expect(textInput).toHaveValue("强力去污，符合国标");
  await expect(textOrigin).toBeHidden({ timeout: 10_000 });
  await expect(orientationOrigin).toBeVisible();
  await expect(languageOrigin).toBeVisible();
  await page.waitForTimeout(4_200);

  return { drawStartMs, drawEndMs: Date.now() };
}
