import type { APIRequestContext } from "@playwright/test";
import { expect, test as base, type FilteringSeedManifest, type SeedAPI } from "../fixtures/seed";
import { resetFiltering } from "../fixtures/filtering";
import { annotationPaintedPixels } from "../fixtures/annotation-canvas-pixels";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:18110";
const test = base.extend<{ filtering: FilteringSeedManifest }>({
  filtering: async ({ seed }, provideFixture) => {
    await provideFixture(await resetFiltering(seed, { videoReady: true }));
  },
});
test.setTimeout(120_000);

async function importCandidates(
  request: APIRequestContext,
  seed: SeedAPI,
  filtering: FilteringSeedManifest,
  taskId: string,
  frames: number[],
) {
  const token = await seed.accessToken(filtering.user_emails.admin);
  const headers = { Authorization: "Bearer " + token };
  const taskResponse = await request.get(API_BASE + "/api/v1/tasks/" + taskId, { headers });
  expect(taskResponse.ok()).toBe(true);
  const task = await taskResponse.json();
  const response = await request.post(
    API_BASE +
      "/api/v1/projects/" +
      filtering.video.project_id +
      "/predictions/import?format=aap_json&dry_run=false",
    {
      headers,
      multipart: {
        file: {
          name: "filter-workbench-candidates.json",
          mimeType: "application/json",
          buffer: Buffer.from(
            JSON.stringify({
              schema_version: "1.3",
              tasks: [
                {
                  task_match: { display_id: task.display_id },
                  media_type: "video",
                  predictions: frames.map((frame, index) => ({
                    class_name: "car",
                    confidence: 0.99,
                    shapes: [
                      {
                        type: "video_bbox",
                        frame_index: frame,
                        x: 0.62 + index * 0.18,
                        y: 0.62,
                        w: 0.12,
                        h: 0.15,
                      },
                    ],
                  })),
                },
              ],
            }),
          ),
        },
      },
    },
  );
  expect(response.ok(), await response.text()).toBe(true);
  expect(await response.json()).toMatchObject({ imported: frames.length, errors: [] });
  return { headers, displayId: task.display_id as string };
}

test("current-frame display keeps the explicit all-loaded-frame acceptance scope", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const taskId = filtering.video.task_ids.neither;
  const { headers } = await importCandidates(request, seed, filtering, taskId, [0, 10]);
  await seed.advanceTask({
    taskId,
    toStatus: "pending",
    annotatorEmail: filtering.user_emails.anno,
  });
  await seed.injectToken(page, filtering.user_emails.anno);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(
    "/projects/" + filtering.video.project_id + "/annotate?task=" + taskId + "&frame=0",
  );
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(1);
  await page.getByTestId("workbench-ai-single").click();
  await page.getByTestId("ai-prediction-advanced-toggle").click();
  const batch = page.getByTestId("ai-prediction-accept-all");
  await expect(batch).toBeEnabled();
  await expect(batch).toHaveText("采纳已加载候选（2）");
  await expect(page.getByTestId("ai-prediction-bulk-scope")).toContainText("其他帧");
  const accepted: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/predictions\/[^/]+\/accept(?:\?|$)/.test(request.url())) {
      accepted.push(request.url());
    }
  });
  await batch.click();
  await expect.poll(() => accepted.length).toBe(2);
  await expect
    .poll(async () => {
      const response = await request.get(API_BASE + "/api/v1/tasks/" + taskId + "/annotations", {
        headers,
      });
      expect(response.ok()).toBe(true);
      const annotations = await response.json();
      return annotations
        .filter(
          (annotation: { geometry: { type: string } }) => annotation.geometry.type === "video_bbox",
        )
        .map((annotation: { geometry: { frame_index: number } }) => annotation.geometry.frame_index)
        .sort((a: number, b: number) => a - b);
    })
    .toEqual([0, 10]);
  expect(errors).toEqual([]);
});

test("source and frame display filters reset on task switch without annotation writes", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const firstId = filtering.video.task_ids.neither;
  const nextId = filtering.video.task_ids.tracker_only;
  await importCandidates(request, seed, filtering, firstId, [0, 10]);
  const next = await importCandidates(request, seed, filtering, nextId, [0, 10]);
  for (const taskId of [firstId, nextId]) {
    await seed.advanceTask({
      taskId,
      toStatus: "pending",
      annotatorEmail: filtering.user_emails.anno,
    });
  }
  await seed.injectToken(page, filtering.user_emails.anno);
  await page.goto(
    "/projects/" + filtering.video.project_id + "/annotate?task=" + firstId + "&frame=0",
  );
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(1);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      ["POST", "PATCH", "DELETE"].includes(request.method()) &&
      /\/(annotations|predictions)(?:\/|\?)/.test(request.url())
    ) {
      writes.push(request.url());
    }
  });
  await page
    .locator('[aria-label="帧过滤"]')
    .getByRole("button", { name: "全部", exact: true })
    .click();
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(2);
  await page
    .locator('[aria-label="预测来源筛选"]')
    .getByRole("checkbox", { name: /导入/ })
    .uncheck();
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(0);
  await page
    .getByRole("tabpanel", { name: "任务队列", exact: true })
    .getByText(next.displayId, { exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp("task=" + nextId));
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(1);
  await expect(
    page.locator('[aria-label="预测来源筛选"]').getByRole("checkbox", { name: /导入/ }),
  ).toBeChecked();
  expect(writes).toEqual([]);
});

test("persisted image hide remains restorable and does not change geometry", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const taskId = filtering.image.task_ids.same_object;
  const annotationId = filtering.image.object_ids.same_object[0];
  const token = await seed.accessToken(filtering.user_emails.admin);
  const headers = { Authorization: "Bearer " + token };
  const response = await request.get(API_BASE + "/api/v1/tasks/" + taskId + "/annotations", {
    headers,
  });
  expect(response.ok()).toBe(true);
  const initial = (await response.json()).find(
    (annotation: { id: string }) => annotation.id === annotationId,
  );
  await seed.injectToken(page, filtering.user_emails.admin);
  await page.goto("/projects/" + filtering.image.project_id + "/annotate?task=" + taskId);
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toBeVisible({ timeout: 30_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true");
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await page.mouse.move(5, 5);
  const paintedPixels = () => annotationPaintedPixels(stage);
  await stage.screenshot({ path: test.info().outputPath("visible.png"), animations: "disabled" });
  const visiblePixels = await paintedPixels();
  expect(visiblePixels).toBeGreaterThan(0);
  const row = page.getByTestId("box-list-item-" + annotationId);
  await row.getByRole("button", { name: "更多操作", exact: true }).hover();
  const hide = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().endsWith("/annotations/" + annotationId),
  );
  await row.getByRole("button", { name: "隐藏", exact: true }).click();
  expect((await hide).ok()).toBe(true);
  await expect(row).toBeVisible();
  await page.mouse.move(5, 5);
  await expect.poll(paintedPixels).toBeLessThan(visiblePixels);
  await stage.screenshot({ path: test.info().outputPath("hidden.png"), animations: "disabled" });
  await page.reload();
  await expect(stage).toBeVisible();
  await expect(stage).toHaveAttribute("data-image-ready", "true");
  await expect.poll(paintedPixels).toBeLessThan(visiblePixels);
  await row.getByRole("button", { name: "更多操作", exact: true }).hover();
  const show = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().endsWith("/annotations/" + annotationId),
  );
  await row.getByRole("button", { name: "显示", exact: true }).click();
  expect((await show).ok()).toBe(true);
  const restoredResponse = await request.get(
    API_BASE + "/api/v1/tasks/" + taskId + "/annotations",
    { headers },
  );
  expect(restoredResponse.ok()).toBe(true);
  const restored = (await restoredResponse.json()).find(
    (annotation: { id: string }) => annotation.id === annotationId,
  );
  expect(restored.is_hidden).toBe(false);
  expect(restored.geometry).toEqual(initial.geometry);
  await expect(row.getByRole("button", { name: "隐藏", exact: true })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await page.mouse.move(5, 5);
  await stage.screenshot({ path: test.info().outputPath("restored.png"), animations: "disabled" });
  await expect
    .poll(async () => Math.abs((await paintedPixels()) - visiblePixels))
    .toBeLessThan(Math.max(10, visiblePixels * 0.001));
});

test("persisted video hide survives reload and restores the current-frame canvas", async ({
  page,
  request,
  seed,
  filtering,
}) => {
  const taskId = filtering.video.task_ids.neither;
  const geometry = { type: "video_bbox", frame_index: 0, x: 0.65, y: 0.65, w: 0.2, h: 0.2 };
  const annotation = await seed.createTaskAnnotation(taskId, filtering.user_emails.admin, {
    annotation_type: "video_bbox",
    tool_unit_id: "bbox",
    class_name: "car",
    geometry,
  });
  try {
    await seed.injectToken(page, filtering.user_emails.admin);
    await page.goto(`/projects/${filtering.video.project_id}/annotate?task=${taskId}&frame=0`);
    const stage = page.getByTestId("video-konva-stage");
    await expect(stage).toBeVisible({ timeout: 30_000 });
    await expect(stage).toHaveAttribute("data-video-view-ready", "true");
    const row = page.getByTestId(`box-list-item-${annotation.id}`);
    await expect(row).toBeVisible();
    await page.mouse.move(5, 5);
    await stage.screenshot({ path: test.info().outputPath("video-visible.png") });
    const before = await annotationPaintedPixels(stage);
    expect(before).toBeGreaterThan(0);
    await row.getByRole("button", { name: "更多操作", exact: true }).hover();
    const hide = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        response.url().endsWith(`/annotations/${annotation.id}`),
    );
    await row.getByRole("button", { name: "隐藏", exact: true }).click();
    expect((await hide).ok()).toBe(true);
    await expect.poll(() => annotationPaintedPixels(stage)).toBeLessThan(before);
    await page.reload();
    await expect(stage).toHaveAttribute("data-video-view-ready", "true");
    await expect.poll(() => annotationPaintedPixels(stage)).toBeLessThan(before);
    await row.getByRole("button", { name: "更多操作", exact: true }).hover();
    await expect(row.getByRole("button", { name: "显示", exact: true })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await stage.screenshot({ path: test.info().outputPath("video-hidden.png") });
    const show = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        response.url().endsWith(`/annotations/${annotation.id}`),
    );
    await row.getByRole("button", { name: "显示", exact: true }).click();
    expect((await show).ok()).toBe(true);
    await expect(row.getByRole("button", { name: "隐藏", exact: true })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await page.mouse.move(5, 5);
    await expect
      .poll(async () => Math.abs((await annotationPaintedPixels(stage)) - before))
      .toBeLessThan(Math.max(10, before * 0.001));
    await stage.screenshot({ path: test.info().outputPath("video-restored.png") });
    const token = await seed.accessToken(filtering.user_emails.admin);
    const response = await request.get(API_BASE + `/api/v1/tasks/${taskId}/annotations`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(response.ok()).toBe(true);
    const restored = (await response.json()).find(
      (item: { id: string }) => item.id === annotation.id,
    );
    expect(restored.is_hidden).toBe(false);
    expect(restored.geometry).toEqual(geometry);
  } finally {
    await seed.deleteTaskAnnotation(taskId, annotation.id, filtering.user_emails.admin);
  }
});
