import { expectStableContextCapsule, openContextToolbar } from "../fixtures/context-toolbar";
import { openMaskSettings } from "../fixtures/mask-toolbar";
import type { APIRequestContext, APIResponse, Page, Request } from "@playwright/test";
import { expect, test, type SeedAPI, type SeedNativeMaskCandidateData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const native = process.env.PLAYWRIGHT_RASTER_MASK_MATRIX === "native";
type Media = "image" | "video";
const setupPattern = /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/setup/;
const inferencePattern =
  /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/interactive-annotating(?:-frame)?$/;
const prompts = ["point", "interactive_box", "exemplar", "mask", "scribble"];
const inputs = ["full_image", "point_prompt", "bbox_prompt", "mask_prompt", "scribble_prompt"];
const model = (id: string) => ({
  id,
  display_name: id,
  task: "interactive_seg",
  model_family: "e2e",
  composition: "atom",
  is_interactive: true,
  supported_prompts: prompts,
  supported_inputs: inputs,
  supported_geometric_outputs: ["polygon", "mask"],
  resource_profile: { device: "cpu", batchable: false },
  supported_variants: [
    {
      key: "size",
      title: "档位",
      variants: [
        { value: "small", label: "小" },
        { value: "large", label: "大" },
      ],
    },
  ],
  default_variants: { size: "small" },
});
const setup = {
  name: "E1 确定性能力夹具",
  version: "1",
  is_interactive: true,
  supported_prompts: prompts,
  supported_inputs: inputs,
  supported_geometric_outputs: ["polygon", "mask"],
  models: [model("e2e-native-mask"), model("e2e-alternate-model")],
};
const polygonResponse = {
  output_geometry: "polygon",
  model_version: "e2e-toolbar",
  result: [
    {
      type: "polygonlabels",
      value: {
        points: [
          [0.4, 0.3],
          [0.7, 0.3],
          [0.7, 0.65],
          [0.4, 0.65],
        ],
        polygonlabels: ["car"],
      },
      score: 0.9,
    },
  ],
};
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}
function contextOf(request: Request): Record<string, unknown> {
  if (request.headers()["content-type"]?.includes("application/json")) {
    return (request.postDataJSON() as { context: Record<string, unknown> }).context;
  }
  const match = request.postData()?.match(/name="context"\r?\n\r?\n([^\r\n]+)/);
  if (!match) throw new Error("video prompt has no context");
  return JSON.parse(match[1]) as Record<string, unknown>;
}
async function routeML(page: Page, response: unknown) {
  const state = {
    capabilityFailure: false,
    failNextPrompt: false,
    calls: [] as Array<{ url: string; context: Record<string, unknown> }>,
    warmups: [] as string[],
  };
  await page.route(setupPattern, (route) =>
    route.fulfill({
      status: state.capabilityFailure ? 503 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        state.capabilityFailure ? { detail: "E1 capability unavailable" } : setup,
      ),
    }),
  );
  await page.route(inferencePattern, (route) => {
    const context = contextOf(route.request());
    // Existing warmup sends a center point without model/output defaults and discards its result.
    if (!("output_geometry" in context)) {
      state.warmups.push(route.request().url());
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(response),
      });
    }
    state.calls.push({ url: route.request().url(), context });
    const fail = state.failNextPrompt;
    state.failNextPrompt = false;
    return route.fulfill({
      status: fail ? 503 : 200,
      contentType: "application/json",
      body: JSON.stringify(fail ? { detail: "E1 inference unavailable" } : response),
    });
  });
  return state;
}
async function fixtureFor(seed: SeedAPI, media: Media) {
  const data = await seed.owned();
  await seed.configureRasterMask(data.project_id, true);
  const taskId =
    media === "video" ? (await seed.videoTask(data.project_id)).task_id : data.task_ids[0];
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  return { data, taskId, token: await seed.accessToken(data.annotator_email) };
}
async function openWorkbench(
  page: Page,
  seed: SeedAPI,
  data: { project_id: string; annotator_email: string },
  taskId: string,
  media: Media,
) {
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(
    page.getByTestId(media === "image" ? "workbench-stage" : "video-konva-stage"),
  ).toBeVisible({ timeout: 20_000 });
}
async function selectTool(page: Page, media: Media, tool: string) {
  const button = page.getByTestId(`${media === "video" ? "video-" : ""}tool-btn-${tool}`);
  await expect(button).toBeVisible({ timeout: 15_000 });
  await expect(button).not.toHaveAttribute("aria-disabled", "true");
  await button.click();
  await expect(page.getByTestId("interactive-toolbar-advanced")).toBeHidden();
  if (await page.getByTestId("interactive-settings-trigger").isVisible())
    await page.getByTestId("interactive-settings-trigger").hover();
}
async function prompt(page: Page, media: Media, tool: "point" | "box" | "exemplar" | "scribble") {
  const stage = page.getByTestId(media === "image" ? "workbench-stage" : "video-konva-stage");
  const box = await stage.boundingBox();
  if (!box) throw new Error("missing stage bounds");
  const next = page.waitForResponse(inferencePattern);
  if (tool === "point") await page.mouse.click(box.x + box.width * 0.63, box.y + box.height * 0.57);
  else {
    await page.mouse.move(box.x + box.width * 0.44, box.y + box.height * 0.44);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.68, box.y + box.height * 0.65, { steps: 8 });
    await page.mouse.up();
  }
  const response = await next;
  await expect(page.getByTestId("interactive-toolbar")).toBeHidden();
  await page.getByTestId("interactive-settings-trigger").hover();
  return response;
}
async function acceptCurrent(page: Page, taskId: string) {
  await expect(page.getByTestId("interactive-candidate-accept")).toBeEnabled();
  await page.getByTestId("interactive-candidate-accept").click();
  const picker = page.getByTestId("class-picker-popover");
  await expect(picker).toBeVisible();
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/tasks/${taskId}/ai-mask-candidates/accept`) &&
      response.request().method() === "POST",
  );
  await picker.getByText("car", { exact: true }).click();
  return json<{ annotation: { id: string; annotation_type: string } }>(await accepted);
}
async function persistedPixels(
  page: Page,
  request: APIRequestContext,
  id: string,
  token: string,
  media: Media,
  expected: SeedNativeMaskCandidateData["rle"],
) {
  await page.reload();
  await expect(
    page.getByTestId(`${media === "video" ? "video-mask-track" : "box-list-item"}-${id}`),
  ).toBeVisible({ timeout: 20_000 });
  expect(
    await json(
      await request.get(
        `${API_BASE}/api/v1/annotations/${id}/mask-content${media === "video" ? "/0" : ""}`,
        { headers: auth(token) },
      ),
    ),
  ).toEqual(expected);
}

test.describe("E1 interactive toolbar layers", () => {
  test.skip(!native, "requires native Mask writes");
  test.use({ viewport: { width: 1440, height: 1080 } });

  for (const media of ["image", "video"] as const) {
    test(`${media}: collapsed prompt inputs and candidate decisions keep native pixels`, async ({
      page,
      request,
      seed,
    }) => {
      const { data, taskId, token } = await fixtureFor(seed, media);
      const fixture = await seed.nativeMaskCandidate(taskId, { variant: "multimask_donut" });
      const routed = await routeML(page, fixture.response);
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await openWorkbench(page, seed, data, taskId, media);
      await selectTool(page, media, "smart-point");
      await page.getByTestId("interactive-settings-trigger").click();
      await expect(page.getByRole("button", { name: "切换到负向提示", exact: true })).toBeVisible();
      expect((await prompt(page, media, "point")).status()).toBe(200);
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/1\s*\/\s*3/);
      await page.getByTestId("interactive-candidate-next").click();
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/2\s*\/\s*3/);
      await page.getByTestId("interactive-candidate-previous").click();
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/1\s*\/\s*3/);
      // Native button Enter must invoke that button, without accepting the candidate behind it.
      await expectStableContextCapsule(page, "interactive");
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").press("Enter");
      await expect(page.getByTestId("interactive-toolbar-advanced")).toBeVisible();
      await expect(page.getByTestId("class-picker-popover")).toBeHidden();
      await page.getByTestId("ai-tool-model-select").press("Tab");
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/1\s*\/\s*3/);
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").click();
      expect(routed.calls).toHaveLength(1);
      await page.getByTestId("interactive-candidate-cancel").click();
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/0\s*\/\s*0/);
      await selectTool(page, media, "smart-box");
      expect((await prompt(page, media, "box")).status()).toBe(200);
      expect(routed.calls.at(-1)?.context.type).toBe("interactive_box");
      await page.getByTestId("interactive-candidate-cancel").click();
      await selectTool(page, media, "exemplar");
      await page.getByTestId("exemplar-text").getByRole("textbox").fill("car");
      await openContextToolbar(page, "interactive");
      await page.getByTestId("exemplar-output-mode-select").selectOption("mask");
      expect((await prompt(page, media, "exemplar")).status()).toBe(200);
      expect(routed.calls.at(-1)?.context).toMatchObject({
        type: "exemplar",
        text: "car",
        output: "mask",
      });
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/1\s*\/\s*3/);
      await page.getByTestId("interactive-candidate-next").click();
      const accepted = await acceptCurrent(page, taskId);
      await page.getByTestId("interactive-settings-trigger").hover();
      await expect(page.getByTestId("interactive-candidate-count")).toContainText(/\d\s*\/\s*2/);
      await page.getByTestId("interactive-candidate-cancel").click();
      expect(errors).toEqual([]);
      await persistedPixels(page, request, accepted.annotation.id, token, media, fixture.rles[1]);
    });

    test(`${media}: advanced backend model and variant persist and reach the request`, async ({
      page,
      request,
      seed,
    }) => {
      const { data, taskId } = await fixtureFor(seed, media);
      const adminToken = await seed.accessToken(data.admin_email);
      const second = await json<{ id: string }>(
        await request.post(`${API_BASE}/api/v1/projects/${data.project_id}/ml-backends`, {
          headers: auth(adminToken),
          data: {
            name: "E1 第二测试后端",
            url: "http://second-toolbar.e2e:9999",
            is_interactive: true,
            extra_params: { e2e_mock: true },
          },
        }),
      );
      const routed = await routeML(page, polygonResponse);
      await openWorkbench(
        page,
        seed,
        { ...data, annotator_email: data.admin_email },
        taskId,
        media,
      );
      await selectTool(page, media, "smart-point");
      await openContextToolbar(page, "interactive");
      await page.getByTestId("single-frame-output-geometry-select").selectOption("polygon");
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").click();
      await page.getByTestId("ai-tool-backend-select").selectOption(second.id);
      await page.getByTestId("ai-tool-model-select").selectOption("e2e-alternate-model");
      const savedProject = page.waitForResponse(
        (response) =>
          response.url().endsWith(`/projects/${data.project_id}`) &&
          response.request().method() === "PATCH",
      );
      await page.getByTestId("ai-variant-size").selectOption("large");
      expect((await savedProject).ok()).toBe(true);
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").click();
      expect((await prompt(page, media, "point")).status()).toBe(200);
      expect(routed.calls.at(-1)).toMatchObject({
        url: expect.stringContaining(`/ml-backends/${second.id}/`),
        context: {
          model_id: "e2e-alternate-model",
          model_variants: { size: "large" },
          output_geometry: "polygon",
        },
      });
      const count = routed.calls.length;
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").click();
      await expect(page.getByTestId("ai-tool-backend-select")).toHaveValue(second.id);
      await expect(page.getByTestId("ai-tool-model-select")).toHaveValue("e2e-alternate-model");
      await expect(page.getByTestId("ai-variant-size")).toHaveValue("large");
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").click();
      await expect
        .poll(async () => {
          const prefs = await json<{
            ai?: {
              model_by_backend?: Record<string, string>;
              interactive_backend_by_project?: Record<string, string>;
            };
          }>(
            await request.get(`${API_BASE}/api/v1/auth/me/preferences`, {
              headers: auth(adminToken),
            }),
          );
          return [
            prefs.ai?.model_by_backend?.[second.id],
            prefs.ai?.interactive_backend_by_project?.[data.project_id],
          ];
        })
        .toEqual(["e2e-alternate-model", second.id]);
      expect(routed.calls).toHaveLength(count);
      await page.reload();
      await selectTool(page, media, "smart-point");
      await openContextToolbar(page, "interactive");
      await page.getByTestId("interactive-toolbar-advanced-toggle").click();
      await expect(page.getByTestId("ai-tool-backend-select")).toHaveValue(second.id);
      await expect(page.getByTestId("ai-tool-model-select")).toHaveValue("e2e-alternate-model");
      await expect(page.getByTestId("ai-variant-size")).toHaveValue("large");
      expect(routed.calls).toHaveLength(count);
    });
  }

  test("capability negotiation and inference failures keep separate recovery actions", async ({
    page,
    request,
    seed,
  }) => {
    const { data, taskId, token } = await fixtureFor(seed, "image");
    const fixture = await seed.nativeMaskCandidate(taskId);
    const routed = await routeML(page, fixture.response);
    routed.capabilityFailure = true;
    await openWorkbench(page, seed, data, taskId, "image");
    await page.getByTestId("interactive-settings-trigger").hover();
    await expect(page.getByTestId("interactive-capability-error")).toContainText(
      "E1 capability unavailable",
    );
    await expect(page.getByTestId("interactive-inference-error")).toBeHidden();
    await expect(page.getByTestId("interactive-toolbar-advanced")).toBeHidden();
    await selectTool(page, "image", "mask");
    await openMaskSettings(page);
    await expect(page.getByTestId("mask-toolbar")).toContainText("就绪");
    await expect(page.getByTestId("interactive-toolbar")).toBeHidden();
    await page.getByTestId("mask-toolbar").getByTitle("Mask 高级工具").click();
    await expect(page.getByRole("menuitem", { name: "直线切割为两个实例" })).toBeVisible();
    await page.keyboard.press("Escape");
    await selectTool(page, "image", "select");
    await page.getByTestId("interactive-settings-trigger").hover();
    await expect(page.getByTestId("interactive-capability-error")).toBeVisible();
    routed.capabilityFailure = false;
    await page.getByTestId("interactive-capability-retry").click();
    await expect(page.getByTestId("interactive-capability-error")).toBeHidden();
    expect(routed.calls).toHaveLength(0);
    await selectTool(page, "image", "smart-point");
    routed.failNextPrompt = true;
    expect((await prompt(page, "image", "point")).status()).toBe(503);
    await expect(page.getByTestId("interactive-inference-error")).toContainText(
      "E1 inference unavailable",
    );
    await expect(page.getByTestId("interactive-capability-error")).toBeHidden();
    await expect(page.getByTestId("interactive-toolbar-advanced")).toBeHidden();
    const retried = page.waitForResponse(inferencePattern);
    await page.getByTestId("interactive-prompt-retry").click();
    expect((await retried).ok()).toBe(true);
    await expect(page.getByTestId("interactive-inference-error")).toBeHidden();
    expect(routed.calls[1].context).toEqual(routed.calls[0].context);
    const accepted = await acceptCurrent(page, taskId);
    await persistedPixels(page, request, accepted.annotation.id, token, "image", fixture.rle);
  });

  test("saved Mask scribble keeps polarity and recovery visible with advanced controls folded", async ({
    page,
    request,
    seed,
  }) => {
    const { data, taskId, token } = await fixtureFor(seed, "image");
    const source = await seed.injectRasterMask({ taskId, userEmail: data.annotator_email });
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
    const routed = await routeML(page, fixture.response);
    await openWorkbench(page, seed, data, taskId, "image");
    await page.getByTestId(`box-list-item-${source.annotation_id}`).click();
    await selectTool(page, "image", "smart-scribble");
    await expect(page.getByTestId("mask-prompt-source")).toBeVisible();
    await page.getByTestId("interactive-settings-trigger").click();
    await page.getByRole("button", { name: "切换到负向提示", exact: true }).click();
    routed.failNextPrompt = true;
    expect((await prompt(page, "image", "scribble")).status()).toBe(503);
    await expect(page.getByTestId("interactive-inference-error")).toBeVisible();
    await page.getByTestId("interactive-prompt-retry").click();
    await expect(page.getByTestId("interactive-candidate-accept")).toBeEnabled();
    expect(routed.calls).toHaveLength(2);
    expect(routed.calls[1].context).toEqual(routed.calls[0].context);
    expect(routed.calls[1].context).toMatchObject({
      type: "scribble",
      mask_prompt_source: { annotation_id: source.annotation_id },
      scribbles: [expect.objectContaining({ polarity: 0 })],
    });
    const accepted = await acceptCurrent(page, taskId);
    expect(accepted.annotation.id).toBe(source.annotation_id);
    await persistedPixels(page, request, source.annotation_id, token, "image", fixture.rle);
  });
});
