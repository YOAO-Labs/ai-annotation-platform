import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, type SeedAPI } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type Prediction = { id: string; result: { class_name: string; shape_index: number }[] };

async function prepare(page: Page, request: APIRequestContext, seed: SeedAPI) {
  const data = await seed.owned();
  const { task_id: taskId } = await seed.videoTask(data.project_id);
  const token = await seed.accessToken(data.admin_email);
  const headers = { Authorization: `Bearer ${token}` };
  const taskResponse = await request.get(`${API_BASE}/api/v1/tasks/${taskId}`, { headers });
  expect(taskResponse.ok()).toBe(true);
  const task = await taskResponse.json();
  const labels = ["car", "person", "a_hotkeys_unmapped_truck"];
  const envelope = {
    schema_version: "1.3",
    tasks: [
      {
        task_match: { display_id: task.display_id },
        media_type: "video",
        predictions: labels.map((class_name, index) => ({
          class_name,
          confidence: 0.99 - index * 0.01,
          shapes: [
            {
              type: "video_bbox",
              frame_index: index === 2 ? 10 : 0,
              x: 0.08 + index * 0.3,
              y: 0.18,
              w: 0.16,
              h: 0.22,
            },
          ],
        })),
      },
    ],
  };
  const file = {
    name: "candidate-decisions.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(envelope)),
  };
  for (const dryRun of [true, false]) {
    const response = await request.post(
      `${API_BASE}/api/v1/projects/${data.project_id}/predictions/import?format=aap_json&dry_run=${dryRun}`,
      { headers, multipart: { file } },
    );
    expect(response.ok(), await response.text()).toBe(true);
    expect(await response.json()).toMatchObject({ imported: 3, errors: [] });
  }
  const predictionsResponse = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/predictions`, {
    headers,
  });
  expect(predictionsResponse.ok()).toBe(true);
  const predictions = (await predictionsResponse.json()) as Prediction[];
  const candidates = labels.map((label) => {
    const prediction = predictions.find((p) =>
      p.result.some((shape) => shape.class_name === label),
    )!;
    return {
      predictionId: prediction.id,
      id: `pred-${prediction.id}-${prediction.result[0].shape_index}`,
      cls: label,
    };
  });
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}&frame=0`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(2);
  return { ...data, taskId, taskDisplayId: task.display_id as string, headers, candidates };
}

/** Consumer-visible selection surface: the floating info card of the selected object. */
function selectionCard(page: Page) {
  return page
    .locator("[data-floating-panel]")
    .filter({ has: page.getByText("选中对象", { exact: true }) });
}

/** A selected AI candidate is the only selection whose card owns the prediction actions. */
function predictionCard(page: Page) {
  return selectionCard(page).filter({ has: page.getByRole("group", { name: "预测操作" }) });
}

test("A/D 按持久结果在当前帧前进，失败可重试，真实 422 补选只接受一次", async ({
  page,
  request,
  seed,
}) => {
  const data = await prepare(page, request, seed);
  const [first, second, otherFrame] = data.candidates;
  const firstRow = page.getByTestId(`box-list-item-${first.id}`);
  await firstRow.click();
  await expect(predictionCard(page)).toContainText(first.cls);
  // Native select type-ahead owns single letter keys even while a candidate is selected.
  const batchSelect = page
    .getByRole("combobox")
    .filter({ has: page.getByRole("option", { name: /全部批次/ }) });
  await batchSelect.press("a");
  await batchSelect.press("d");
  await expect(predictionCard(page)).toContainText(first.cls);
  // The selected-object floating card can cover the list row. Selection already remains
  // active, so only return focus to the workbench before dispatching the global hotkey.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const accepted = page.waitForResponse(
    (r) =>
      r.url().includes(`/predictions/${first.predictionId}/accept`) &&
      r.request().method() === "POST",
  );
  await page.keyboard.press("a");
  expect((await accepted).status()).toBe(200);
  await expect(firstRow).toHaveCount(0);
  await expect(predictionCard(page)).toContainText(second.cls);

  // Explicit failure injection; successful paths below use the real API.
  const rejectURL = `**/predictions/${second.predictionId}/reject?*`;
  await page.route(
    rejectURL,
    (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "A milestone injected failure" }),
      }),
    { times: 1 },
  );
  await page.keyboard.press("d");
  await expect(page.getByText("忽略失败", { exact: true })).toBeVisible();
  await expect(predictionCard(page)).toContainText(second.cls);
  const rejected = page.waitForResponse(
    (r) =>
      r.url().includes(`/predictions/${second.predictionId}/reject`) &&
      r.request().method() === "POST",
  );
  await page.keyboard.press("d");
  expect((await rejected).status()).toBe(204);
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("video-konva-stage")).toBeVisible();
  await expect(page.locator('[data-testid^="box-list-item-pred-"]')).toHaveCount(0);
  for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId("video-konva-stage")).toHaveAttribute(
    "data-video-frame-index",
    "10",
  );
  const otherRow = page.getByTestId(`box-list-item-${otherFrame.id}`);
  await otherRow.click();
  const unmapped = page.waitForResponse(
    (r) =>
      r.url().includes(`/predictions/${otherFrame.predictionId}/accept`) &&
      r.request().method() === "POST",
  );
  await page.keyboard.press("a");
  expect((await unmapped).status()).toBe(422);
  await expect(predictionCard(page)).toContainText(otherFrame.cls);
  const picker = page.getByTestId("class-picker-popover");
  await expect(picker).toBeVisible();
  const retried = page.waitForResponse(
    (r) => r.url().includes("override_class_name=car") && r.request().method() === "POST",
  );
  await picker.getByText("car", { exact: true }).click();
  expect((await retried).status()).toBe(200);
  await expect(otherRow).toHaveCount(0);
  await page.reload();
  const saved = await request.get(`${API_BASE}/api/v1/tasks/${data.taskId}/annotations`, {
    headers: data.headers,
  });
  expect(saved.ok()).toBe(true);
  const annotations = (await saved.json()) as {
    parent_prediction_id?: string;
    class_name: string;
  }[];
  expect(annotations.filter((ann) => ann.parent_prediction_id === first.predictionId)).toHaveLength(
    1,
  );
  expect(
    annotations.filter((ann) => ann.parent_prediction_id === second.predictionId),
  ).toHaveLength(0);
  expect(annotations.filter((ann) => ann.parent_prediction_id === otherFrame.predictionId)).toEqual(
    [expect.objectContaining({ class_name: "car" })],
  );
});

test("延迟响应期间重复按键只有一项写入，切题后旧响应不改新选择", async ({
  page,
  request,
  seed,
}) => {
  const data = await prepare(page, request, seed);
  const [candidate] = data.candidates;
  const { task_id: targetId } = await seed.videoTask(data.project_id);
  await seed.advanceTask({
    taskId: targetId,
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  const newSelection = await seed.createTaskAnnotation(targetId, data.admin_email, {
    annotation_type: "video_bbox",
    tool_unit_id: "bbox",
    class_name: "person",
    geometry: { type: "video_bbox", frame_index: 0, x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
  });
  const targetResponse = await request.get(`${API_BASE}/api/v1/tasks/${targetId}`, {
    headers: data.headers,
  });
  expect(targetResponse.ok()).toBe(true);
  const targetTask = await targetResponse.json();
  // Refresh the queue after creating the second task, before starting any pending decision.
  await page.reload();
  await page.getByTestId(`box-list-item-${candidate.id}`).click();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let calls = 0;
  // Delay transport only; the accepted annotation is still written by the real API.
  await page.route(`**/predictions/${candidate.predictionId}/accept?*`, async (route) => {
    calls++;
    await gate;
    await route.continue();
  });
  await page.keyboard.down("a");
  await expect.poll(() => calls).toBe(1);
  await page.keyboard.down("a");
  await page.keyboard.up("a");
  await page.keyboard.press("a");
  // The single pending write keeps the candidate selected: its card still owns the prediction
  // actions, so repeated keys cannot start a second decision or advance the selection.
  await expect(predictionCard(page)).toContainText(candidate.cls);
  expect(calls).toBe(1);
  // SPA navigation keeps the owner mounted while its task changes.
  const queue = page.getByRole("tabpanel", { name: "任务队列", exact: true });
  await queue.getByText(targetTask.display_id, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${targetId}`));
  await queue.getByText(data.taskDisplayId, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${data.taskId}`));
  await page.getByTestId(`box-list-item-${candidate.id}`).click();
  await page.keyboard.press("a");
  expect(calls).toBe(1);
  await queue.getByText(targetTask.display_id, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`task=${targetId}`));
  const newRow = page.getByTestId(`box-list-item-${newSelection.id}`);
  await newRow.click();
  const targetCard = selectionCard(page);
  await expect(targetCard).toContainText("person");
  const oldDecision = page.waitForResponse(
    (r) =>
      r.url().includes(`/predictions/${candidate.predictionId}/accept`) &&
      r.request().method() === "POST",
  );
  release();
  expect((await oldDecision).status()).toBe(200);
  expect(calls).toBe(1);
  // The old task's late receipt may only land in its own task: the current selection and the
  // current task's persisted annotations stay untouched.
  await expect(targetCard).toContainText("person");
  const targetAnnotations = await request.get(`${API_BASE}/api/v1/tasks/${targetId}/annotations`, {
    headers: data.headers,
  });
  expect(targetAnnotations.ok()).toBe(true);
  const persistedTarget = (await targetAnnotations.json()) as {
    id: string;
    class_name: string;
  }[];
  expect(persistedTarget).toEqual([
    expect.objectContaining({ id: newSelection.id, class_name: "person" }),
  ]);
  await expect(page.getByTestId(`box-list-item-${candidate.id}`)).toHaveCount(0);
});
