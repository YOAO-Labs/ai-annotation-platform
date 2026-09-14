import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * Real SAM3 demonstrations use the primary vehicle as the prompt target.
 * Point candidates are selected by target geometry; Exemplar can accept a matching
 * car elsewhere in the scene. All accepted results use the current class picker.
 */
import { expect, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import type { InteractiveAnnotateResponse, InteractiveRequest } from "../../../src/api/ml-backends";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import {
  commitPendingAnnotationClass,
  mediaBbox,
  mediaPoint,
  movePointerAtRefreshRate,
  openImageAnnotate,
  renderedMediaBounds,
} from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";
import { recordingPanelCommand, waitForRecordingPanels } from "./_workbench-layout";
import { verifySavedImageDrawing } from "./_image-drawing";
import {
  pickSamRecordingCandidate,
  assertSamRecordingSavedGeometry,
} from "./_sam-recording-candidates";

export interface SamRecordingWindow extends DrawWindow {
  evidence: Record<string, unknown>;
}

export type SamRecordingTool = "smart-point" | "smart-box" | "magic-box" | "exemplar";

export async function runSamToolRecording(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  toolId: SamRecordingTool,
  options: {
    accept?: boolean;
    onCreated?: (id: string, annotation: Record<string, unknown>) => void;
  } = {},
): Promise<SamRecordingWindow> {
  const anchor = catalog.projects.image_demo.tasks.annotating.recording_anchors?.primary_vehicle;
  if (!anchor) {
    throw new Error("[sam-interactive] image_demo.annotating 缺少 primary_vehicle 语义锚点");
  }
  await openImageAnnotate(page, catalog);
  // fresh repair 会异步回填媒体；stage 外壳先出现，Konva 背景层稍后才真正绘出图像。
  // 等画布中心像素有内容再派发 prompt，避免在 checkerboard 占位期拖框被静默忽略。
  await page.waitForFunction(
    () => {
      const canvas = document.querySelector('[data-testid="workbench-stage"] canvas');
      if (!(canvas instanceof HTMLCanvasElement)) return false;
      try {
        const data = canvas
          .getContext("2d")
          ?.getImageData(Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), 1, 1).data;
        return !!data && data[3] > 0;
      } catch {
        return false;
      }
    },
    undefined,
    { timeout: 30_000 },
  );
  await waitForRecordingPanels(page, ["canvas", "task-queue", "ai-task"]);
  await recordingPanelCommand(page, "讨论", "隐藏面板");
  for (const title of ["类别面板", "标注详情"]) {
    await page
      .getByRole("tab")
      .filter({
        has: page.getByRole("button", { name: `隐藏${title}`, exact: true }),
      })
      .click();
  }
  await waitForRecordingPanels(
    page,
    ["canvas", "class-palette", "inspector"],
    ["discussion", "ai-task"],
  );
  await page.waitForTimeout(500);

  const tool = page.getByTestId(`tool-btn-${toolId}`);
  await tool.waitFor({ state: "visible" });
  if (!(await tool.isEnabled())) {
    throw new Error(`[sam-interactive] ${toolId} 被禁用，检查 image_demo 的 SAM3 能力绑定`);
  }
  await tool.click();
  await openContextToolbar(page, "interactive");
  if (toolId === "exemplar") {
    // SAM3 exemplar 当前返回框候选；显式让请求几何与 Exemplar 形态都选择框，
    // 避免默认原生 Mask 合同把合法 rectangle candidate 当成无效 Mask。
    await page.getByTestId("single-frame-output-geometry-select").selectOption("polygon");
    await page.getByTestId("exemplar-output-mode-select").selectOption("box");
  }
  // Magic Box's palette is a read-only legend until its post-draw picker opens.
  // Its selected class is therefore verified on the actual saved annotation.
  if (toolId !== "magic-box") {
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
      "data-active-class",
      anchor.label,
    );
  }
  await page.waitForTimeout(600);

  const stage = page.getByTestId("workbench-stage");
  const box = await renderedMediaBounds(stage);

  const inferenceResponse = page.waitForResponse(
    (response) => {
      if (
        response.request().method() !== "POST" ||
        !new URL(response.url()).pathname.endsWith("/interactive-annotating")
      )
        return false;
      const request = response.request().postDataJSON() as InteractiveRequest;
      // Backend warmup uses this same endpoint with a synthetic center point.
      // Only the actual tool dispatch has the selected model and output geometry.
      return (
        request.task_id === catalog.projects.image_demo.tasks.annotating.id &&
        !!request.context.model_id &&
        !!request.context.output_geometry &&
        request.context.type ===
          (toolId === "smart-point"
            ? "point"
            : toolId === "exemplar"
              ? "exemplar"
              : "interactive_box")
      );
    },
    { timeout: 120_000 },
  );
  const drawStartMs = Date.now();
  await page.waitForTimeout(1_200); // 稳定展示已选工具、目标和当前 car 类别

  // 锚点由媒体归一化坐标表达；可由模型预选、人工复核后写入 screenshot catalog。
  // 录制阶段只消费已版本化的锚点，避免每次推理漂移导致 GIF 构图不稳定。
  if (toolId === "smart-point") {
    const point = mediaPoint(box, anchor.point);
    // 4K Konva 重绘下 Playwright 的 steps 会把一次移入拖成约 10 秒；单点提示无需
    // 人为放慢鼠标，直接移入后保留短暂停顿即可看清点击位置与因果关系。
    await page.mouse.move(point.x, point.y);
    await page.waitForTimeout(350);
    await page.mouse.click(point.x, point.y);
  } else {
    const { start, end } = mediaBbox(box, anchor.bbox);
    await page.mouse.move(start.x, start.y);
    await page.waitForTimeout(350);
    await page.mouse.down();
    await movePointerAtRefreshRate(page, start, end, 650);
    await page.mouse.up();
  }

  const response = await inferenceResponse;
  expect(response.ok(), `SAM ${toolId} HTTP ${response.status()}`).toBeTruthy();
  const result = (await response.json()) as InteractiveAnnotateResponse;
  expect(result.result.length, "A real SAM response must include candidates").toBeGreaterThan(0);
  expect(result.model_version, "Record actual model lineage").toBeTruthy();
  const requestedBackendId = new URL(response.url()).pathname
    .split("/ml-backends/")[1]
    ?.split("/")[0];
  expect(requestedBackendId).toBeTruthy();
  expect(result.routing?.requested_backend_id).toBe(requestedBackendId);
  const evidence: Record<string, unknown> = {
    endpoint: "POST /api/v1/projects/{project_id}/ml-backends/{backend_id}/interactive-annotating",
    tool: toolId,
    model_version: result.model_version,
    routing: result.routing,
    prompt_summary: result.prompt_summary,
    inference_time_ms: result.inference_time_ms,
    result_count: result.result.length,
    result_sha256: createHash("sha256").update(JSON.stringify(result.result)).digest("hex"),
  };
  let created: Record<string, unknown> | undefined;
  const acceptTitle = page.getByText("接受 SAM 候选 → 选类别", { exact: true });
  if (toolId === "magic-box") {
    await acceptTitle.waitFor({ state: "visible", timeout: 120_000 });
  } else {
    // Candidate readiness belongs to the canvas, independently of the optional pet.
    await expect(stage).toHaveAttribute("data-sam-candidate-count", /^[1-9]\d*$/, {
      timeout: 120_000,
    });
  }
  await page.waitForTimeout(1_500);
  const selectedCandidate =
    toolId === "smart-point" ? pickSamRecordingCandidate(result.result, anchor.bbox) : undefined;
  if (selectedCandidate) {
    await expect(stage).toHaveAttribute("data-sam-candidate-count", String(result.result.length));
    for (let index = 0; index < selectedCandidate.index; index += 1) {
      await page.keyboard.press("Tab");
      await page.waitForTimeout(1_000);
    }
    evidence.selected_candidate = selectedCandidate;
  }

  if (options.accept) {
    if (toolId !== "magic-box") await page.keyboard.press("Enter");
    await page.getByTestId("class-picker-popover").waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForTimeout(800);
    created = await commitPendingAnnotationClass(page, {
      onCreated: options.onCreated,
      label: anchor.label,
      taskId: catalog.projects.image_demo.tasks.annotating.id,
    });
    expect(created.class_name).toBe(anchor.label);
    if (selectedCandidate) assertSamRecordingSavedGeometry(created.geometry, selectedCandidate);
    await acceptTitle.waitFor({ state: "hidden", timeout: 15_000 });
    const savedRow = page.getByTestId(`box-list-item-${created.id}`);
    await expect(savedRow).toBeVisible();
    if ((created.geometry as { type?: string })?.type === "raster_mask") {
      await expect(savedRow).toContainText(/\d+ px · \d+ 组件/);
    }
    await page.waitForTimeout(toolId === "magic-box" ? 3_200 : 2_000);
  } else {
    // 文档工具示例停在真实候选态，不落标注；page 关闭后候选自然消失。
    await page.waitForTimeout(1200);
  }

  const drawEndMs = Date.now();
  if (created) {
    const saved = await verifySavedImageDrawing(
      page,
      catalog.projects.image_demo.tasks.annotating.id,
      String(created.id),
      ["bbox", "polygon", "multipolygon", "raster_mask"],
    );
    evidence.saved_annotation = saved;
  }
  return { drawStartMs, drawEndMs, evidence };
}

export async function runSamInteractive(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated?: (id: string, annotation: Record<string, unknown>) => void,
): Promise<SamRecordingWindow> {
  return runSamToolRecording(page, catalog, "magic-box", { accept: true, onCreated });
}
