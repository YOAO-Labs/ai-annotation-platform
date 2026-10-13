import { openContextToolbar } from "../fixtures/context-toolbar";
import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import type { APIResponse, Page, Request } from "@playwright/test";
import { expect, test, type SeedNativeMaskCandidateData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const MATRIX = process.env.PLAYWRIGHT_RASTER_MASK_MATRIX;

interface AcceptedMaskResponse {
  prediction: { id: string };
  annotation: {
    id: string;
    annotation_type: "raster_mask" | "video_track_mask";
    version?: number;
  };
  content_digest: string;
  replayed: boolean;
}

const nativeSetup = {
  name: "e2e-native-mask",
  version: "1.0.0",
  model_version: "e2e-native-mask",
  is_interactive: true,
  supported_prompts: ["point", "interactive_box", "exemplar", "mask", "scribble"],
  supported_inputs: ["full_image", "point_prompt", "bbox_prompt", "mask_prompt", "scribble_prompt"],
  supported_geometric_outputs: ["polygon", "mask"],
  supported_trackers: ["sam2_video"],
  models: [
    {
      id: "e2e-native-mask",
      display_name: "E2E Native Mask",
      task: "interactive_seg",
      model_family: "e2e",
      composition: "atom",
      is_interactive: true,
      supported_prompts: ["point", "interactive_box", "exemplar", "mask", "scribble"],
      supported_inputs: [
        "full_image",
        "point_prompt",
        "bbox_prompt",
        "mask_prompt",
        "scribble_prompt",
      ],
      supported_geometric_outputs: ["polygon", "mask"],
      resource_profile: { device: "cpu", batchable: false },
    },
    {
      id: "grounded-sam2-tracker",
      display_name: "Grounded-SAM2 Tracker",
      task: "tracker",
      model_family: "grounded-sam2",
      composition: "composite",
      is_interactive: false,
      supported_prompts: ["point", "bbox", "correction_frame"],
      supported_inputs: ["video", "point_prompt", "bbox_prompt", "mask_prompt"],
      supported_geometric_outputs: ["mask"],
      supported_trackers: ["sam2_video"],
      max_window_frames: 16,
      resource_profile: { device: "gpu", batchable: false },
    },
  ],
};

const polygonOnlySetup = {
  ...nativeSetup,
  supported_prompts: ["point", "interactive_box", "exemplar"],
  supported_inputs: ["full_image", "point_prompt", "bbox_prompt"],
  supported_geometric_outputs: ["polygon"],
  models: nativeSetup.models.map((model) =>
    model.id === "e2e-native-mask"
      ? {
          ...model,
          supported_prompts: ["point", "interactive_box", "exemplar"],
          supported_inputs: ["full_image", "point_prompt", "bbox_prompt"],
          supported_geometric_outputs: ["polygon"],
        }
      : model,
  ),
};

interface VideoMaskAnnotationResponse {
  id: string;
  version: number;
  geometry: {
    type: "video_track_mask";
    track_id: string;
    keyframes: Array<{
      frame_index: number;
      mask: {
        encoding: "coco_rle_ref";
        size: [number, number];
        object_key: string;
        sha256: string;
        runs: number;
        bytes: number;
      };
      source?: string;
    }>;
  };
}

async function routeNativeCandidate(
  page: Page,
  fixture: SeedNativeMaskCandidateData,
  options?: { failFirst?: boolean },
): Promise<{ contexts: Array<Record<string, unknown>> }> {
  const contexts: Array<Record<string, unknown>> = [];
  let failedPrompt = false;
  await page.route(/\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/setup/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(nativeSetup),
    }),
  );
  await page.route(
    /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/interactive-annotating(?:-frame)?/,
    async (route) => {
      const request = route.request();
      if (request.headers()["content-type"]?.includes("application/json")) {
        const body = request.postDataJSON() as { context?: Record<string, unknown> };
        contexts.push(body.context ?? {});
      } else {
        const match = request.postData()?.match(/name="context"\r?\n\r?\n([^\r\n]+)/);
        contexts.push(match ? (JSON.parse(match[1]) as Record<string, unknown>) : {});
      }
      const isTargetPrompt = contexts.at(-1)?.type === "scribble";
      const shouldFail = options?.failFirst && isTargetPrompt && !failedPrompt;
      if (shouldFail) failedPrompt = true;
      await route.fulfill(
        shouldFail
          ? {
              status: 503,
              contentType: "application/json",
              body: JSON.stringify({
                detail: { reason: "temporary_failure", message: "retry the same prompt" },
              }),
            }
          : {
              status: 200,
              contentType: "application/json",
              body: JSON.stringify(fixture.response),
            },
      );
    },
  );
  return { contexts };
}

async function routePolygonCandidate(page: Page): Promise<Array<Record<string, unknown>>> {
  const contexts: Array<Record<string, unknown>> = [];
  await page.route(/\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/setup/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(polygonOnlySetup),
    }),
  );
  await page.route(
    /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/interactive-annotating(?:-frame)?/,
    async (route) => {
      const request = route.request();
      if (request.headers()["content-type"]?.includes("application/json")) {
        const body = request.postDataJSON() as { context?: Record<string, unknown> };
        contexts.push(body.context ?? {});
      } else {
        const match = request.postData()?.match(/name="context"\r?\n\r?\n([^\r\n]+)/);
        contexts.push(match ? (JSON.parse(match[1]) as Record<string, unknown>) : {});
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          result: [
            {
              type: "polygonlabels",
              value: {
                points: [
                  [0.42, 0.34],
                  [0.72, 0.36],
                  [0.68, 0.66],
                  [0.4, 0.62],
                ],
                polygonlabels: ["object"],
              },
              score: 0.91,
            },
          ],
          output_geometry: "polygon",
          model_version: "e2e-polygon-only",
        }),
      });
    },
  );
  return contexts;
}

async function generateAndAccept(
  page: Page,
  projectId: string,
  taskId: string,
  media: "image" | "video",
  options?: { candidateCycles?: number; zoomBeforeAccept?: boolean },
): Promise<AcceptedMaskResponse> {
  await page.goto(`/projects/${projectId}/annotate?task=${taskId}`);
  const stage = page.getByTestId(media === "image" ? "workbench-stage" : "video-konva-stage");
  await expect(stage).toBeVisible({ timeout: 20_000 });

  const point = page.getByTestId(
    media === "image" ? "tool-btn-smart-point" : "video-tool-btn-smart-point",
  );
  await expect(point).toBeVisible({ timeout: 15_000 });
  await expect(point).not.toHaveAttribute("aria-disabled", "true");
  await point.click();
  await openContextToolbar(page, "interactive");
  await expect(page.getByTestId("single-frame-output-geometry-select")).toHaveValue("mask");

  const box = await stage.boundingBox();
  if (!box) throw new Error("workbench stage has no bounding box");
  const interactiveResponse = page.waitForResponse((response) =>
    /\/interactive-annotating(?:-frame)?$/.test(new URL(response.url()).pathname),
  );
  await page.mouse.click(box.x + box.width * 0.68, box.y + box.height * 0.48);
  expect((await interactiveResponse).status()).toBe(200);
  const pendingLabel = options?.candidateCycles
    ? `${options.candidateCycles + 1} 个候选待处理`
    : "候选待处理";
  await expect(page.getByText(pendingLabel, { exact: true })).toBeVisible({ timeout: 10_000 });
  // Polygon outlines are immediate; only the active candidate's pixel preview is decoded lazily.
  await page.waitForTimeout(media === "image" ? 250 : 750);
  if (options?.zoomBeforeAccept) {
    const zoomLabel = page.getByText(/^\d+%$/).first();
    const initialZoom = await zoomLabel.textContent();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, 300);
    await page.keyboard.up("Control");
    await expect(zoomLabel).not.toHaveText(initialZoom ?? "", { timeout: 5_000 });
  }
  for (let index = 0; index < (options?.candidateCycles ?? 0); index += 1) {
    await page.keyboard.press("Tab");
  }

  await page.keyboard.press("Enter");
  const picker = page.getByTestId("class-picker-popover");
  await expect(picker).toBeVisible({ timeout: 10_000 });
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/v1/tasks/${taskId}/ai-mask-candidates/accept`) &&
      response.request().method() === "POST",
  );
  await picker.getByText("car", { exact: true }).click();
  const response = await accepted;
  expect(response.status(), await response.text()).toBe(200);
  return response.json() as Promise<AcceptedMaskResponse>;
}

async function expectRleEquals(
  response: APIResponse,
  expected: SeedNativeMaskCandidateData["rle"],
): Promise<void> {
  expect(response.ok(), await response.text()).toBeTruthy();
  expect(await response.json()).toEqual(expected);
}

test.describe("native Mask interactive candidate acceptance", () => {
  test.skip(MATRIX !== "native", "requires the native raster Mask matrix");

  test("image candidate and committed raster Mask keep identical pixels", async ({
    page,
    request,
    seed,
  }) => {
    // Remote HTTP hosts are not secure contexts, so randomUUID is unavailable.
    // Force that browser path even though Playwright's loopback origin is trusted.
    await page.addInitScript(() => {
      Object.defineProperty(globalThis.crypto, "randomUUID", {
        configurable: true,
        value: undefined,
      });
    });
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const fixture = await seed.nativeMaskCandidate(taskId, { variant: "multimask_donut" });
    const routed = await routeNativeCandidate(page, fixture);
    await seed.injectToken(page, data.annotator_email);
    await seed.setPetEnabled(data.annotator_email, true);

    const accepted = await generateAndAccept(page, data.project_id, taskId, "image", {
      candidateCycles: 2,
      zoomBeforeAccept: true,
    });
    expect(accepted.annotation.annotation_type).toBe("raster_mask");
    expect(accepted.prediction.id).toBeTruthy();
    expect(accepted.replayed).toBe(false);
    expect(routed.contexts.at(-1)?.output_geometry).toBe("mask");

    const token = await seed.accessToken(data.annotator_email);
    await expectRleEquals(
      await request.get(`${API_BASE}/api/v1/annotations/${accepted.annotation.id}/mask-content`, {
        headers: { Authorization: `Bearer ${token}` },
      }),
      fixture.rles[2],
    );
    const row = page.getByTestId(`box-list-item-${accepted.annotation.id}`);
    await expect(row).toContainText("3 组件", { timeout: 15_000 });
    await expect(row).toContainText("1 孔洞");

    const stage = page.getByTestId("workbench-stage");
    const stageBox = await stage.boundingBox();
    if (!stageBox) throw new Error("workbench stage has no bounding box");
    const nextPrompt = page.waitForResponse((response) =>
      /\/interactive-annotating$/.test(new URL(response.url()).pathname),
    );
    await page.mouse.click(stageBox.x + stageBox.width * 0.3, stageBox.y + stageBox.height * 0.45);
    expect((await nextPrompt).status()).toBe(200);
    expect(routed.contexts.at(-1)?.mask_prompt_source).toBeUndefined();
  });

  test("model without Mask capability stays on explicit polygon without creating a raster annotation", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const contexts = await routePolygonCandidate(page);
    await seed.injectToken(page, data.annotator_email);
    await seed.setPetEnabled(data.annotator_email, true);

    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("tool-btn-smart-point").click();
    await openContextToolbar(page, "interactive");
    const output = page.getByTestId("single-frame-output-geometry-select");
    await expect(output).toHaveValue("polygon");
    await expect(output).toHaveAttribute("title", "当前模型未声明原生 Mask 输出能力");

    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench stage has no bounding box");
    await page.mouse.click(box.x + box.width * 0.58, box.y + box.height * 0.5);
    await expect(page.getByText("候选待处理", { exact: true })).toBeVisible({ timeout: 10_000 });
    expect(contexts.at(-1)?.output_geometry).toBe("polygon");

    await page.keyboard.press("Enter");
    const picker = page.getByTestId("class-picker-popover");
    const created = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/annotations`) &&
        response.request().method() === "POST" &&
        response.status() === 201,
    );
    await picker.getByText("car", { exact: true }).click();
    await expect(picker).toBeVisible();
    await page.keyboard.press("Enter");
    const createdBody = (await created.then((response) => response.json())) as {
      annotation_type: string;
    };
    expect(createdBody.annotation_type).toBe("polygon");

    const token = await seed.accessToken(data.annotator_email);
    const rows = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(rows.ok(), await rows.text()).toBeTruthy();
    expect(
      ((await rows.json()) as Array<{ annotation_type: string }>).some(
        (annotation) => annotation.annotation_type === "raster_mask",
      ),
    ).toBe(false);
  });

  test("lost accept response retries with the same idempotency key and commits once", async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(60_000);
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const fixture = await seed.nativeMaskCandidate(taskId);
    await routeNativeCandidate(page, fixture);
    await seed.injectToken(page, data.annotator_email);
    await seed.setPetEnabled(data.annotator_email, true);

    let attempts = 0;
    let committedBeforeDisconnect: AcceptedMaskResponse | null = null;
    await page.route(
      new RegExp(`/api/v1/tasks/${taskId}/ai-mask-candidates/accept$`),
      async (route) => {
        attempts += 1;
        if (attempts === 1) {
          const serverResponse = await route.fetch();
          expect(serverResponse.status(), await serverResponse.text()).toBe(200);
          committedBeforeDisconnect = (await serverResponse.json()) as AcceptedMaskResponse;
          await route.abort("failed");
          return;
        }
        await route.continue();
      },
    );

    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("tool-btn-smart-point").click();
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench stage has no bounding box");
    await page.mouse.click(box.x + box.width * 0.63, box.y + box.height * 0.47);
    await expect(page.getByText("候选待处理", { exact: true })).toBeVisible({ timeout: 10_000 });

    await page.keyboard.press("Enter");
    let picker = page.getByTestId("class-picker-popover");
    await picker.getByText("car", { exact: true }).click();
    await expect(page.getByText("原生 Mask 采纳失败", { exact: true })).toBeVisible({
      timeout: 15_000,
    });
    expect(committedBeforeDisconnect).not.toBeNull();

    await page.keyboard.press("Enter");
    picker = page.getByTestId("class-picker-popover");
    await expect(picker).toBeVisible();
    const replay = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/ai-mask-candidates/accept`) &&
        response.request().method() === "POST",
    );
    await picker.getByText("car", { exact: true }).click();
    const replayResponse = await replay;
    expect(replayResponse.status(), await replayResponse.text()).toBe(200);
    const replayed = (await replayResponse.json()) as AcceptedMaskResponse;
    expect(replayed.replayed).toBe(true);
    expect(replayed.annotation.id).toBe(committedBeforeDisconnect?.annotation.id);
    expect(replayed.prediction.id).toBe(committedBeforeDisconnect?.prediction.id);
    expect(attempts).toBe(2);

    const token = await seed.accessToken(data.annotator_email);
    const rows = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(rows.ok(), await rows.text()).toBeTruthy();
    const matching = ((await rows.json()) as Array<{ id: string }>).filter(
      (annotation) => annotation.id === replayed.annotation.id,
    );
    expect(matching).toHaveLength(1);
  });

  test("video current-frame candidate and committed keyframe keep identical pixels", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const { task_id: taskId } = await seed.videoTask(data.project_id);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const fixture = await seed.nativeMaskCandidate(taskId);
    const routed = await routeNativeCandidate(page, fixture);
    await seed.injectToken(page, data.annotator_email);
    await seed.setPetEnabled(data.annotator_email, true);

    const accepted = await generateAndAccept(page, data.project_id, taskId, "video");
    expect(accepted.annotation.annotation_type).toBe("video_track_mask");
    expect(routed.contexts.at(-1)?.output_geometry).toBe("mask");

    const token = await seed.accessToken(data.annotator_email);
    await expectRleEquals(
      await request.get(`${API_BASE}/api/v1/annotations/${accepted.annotation.id}/mask-content/0`, {
        headers: { Authorization: `Bearer ${token}` },
      }),
      fixture.rle,
    );
    await page.getByLabel("收起浮窗").click();
    await expect(page.getByLabel(/展开选中信息卡.*可拖动/)).toBeVisible();
    await page.getByTitle("AI 延展 Mask 轨迹").click();
    await expect(page.getByTestId("video-tracker-propagate-dialog")).toBeVisible();
  });

  test("video drift frame correction propagates backward and accepts a local window", async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(60_000);
    const data = await seed.owned();
    const { task_id: taskId } = await seed.videoTask(data.project_id);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const fixture = await seed.nativeMaskCandidate(taskId);
    await routeNativeCandidate(page, fixture);
    await seed.injectToken(page, data.annotator_email);
    await seed.setPetEnabled(data.annotator_email, true);

    const accepted = await generateAndAccept(page, data.project_id, taskId, "video");
    const annotationId = accepted.annotation.id;
    const token = await seed.accessToken(data.annotator_email);
    const segmentsResponse = await request.get(
      `${API_BASE}/api/v1/tasks/${taskId}/video/segments`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(segmentsResponse.ok(), await segmentsResponse.text()).toBeTruthy();
    const segments = (await segmentsResponse.json()) as {
      segments: Array<{ id: string; start_frame: number; end_frame: number }>;
    };
    const segment = segments.segments.find((item) => item.start_frame <= 2 && item.end_frame >= 2);
    if (!segment) throw new Error("video correction segment is missing");
    const claimResponse = await request.post(
      `${API_BASE}/api/v1/tasks/${taskId}/video/segments/${segment.id}:claim`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(claimResponse.ok(), await claimResponse.text()).toBeTruthy();
    const jobId = "00000000-0000-4000-8000-000000000238";
    let correctionPayload: Record<string, unknown> | null = null;
    let decisionPayload: Record<string, unknown> | null = null;
    let resolveCandidate!: (value: {
      saved: VideoMaskAnnotationResponse;
      mask: SeedNativeMaskCandidateData["rle"];
    }) => void;
    const candidateReady = new Promise<{
      saved: VideoMaskAnnotationResponse;
      mask: SeedNativeMaskCandidateData["rle"];
    }>((resolve) => {
      resolveCandidate = resolve;
    });

    const jobBody = (status: "pending_review" | "accepted") => ({
      id: jobId,
      task_id: taskId,
      dataset_item_id: "00000000-0000-4000-8000-000000000239",
      annotation_id: annotationId,
      segment_id: correctionPayload?.segment_id ?? null,
      created_by: null,
      status,
      job_kind: "correction",
      track_id_snapshot: "e2e-mask-track",
      correction_frame: correctionPayload?.correction_frame ?? 2,
      revision: 1,
      model_key: "sam2_video",
      direction: "backward",
      from_frame: correctionPayload?.from_frame ?? 0,
      to_frame: correctionPayload?.to_frame ?? 2,
      prompt: {},
      event_channel: `video-tracker-job:${jobId}`,
      celery_task_id: null,
      cancel_requested_at: null,
      started_at: null,
      completed_at: status === "accepted" ? new Date().toISOString() : null,
      error_message: null,
      created_at: new Date().toISOString(),
      updated_at: null,
    });

    await page.route(
      new RegExp(`/api/v1/tasks/${taskId}/video/tracks/${annotationId}/correction-jobs$`),
      async (route) => {
        correctionPayload = route.request().postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify(jobBody("pending_review")),
        });
      },
    );
    await page.route(new RegExp(`/api/v1/video-tracker-jobs/${jobId}$`), (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(jobBody("pending_review")),
      }),
    );
    await page.route(new RegExp(`/api/v1/video-tracker-jobs/${jobId}/preview$`), async (route) => {
      const { saved } = await candidateReady;
      const correctionFrame = Number(correctionPayload?.correction_frame ?? 2);
      const keyframe = saved.geometry.keyframes.find(
        (item) => item.frame_index === correctionFrame,
      );
      if (!keyframe) throw new Error("saved correction keyframe is missing");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          job_id: jobId,
          status: "pending_review",
          annotation_id: annotationId,
          job_kind: "correction",
          correction_frame: correctionFrame,
          direction: "backward",
          from_frame: correctionPayload?.from_frame ?? 0,
          to_frame: correctionPayload?.to_frame ?? correctionFrame,
          fallback_reason: null,
          seed_mode: "native_mask",
          protect_manual: true,
          results: [
            {
              frame_index: correctionFrame - 1,
              instance_id: "1",
              candidate_key: `1:${correctionFrame - 1}`,
              source_annotation_id: annotationId,
              target_annotation_id: annotationId,
              manual_protected: false,
              geometry: { type: "mask", mask: keyframe.mask },
            },
          ],
          grid_step: 1,
          output_geometry: "mask",
          job_revision: 1,
          expected_source_versions: { [annotationId]: saved.version },
          candidate_total: 1,
          candidate_pending: 1,
          candidate_accepted: 0,
          candidate_rejected: 0,
        }),
      });
    });
    await page.route(
      new RegExp(`/api/v1/video-tracker-jobs/${jobId}/mask-content/[a-f0-9]{64}$`),
      async (route) => {
        const { mask } = await candidateReady;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(mask),
        });
      },
    );
    await page.route(
      new RegExp(`/api/v1/video-tracker-jobs/${jobId}/decisions$`),
      async (route) => {
        decisionPayload = route.request().postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(jobBody("accepted")),
        });
      },
    );

    const row = page.getByTestId(`video-mask-track-${annotationId}`);
    await expect(row).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: "收起浮窗" }).click();
    await row.click();
    await page.getByLabel(/展开选中信息卡.*可拖动/).click();
    // The selected track command intentionally owns arrow keys while focused;
    // return focus to the workbench before testing global frame navigation.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(page.getByText(/F 2 \//)).toBeVisible({ timeout: 10_000 });
    await page.getByTitle("编辑当前帧 Mask").click();
    await openMaskSettings(page);
    const toolbar = page.getByTestId("mask-toolbar");
    await expect(toolbar).toContainText("就绪", { timeout: 15_000 });
    await closeMaskSettings(page);

    const stage = page.getByTestId("video-konva-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("video stage has no bounding box");
    await page.mouse.move(box.x + box.width * 0.48, box.y + box.height * 0.48);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.56, box.y + box.height * 0.54, { steps: 6 });
    await page.mouse.up();
    await openMaskSettings(page);
    await expect(toolbar).toContainText("未保存");
    await toolbar.getByRole("button", { name: "保存并传播" }).click();

    const dialog = page.getByRole("dialog", { name: "保存 Mask 纠错帧" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("radio", { name: "← 更早帧" }).click();
    const saveResponsePromise = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith(`/api/v1/tasks/${taskId}/video/tracks/${annotationId}/mask-keyframes/2`) &&
        response.request().method() === "PUT",
    );
    await dialog.getByRole("button", { name: "保存并启动传播" }).click();
    const saveResponse = await saveResponsePromise;
    expect(saveResponse.status(), await saveResponse.text()).toBe(200);
    const saved = (await saveResponse.json()) as VideoMaskAnnotationResponse;
    await expect.poll(() => correctionPayload).not.toBeNull();
    const correctionFrame = Number(correctionPayload?.correction_frame);
    const savedMask = saved.geometry.keyframes.find(
      (item) => item.frame_index === correctionFrame,
    )?.mask;
    expect(savedMask).toBeTruthy();
    expect(saved.geometry.keyframes.find((item) => item.frame_index === 2)?.source).toBe("manual");
    expect(correctionPayload).toMatchObject({
      correction_frame: 2,
      direction: "backward",
      model_key: "sam2_video",
      model_id: "grounded-sam2-tracker",
      source_annotation_version: saved.version,
      corrected_mask_digest: savedMask?.sha256,
    });

    const maskResponse = await request.get(
      `${API_BASE}/api/v1/annotations/${annotationId}/mask-content/${correctionFrame}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(maskResponse.ok(), await maskResponse.text()).toBeTruthy();
    resolveCandidate({
      saved,
      mask: (await maskResponse.json()) as SeedNativeMaskCandidateData["rle"],
    });

    const capsule = page.getByTestId("tracker-review-tool-capsule");
    await expect(capsule).toContainText("Mask 纠错", { timeout: 15_000 });
    const review = page.getByTestId("video-tracker-review-bar");
    await expect(review).toBeHidden();
    await openContextToolbar(page, "tracker-review");
    await expect(review.getByTestId("tracker-review-correction-summary")).toContainText(
      "向更早帧 · 原生 Mask seed · 保护人工帧",
    );
    await review.getByTestId("tracker-review-accept").click();
    await expect(review).toBeHidden();
    expect(decisionPayload).toMatchObject({
      instance_ids: ["1"],
      from_frame: 1,
      to_frame: 1,
      decision: "accept",
      override_manual: false,
      expected_source_versions: { [annotationId]: saved.version },
      job_revision: 1,
    });
  });

  test("saved Mask negative scribble survives a failed prompt retry and refresh", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
    const source = await seed.injectRasterMask({
      taskId,
      userEmail: data.annotator_email,
    });
    const fixture = await seed.nativeMaskCandidate(taskId, {
      variant: "negative_scribble",
      promptFamily: "scribble",
      negativeScribbles: 1,
      promptSource: {
        annotationId: source.annotation_id,
        sourceVersion: 1,
        sourceDigest: source.mask.sha256,
      },
    });
    fixture.response.mask_input_next = "opaque-e2e-mask-session";
    const routed = await routeNativeCandidate(page, fixture, { failFirst: true });
    await seed.injectToken(page, data.annotator_email);
    await seed.setPetEnabled(data.annotator_email, true);

    const token = await seed.accessToken(data.annotator_email);
    const before = await request.get(
      `${API_BASE}/api/v1/annotations/${source.annotation_id}/mask-content`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect((await before.json()).counts).not.toEqual(fixture.rle.counts);

    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toBeVisible({ timeout: 20_000 });
    await page.getByTestId(`box-list-item-${source.annotation_id}`).click();
    const point = page.getByTestId("tool-btn-smart-point");
    await expect(point).not.toHaveAttribute("aria-disabled", "true");
    await point.click();
    await expect(page.getByTestId("mask-prompt-source")).toContainText("精修 Mask");
    await openContextToolbar(page, "interactive");
    const polarity = page.getByTestId("ai-tool-polarity");
    await polarity.click();
    await expect(polarity).toHaveAttribute("title", /负向/);

    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench stage has no bounding box");
    const pointContexts = () =>
      routed.contexts.filter(
        (context) =>
          context.type === "point" &&
          (context.mask_prompt_source as { annotation_id?: unknown } | undefined)?.annotation_id ===
            source.annotation_id,
      );
    await page.mouse.click(box.x + box.width * 0.52, box.y + box.height * 0.48);
    await expect.poll(() => pointContexts().length, { timeout: 10_000 }).toBe(1);
    expect(pointContexts().at(-1)).toMatchObject({
      labels: [0],
      mask_prompt_source: { annotation_id: source.annotation_id },
    });

    const scribble = page.getByTestId("tool-btn-smart-scribble");
    await expect(scribble).not.toHaveAttribute("aria-disabled", "true");
    await scribble.click();
    await expect(page.getByTestId("interactive-summary-polarity")).toHaveAttribute(
      "aria-label",
      "负向笔迹",
    );
    await page.mouse.move(box.x + box.width * 0.46, box.y + box.height * 0.46);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.62, box.y + box.height * 0.54, { steps: 8 });
    await page.mouse.up();
    const scribbleContexts = () => routed.contexts.filter((context) => context.type === "scribble");
    await expect.poll(() => scribbleContexts().length, { timeout: 10_000 }).toBe(1);

    // Hover reveals retry without taking keyboard focus from the canvas.
    await page.getByTestId("interactive-settings-trigger").hover();
    const retry = page.getByTestId("interactive-prompt-retry");
    await expect(retry).toBeVisible({ timeout: 10_000 });
    const staleAcceptRequests: string[] = [];
    const captureAccept = (request: Request) => {
      if (request.url().endsWith(`/api/v1/tasks/${taskId}/ai-mask-candidates/accept`)) {
        staleAcceptRequests.push(request.url());
      }
    };
    page.on("request", captureAccept);
    expect(
      await page.evaluate(
        () =>
          document.activeElement?.closest(
            "[data-workbench-context-toolbar], [data-workbench-context-primary], [data-workbench-context-toolbar-trigger]",
          ) != null,
      ),
    ).toBe(false);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeHidden();
    await page.waitForTimeout(200);
    page.off("request", captureAccept);
    expect(staleAcceptRequests).toEqual([]);

    await retry.click();
    await expect.poll(() => scribbleContexts().length, { timeout: 10_000 }).toBe(2);
    await expect(page.locator('[data-pet-mood="candidateReady"]')).toBeVisible({ timeout: 10_000 });

    expect(scribbleContexts()[1]).toEqual(scribbleContexts()[0]);
    expect(scribbleContexts()[1]?.mask_prompt_source).toMatchObject({
      annotation_id: source.annotation_id,
    });
    expect(scribbleContexts()[1]?.mask_input).toBe("opaque-e2e-mask-session");
    expect(scribbleContexts()[1]?.scribbles).toEqual([expect.objectContaining({ polarity: 0 })]);

    await page.keyboard.press("Enter");
    const picker = page.getByTestId("class-picker-popover");
    await expect(picker).toBeVisible({ timeout: 10_000 });
    const accepted = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/tasks/${taskId}/ai-mask-candidates/accept`) &&
        response.request().method() === "POST",
    );
    await picker.getByText("car", { exact: true }).click();
    const acceptedResponse = await accepted;
    expect(acceptedResponse.status()).toBe(200);
    const acceptedBody = (await acceptedResponse.json()) as AcceptedMaskResponse;
    expect(acceptedBody.annotation.id).toBe(source.annotation_id);

    await expectRleEquals(
      await request.get(`${API_BASE}/api/v1/annotations/${source.annotation_id}/mask-content`, {
        headers: { Authorization: `Bearer ${token}` },
      }),
      fixture.rle,
    );
    await page.reload();
    await expect(page.getByTestId(`box-list-item-${source.annotation_id}`)).toBeVisible({
      timeout: 15_000,
    });
    await expectRleEquals(
      await request.get(`${API_BASE}/api/v1/annotations/${source.annotation_id}/mask-content`, {
        headers: { Authorization: `Bearer ${token}` },
      }),
      fixture.rle,
    );
  });
});
