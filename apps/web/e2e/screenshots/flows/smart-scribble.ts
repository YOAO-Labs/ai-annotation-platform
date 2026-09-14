import { openContextToolbar } from "../../fixtures/context-toolbar";
/** Real SAM3 refinement of one stored road-vehicle Mask; no inference fixtures. */
import { expect, type Page, type Request, type Response } from "@playwright/test";
import type Konva from "konva";
import type { AnnotationResponse } from "../../../src/types";
import type { AiMaskAcceptRequest, AiMaskAcceptResponse } from "../../../src/api/aiMasks";
import type { InteractiveAnnotateResponse, InteractiveRequest } from "../../../src/api/ml-backends";
import type { CocoRle } from "../../../src/pages/Workbench/stage/shared/geometry/maskRle";
import type { ScreenshotSeedCatalog, SeedRasterMaskData } from "../../fixtures/seed";
import {
  commitPendingAnnotationClass,
  hidePredictions,
  mediaPoint,
  movePointerAtRefreshRate,
  openImageAnnotate,
  recordingAnchor,
  renderedMediaBounds,
} from "./_canvas";
import {
  recordingLayoutCommand,
  recordingPanelCommand,
  waitForRecordingPanels,
} from "./_workbench-layout";
import {
  inspectScribbleRound,
  isSmartScribbleRequest,
  scribbleMaskDifference,
  scribbleMaskEvidence,
  SMART_SCRIBBLE_MODEL,
  trackScribbleAcceptance,
  type ScribbleSourceIdentity,
} from "./_smart-scribble-evidence";
import type { CandidateReviewCleanupRecord } from "./candidate-review-lifecycle";
import type { DrawWindow } from "./rotated-bbox";
import { pickSamRecordingCandidate } from "./_sam-recording-candidates";

async function readJson<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (path) => {
    const response = await fetch(path, {
      headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
    });
    if (!response.ok) throw new Error(`[smart-scribble] ${path}: HTTP ${response.status}`);
    return response.json();
  }, path);
}

async function waitForVisibleCandidate(
  page: Page,
  samples: [number, number][],
  candidateId: string,
  size: [number, number],
  difference?: ReturnType<typeof scribbleMaskDifference>,
) {
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-sam-candidate-count", "1", { timeout: 20_000 });
  const media = await renderedMediaBounds(stage);
  // Sample real foreground pixels, not a DOM count that can precede bitmap decoding.
  await expect
    .poll(
      async () =>
        stage.evaluate(
          async (element, { media, samples, candidateId, size, difference }) => {
            const runtime = (window as unknown as { Konva?: typeof Konva }).Konva;
            const group = runtime?.stages
              .flatMap((stage) => stage.find(".sam-native-mask-candidate"))
              .find((node) => node.id() === candidateId) as Konva.Group | undefined;
            const layer = group?.getLayer() as
              | (Konva.Layer & { _waitingForDraw?: boolean })
              | null
              | undefined;
            const maskImage = group?.findOne("Image") as Konva.Image | undefined;
            const bitmap = maskImage?.image() as
              | (CanvasImageSource & { width: number; height: number })
              | undefined;
            if (
              !group?.isVisible() ||
              !layer?.isVisible() ||
              !maskImage ||
              !bitmap?.width ||
              !bitmap.height
            )
              return {
                ready: false,
                reason: "candidate bitmap readiness",
                runtime: !!runtime,
                groups: runtime?.stages
                  .flatMap((stage) => stage.find(".sam-native-mask-candidate"))
                  .map((node) => node.id()),
                candidateId,
                layerWaiting: layer?._waitingForDraw,
                groupVisible: group?.isVisible(),
                bitmapWidth: bitmap?.width,
                bitmapHeight: bitmap?.height,
              };
            // The candidate outline animates this layer continuously, so its
            // private _waitingForDraw flag is not an idle/readiness signal.
            // Observe a natural completed draw after the exact bitmap is ready.
            const painted = await new Promise<boolean>((resolve) => {
              const event = "draw.aapScribbleRecording";
              const finish = (ready: boolean) => {
                clearTimeout(timeout);
                layer.off(event, onDraw);
                resolve(ready);
              };
              const onDraw = () => finish(true);
              const timeout = setTimeout(() => finish(false), 1_000);
              layer.on(event, onDraw);
            });
            if (!painted) return { ready: false, reason: "no completed candidate layer draw" };
            // Inspect the current decoded bitmap: strokes and preview outlines may
            // cover changed pixels on the composed canvas even after a correct draw.
            if (difference) {
              const buffer = document.createElement("canvas");
              buffer.width = bitmap.width;
              buffer.height = bitmap.height;
              const context = buffer.getContext("2d");
              if (!context) return { ready: false, reason: "bitmap context" };
              context.drawImage(bitmap, 0, 0);
              const foreground = ([x, y]: [number, number]) => {
                const px = Math.floor(
                  ((x * size[1] - maskImage.x()) / maskImage.width()) * bitmap.width,
                );
                const py = Math.floor(
                  ((y * size[0] - maskImage.y()) / maskImage.height()) * bitmap.height,
                );
                return (
                  px >= 0 &&
                  py >= 0 &&
                  px < bitmap.width &&
                  py < bitmap.height &&
                  context.getImageData(px, py, 1, 1).data[3] > 0
                );
              };
              if (
                !difference.added.every(foreground) ||
                !difference.removed.every((point) => !foreground(point))
              )
                return {
                  ready: false,
                  reason: "decoded bitmap pixel delta",
                  added: difference.added.map(foreground),
                  removed: difference.removed.map(foreground),
                  bitmapWidth: bitmap.width,
                  bitmapHeight: bitmap.height,
                  x: maskImage.x(),
                  y: maskImage.y(),
                  width: maskImage.width(),
                  height: maskImage.height(),
                  size,
                };
            }
            const canvases = [...element.querySelectorAll("canvas")];
            const isPurple = ([x, y]: [number, number]) =>
              canvases.some((canvas) => {
                const bounds = canvas.getBoundingClientRect();
                if (bounds.width <= 0 || bounds.height <= 0) return false;
                const px = Math.floor(
                  ((media.x + x * media.width - bounds.x) * canvas.width) / bounds.width,
                );
                const py = Math.floor(
                  ((media.y + y * media.height - bounds.y) * canvas.height) / bounds.height,
                );
                if (px < 0 || py < 0 || px >= canvas.width || py >= canvas.height) return false;
                const pixel = canvas.getContext("2d")?.getImageData(px, py, 1, 1).data;
                return (
                  !!pixel &&
                  pixel[3] > 0 &&
                  Math.abs(pixel[0] - 168) < 12 &&
                  Math.abs(pixel[1] - 85) < 12 &&
                  Math.abs(pixel[2] - 247) < 12
                );
              });
            return samples.some(isPurple)
              ? { ready: true }
              : { ready: false, reason: "painted foreground" };
          },
          { media, samples, candidateId, size, difference },
        ),
      { timeout: 20_000, message: "The returned native Mask must be painted on the canvas" },
    )
    .toEqual({ ready: true });
}

export async function runSmartScribble(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  source: SeedRasterMaskData,
  cleanup: CandidateReviewCleanupRecord,
  onEvidence?: (evidence: unknown) => void,
): Promise<DrawWindow> {
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  const backend = project.ml_backend;
  if (!backend) throw new Error("[smart-scribble] image_demo 未注册真实 SAM3 后端");
  expect(cleanup.projectId).toBe(project.id);
  expect(cleanup.taskId).toBe(task.id);
  expect(cleanup.annotationIds).toContain(source.annotation_id);
  const endpoint = `/api/v1/projects/${project.id}/ml-backends/${backend.id}/interactive-annotating`;
  const requests: InteractiveRequest[] = [];
  const failedMaskRequests: string[] = [];
  const isScribbleInference = (request: Request) =>
    request.method() === "POST" &&
    new URL(request.url()).pathname === endpoint &&
    isSmartScribbleRequest(request.postDataJSON() as InteractiveRequest);
  const observeRequest = (request: Request) => {
    if (isScribbleInference(request)) requests.push(request.postDataJSON() as InteractiveRequest);
    if (/\/annotations\/tmp_[^/]+\/mask-content/.test(request.url()))
      failedMaskRequests.push("temporary annotation requested persisted content");
  };
  const observeResponse = (response: Response) => {
    if (response.url().includes("/mask-content") && !response.ok())
      failedMaskRequests.push(`${response.status()} ${new URL(response.url()).pathname}`);
  };
  page.on("request", observeRequest);
  page.on("response", observeResponse);
  try {
    await openImageAnnotate(page, catalog);
    const setup = await readJson<{
      models?: Array<{
        id: string;
        is_interactive?: boolean;
        supported_prompts?: string[];
        supported_inputs?: string[];
        supported_geometric_outputs?: string[];
      }>;
    }>(page, `/api/v1/projects/${project.id}/ml-backends/${backend.id}/setup`);
    const model = setup.models?.find((item) => item.id === SMART_SCRIBBLE_MODEL);
    expect(model, "Registered SAM3 must advertise native Mask and scribble input").toMatchObject({
      is_interactive: true,
      supported_prompts: expect.arrayContaining(["mask", "scribble"]),
      supported_inputs: expect.arrayContaining(["mask_prompt", "scribble_prompt"]),
      supported_geometric_outputs: expect.arrayContaining(["mask"]),
    });
    const annotationsPath = `/api/v1/tasks/${task.id}/annotations`;
    const before = await readJson<AnnotationResponse[]>(page, annotationsPath);
    const original = before.find((item) => item.id === source.annotation_id);
    expect(original?.geometry).toMatchObject({
      type: "raster_mask",
      mask: { sha256: source.mask.sha256 },
    });
    expect(original?.version).toBeGreaterThan(0);
    const identity: ScribbleSourceIdentity = {
      task_id: task.id,
      annotation_id: source.annotation_id,
      source_version: original!.version!,
      source_digest: source.mask.sha256,
    };
    const sourceRle = await readJson<CocoRle>(
      page,
      `/api/v1/annotations/${source.annotation_id}/mask-content`,
    );
    expect(scribbleMaskEvidence(sourceRle).content_digest).toBe(identity.source_digest);
    const anchor = recordingAnchor(catalog, "image_demo", "annotating", "primary_vehicle");
    expect(original?.class_name).toBe(anchor.label);

    await recordingLayoutCommand(page, "图片 AI 审阅布局");
    await recordingPanelCommand(page, "讨论", "隐藏面板");
    await page
      .getByRole("tab")
      .filter({ has: page.getByRole("button", { name: "隐藏类别面板", exact: true }) })
      .click();
    await page
      .getByRole("tab")
      .filter({ has: page.getByRole("button", { name: "隐藏标注详情", exact: true }) })
      .click();
    await waitForRecordingPanels(page, ["canvas", "class-palette", "inspector"], ["discussion"]);
    await hidePredictions(page);
    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toHaveAttribute("data-image-ready", "true");
    const sourceRow = page.getByTestId(`box-list-item-${source.annotation_id}`);
    await expect(sourceRow).toContainText(/\d+ px · \d+ 组件 · \d+ 孔洞 · AABB/);
    await sourceRow.click();
    await expect(sourceRow).toHaveClass(/border-brand/);
    const collapse = page.getByRole("button", { name: "收起浮窗", exact: true });
    if (await collapse.isVisible()) await collapse.click();
    await page.getByTitle("适应视口（双击空白）").click();
    await page.waitForTimeout(800);

    const tool = page.getByTestId("tool-btn-smart-scribble");
    await expect(tool).toBeEnabled({ timeout: 20_000 });
    await tool.click();
    await expect(page.getByTestId("mask-prompt-source")).toBeVisible();
    await openContextToolbar(page, "interactive");
    await page.getByTestId("interactive-toolbar-advanced-toggle").click();
    const modelSelect = page.getByTestId("ai-tool-model-select");
    if (await modelSelect.isVisible()) await modelSelect.selectOption(SMART_SCRIBBLE_MODEL);
    await expect(page.getByTestId("single-frame-output-geometry-select")).toHaveValue("mask");
    await page.waitForTimeout(800);
    const drawStartMs = Date.now();
    await page.waitForTimeout(1000);
    const rounds: ReturnType<typeof inspectScribbleRound>["evidence"][] = [];
    let previousSession: string | null | undefined;
    let previousRle: CocoRle | undefined;
    for (const [index, anchors] of [anchor.positive_stroke, anchor.negative_stroke].entries()) {
      const [first, last] = anchors;
      if (!first || !last) throw new Error("[smart-scribble] primary_vehicle 缺少正负笔迹锚点");
      await openContextToolbar(page, "interactive");
      if (index === 1) await page.getByTestId("ai-tool-polarity").click();
      await expect(page.getByTestId("ai-tool-polarity")).toHaveAttribute(
        "title",
        index === 0 ? /正向/ : /负向/,
      );
      const media = await renderedMediaBounds(stage);
      const start = mediaPoint(media, first);
      const end = mediaPoint(media, last);
      const pending = page.waitForResponse((response) => isScribbleInference(response.request()), {
        timeout: 120_000,
      });
      await page.mouse.move(start.x, start.y);
      await page.mouse.down();
      await movePointerAtRefreshRate(page, start, end, 900);
      await page.mouse.up();
      const response = await pending;
      expect(response.ok(), await response.text()).toBeTruthy();
      const request = response.request().postDataJSON() as InteractiveRequest;
      const result = (await response.json()) as InteractiveAnnotateResponse;
      if (index === 1 && previousSession) expect(request.context.mask_input).toBe(previousSession);
      const round = inspectScribbleRound(
        request,
        result,
        identity,
        backend.id,
        index === 0 ? [1] : [1, 0],
      );
      expect(round.evidence.mask.size).toEqual(source.mask.size);
      const target = pickSamRecordingCandidate(result.result, anchor.bbox);
      expect(target.index).toBe(0);
      Object.assign(round.evidence, { target_geometry: target });
      expect(
        round.evidence.mask.content_digest,
        "Each stroke must change the visible Mask result",
      ).not.toBe(index === 0 ? identity.source_digest : rounds[0].mask.content_digest);
      previousSession = result.mask_input_next;
      rounds.push(round.evidence);
      await page.mouse.move(0, 0);
      // A retained previous candidate shares most foreground with the new result.
      // Require added pixels to turn purple and removed pixels to stop being purple.
      const difference = previousRle
        ? scribbleMaskDifference(previousRle, round.candidate.value.rle)
        : undefined;
      await waitForVisibleCandidate(
        page,
        round.evidence.mask.samples,
        round.evidence.candidate_id,
        round.evidence.mask.size,
        difference,
      );
      previousRle = round.candidate.value.rle;
      await page.waitForTimeout(1800);
    }
    expect(requests).toHaveLength(2);
    const finalRound = rounds[1];
    const acceptance = page
      .waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname === `/api/v1/tasks/${task.id}/ai-mask-candidates/accept`,
        { timeout: 30_000 },
      )
      .then(async (response) => {
        expect(response.ok(), await response.text()).toBeTruthy();
        const accepted = (await response.json()) as AiMaskAcceptResponse;
        trackScribbleAcceptance(accepted, cleanup);
        return { response, accepted };
      });
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeVisible();
    await page.waitForTimeout(1_200);
    const [created, { response, accepted }] = await Promise.all([
      commitPendingAnnotationClass(page, {
        label: anchor.label,
        taskId: task.id,
        onCreated: (id) => {
          if (!cleanup.annotationIds.includes(id)) cleanup.annotationIds.push(id);
        },
      }),
      acceptance,
    ]);
    const acceptRequest = response.request().postDataJSON() as AiMaskAcceptRequest;
    expect(acceptRequest.target).toMatchObject({
      mode: "refine",
      source_annotation_id: source.annotation_id,
      source_version: identity.source_version,
    });
    expect(response.request().headers()["if-match"]).toBe(`W/"${identity.source_version}"`);
    expect(acceptRequest.candidate.candidate.candidate_id).toBe(finalRound.candidate_id);
    expect(acceptRequest.routing).toEqual(finalRound.routing);
    expect(created.id).toBe(source.annotation_id);
    expect(accepted.source_version).toBe(identity.source_version);
    expect(accepted.result_version).toBeGreaterThan(identity.source_version);
    expect(accepted.annotation.version).toBe(accepted.result_version);
    expect(accepted.content_digest).toBe(finalRound.mask.content_digest);
    expect(accepted.annotation.geometry).toMatchObject({
      type: "raster_mask",
      mask: { sha256: accepted.content_digest },
    });
    expect(accepted.prediction.source).toBe("interactive_accept");
    expect(accepted.prediction.ml_backend_id).toBe(finalRound.routing?.backend_instance_id);
    expect(accepted.prediction.model_version).toBe(finalRound.model_version);
    await expect(stage).toHaveAttribute("data-sam-candidate-count", "0");
    await expect(sourceRow).toContainText(`${finalRound.mask.area} px`);
    await expect(sourceRow).toContainText(/\d+ 组件 · \d+ 孔洞 · AABB/);
    await expect(sourceRow).not.toContainText("load_failed");
    await page.waitForFunction(
      ({ id, area }) => {
        const runtime = (window as unknown as { Konva?: typeof Konva }).Konva;
        if (
          !runtime ||
          runtime.stages.some((stage) => stage.find(".sam-native-mask-candidate").length > 0)
        )
          return false;
        const group = runtime.stages
          .flatMap((stage) => stage.find(".raster-mask-annotation"))
          .find((node) => node.id() === id) as Konva.Group | undefined;
        const image = group?.findOne(".raster-mask-fill") as Konva.Image | undefined;
        const bitmap = image?.image() as
          | (CanvasImageSource & { width: number; height: number })
          | undefined;
        if (!group?.isVisible() || !bitmap?.width || !bitmap.height) return false;
        const buffer = document.createElement("canvas");
        buffer.width = bitmap.width;
        buffer.height = bitmap.height;
        const context = buffer.getContext("2d");
        if (!context) return false;
        context.drawImage(bitmap, 0, 0);
        const pixels = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
        let foreground = 0;
        for (let index = 3; index < pixels.length; index += 4)
          if (pixels[index] > 0) foreground += 1;
        return foreground === area;
      },
      { id: source.annotation_id, area: finalRound.mask.area },
      { timeout: 20_000 },
    );
    await page.waitForTimeout(3_500);
    const drawEndMs = Date.now();
    const after = await readJson<AnnotationResponse[]>(page, annotationsPath);
    expect(after.map((item) => item.id).sort()).toEqual(before.map((item) => item.id).sort());
    expect(after.find((item) => item.id === source.annotation_id)).toMatchObject({
      version: accepted.result_version,
      geometry: { type: "raster_mask", mask: { sha256: accepted.content_digest } },
    });
    await page.reload();
    await expect(stage).toHaveAttribute("data-image-ready", "true");
    await expect(sourceRow).toContainText(`${finalRound.mask.area} px`);
    expect(failedMaskRequests).toEqual([]);
    onEvidence?.({
      endpoint,
      source: identity,
      source_mask: scribbleMaskEvidence(sourceRle),
      rounds,
      accepted: {
        annotation_id: accepted.annotation.id,
        prediction_id: accepted.prediction.id,
        source_version: accepted.source_version,
        result_version: accepted.result_version,
        content_digest: accepted.content_digest,
      },
    });
    return { drawStartMs, drawEndMs };
  } finally {
    page.off("request", observeRequest);
    page.off("response", observeResponse);
  }
}
