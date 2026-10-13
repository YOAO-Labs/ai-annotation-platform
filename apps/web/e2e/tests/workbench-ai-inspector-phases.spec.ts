import { openContextToolbar } from "../fixtures/context-toolbar";
import { panelCommand } from "../fixtures/workbench-panel-actions";
import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { expect, test as seededTest, type SeedData } from "../fixtures/seed";
import { startAiRequestBackend } from "../fixtures/ai-request-backend";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
type Backend = Awaited<ReturnType<typeof startAiRequestBackend>>;
type Job = { id: string; celery_task_id: string; status: string; progress_pct: number };
type Prediction = { id: string; result: Array<{ attributes?: Record<string, unknown> }> };
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });
async function json<T>(response: Pick<APIResponse, "ok" | "text" | "status" | "json">): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}
async function jobs(request: APIRequestContext, projectId: string, token: string) {
  return (
    await json<{ items: Job[] }>(
      await request.get(
        `${API_BASE}/api/v1/async-jobs?kind=batch_predict&project_id=${projectId}&limit=200`,
        { headers: auth(token) },
      ),
    )
  ).items;
}
async function predictions(request: APIRequestContext, taskId: string, token: string) {
  return json<Prediction[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/predictions`, {
      headers: auth(token),
    }),
  );
}
async function annotations(request: APIRequestContext, taskId: string, token: string) {
  return json<
    Array<{
      id: string;
      attributes: Record<string, unknown>;
      geometry: { type: string; frame_index?: number };
    }>
  >(await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers: auth(token) }));
}

async function openAi(page: Page, data: SeedData, taskId: string, video = false) {
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(page.getByTestId(video ? "video-konva-stage" : "workbench-stage")).toBeVisible({
    timeout: 30_000,
  });
  await page.getByTestId("workbench-ai-single").click();
  await expect(page.getByTestId("ai-prediction-primary-action")).toBeEnabled({ timeout: 20_000 });
}
async function expectPhase(page: Page, phase: string) {
  await expect(page.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", phase, {
    timeout: 30_000,
  });
  await expect(page.getByTestId("ai-prediction-primary-action")).toHaveCount(1);
}
async function runImage(page: Page) {
  const queued = page.waitForResponse(
    (response) => response.url().endsWith("/preannotate") && response.request().method() === "POST",
  );
  await page.getByTestId("ai-prediction-primary-action").click();
  return json<{ job_id: string }>(await queued);
}

const test = seededTest.extend<{
  ai: { data: SeedData; token: string; backend: Backend; pageErrors: string[] };
}>({
  ai: async ({ request, seed, page }, provideFixture, testInfo) => {
    const data = await seed.owned();
    const token = await seed.accessToken(data.admin_email);
    const backend = await startAiRequestBackend();
    const detach = await backend.attach(request, {
      apiBase: API_BASE,
      projectId: data.project_id,
      token,
      defaultBackendId: data.ml_backend_id,
    });
    const project = await json<{ tool_bindings: Record<string, Record<string, unknown>> }>(
      await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, { headers: auth(token) }),
    );
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
        headers: auth(token),
        data: {
          tool_bindings: {
            ...project.tool_bindings,
            bbox: {
              ...project.tool_bindings.bbox,
              attribute_schema: {
                fields: [{ key: "color", label: "颜色", type: "text", applies_to: "*" }],
              },
            },
          },
        },
      }),
    );
    await seed.injectToken(page, data.admin_email);
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") {
        const location = message.location().url;
        const path = location ? new URL(location).pathname : "console";
        pageErrors.push(`${path}: ${message.text()}`);
      }
    });
    try {
      await provideFixture({ data, token, backend, pageErrors });
    } finally {
      backend.failAll(false);
      backend.release();
      // Keep only non-secret ownership evidence for exact cleanup of uploaded frame JPEGs.
      await writeFile(
        testInfo.outputPath("ai-request-metadata.json"),
        JSON.stringify({
          projectId: data.project_id,
          taskIds: [
            ...new Set(backend.requests.flatMap((entry) => entry.tasks.map((task) => task.id))),
          ],
        }),
      );
      try {
        await expect
          .poll(
            async () =>
              (await jobs(request, data.project_id, token)).every((job) =>
                ["completed", "failed", "cancelled"].includes(job.status),
              ),
            { timeout: 30_000 },
          )
          .toBe(true);
      } finally {
        await detach();
        await backend.close();
      }
    }
  },
});

test.describe("当前题 AI 的真实请求归属与阶段", () => {
  test.skip(
    process.env.PLAYWRIGHT_AI_REQUEST_WORKER !== "1",
    "Requires a dedicated API/Celery broker; see e2e/README.md",
  );
  test.setTimeout(100_000);
  test.use({ viewport: { width: 1440, height: 1080 }, actionTimeout: 15_000 });

  test("图片运行摘要冻结，移动隐藏不重建，候选属性草稿随采纳持久化", async ({
    page,
    request,
    ai,
  }) => {
    const { data, token, backend, pageErrors } = ai;
    const taskId = data.task_ids[0];
    await openAi(page, data, taskId);
    await expectPhase(page, "idle");
    await expect(page.getByTestId("ai-prediction-advanced")).toBeHidden();
    backend.hold();
    const queued = await runImage(page);
    await expect.poll(() => backend.requests.length).toBe(1);
    await expectPhase(page, "running");
    const summary = page.getByTestId("ai-request-summary");
    await expect(summary).toContainText("small");
    const originalSummary = await summary.innerText();
    const wrapper = page.locator('[data-workbench-panel="ai-task"]');
    const identity = await wrapper.elementHandle();
    await page.getByTestId("ai-prediction-advanced-toggle").click();
    await page
      .getByTestId("ai-prediction-advanced")
      .getByTestId("ai-variant-size")
      .selectOption("large");
    await expect(summary).toHaveText(originalSummary, { useInnerText: true });
    await expect(page.getByTestId("ai-prediction-run-next")).toBeDisabled();
    await panelCommand(page, "当前题 AI", "浮动面板");
    await panelCommand(page, "当前题 AI", "停靠到右侧");
    await panelCommand(page, "当前题 AI", "隐藏面板");
    await expect(wrapper).toHaveAttribute("aria-hidden", "true");
    await expect(page.getByTestId("ai-prediction-popover")).toHaveCount(1);
    await page.getByTestId("workbench-ai-single").click();
    expect(await wrapper.evaluate((node, original) => node === original, identity)).toBe(true);
    await expect(summary).toHaveText(originalSummary, { useInnerText: true });
    await expect
      .poll(
        async () =>
          (await jobs(request, data.project_id, token)).find(
            (job) => job.celery_task_id === queued.job_id,
          )?.status,
      )
      .toBe("running");
    await expect(page.getByRole("progressbar", { name: "本次请求进度" })).toHaveAttribute(
      "aria-valuenow",
      "0",
    );
    backend.release();
    await expectPhase(page, "review");
    await page.getByTestId("ai-prediction-primary-action").click();
    const [candidate] = await predictions(request, taskId, token);
    // The bbox candidate keeps its own fields even while the polygon tool is active.
    await page.getByTestId("tool-btn-polygon").click();
    await page.getByTestId(`box-list-item-pred-${candidate.id}-0`).click();
    const collapseCard = page.getByRole("button", { name: "收起浮窗", exact: true });
    if (await collapseCard.isVisible()) await collapseCard.click();
    const inspector = page.locator('[data-workbench-panel="inspector"]');
    const inspectorIdentity = await inspector.elementHandle();
    const color = inspector.getByRole("textbox", { name: "颜色", exact: true });
    await expect(color).toHaveValue("blue");
    await color.fill("red after review");
    await panelCommand(page, "标注详情", "隐藏面板");
    await page.getByTestId("ai-prediction-primary-action").click();
    expect(await inspector.evaluate((node, original) => node === original, inspectorIdentity)).toBe(
      true,
    );
    await expect(color).toHaveValue("red after review");
    await expect(page.getByTestId("ai-candidate-accept")).toBeInViewport();
    const saved = page.waitForResponse(
      (response) =>
        response.url().includes(`/predictions/${candidate.id}/accept`) &&
        response.request().method() === "POST",
    );
    await page.getByTestId("ai-candidate-accept").press("a");
    expect((await saved).status()).toBe(200);
    await page.reload();
    const rows = await annotations(request, taskId, token);
    expect(rows).toHaveLength(1);
    expect(rows[0].attributes.color).toBe("red after review");
    await expect(page.getByTestId(`box-list-item-${rows[0].id}`)).toBeVisible({ timeout: 20_000 });
    expect(backend.requests).toHaveLength(1);
    expect(pageErrors).toEqual([]);
  });

  test("真实作业失败后重试原输入，下一轮配置不改写重试请求", async ({ page, request, ai }) => {
    const { data, token, backend, pageErrors } = ai;
    await openAi(page, data, data.task_ids[0]);
    backend.failAll(true);
    const first = await runImage(page);
    await expectPhase(page, "error");
    await expect(page.getByTestId("ai-request-error")).toContainText("预标失败");
    await page.getByTestId("ai-prediction-advanced-toggle").click();
    await page
      .getByTestId("ai-prediction-advanced")
      .getByTestId("ai-variant-size")
      .selectOption("large");
    backend.failAll(false);
    backend.hold();
    const second = await runImage(page);
    expect(second.job_id).not.toBe(first.job_id);
    await expect.poll(() => backend.requests.length).toBe(2);
    expect(backend.requests[1].context).toEqual(backend.requests[0].context);
    await expect(page.getByTestId("ai-request-summary")).toContainText("small");
    backend.release();
    await expectPhase(page, "review");
    const found = await jobs(request, data.project_id, token);
    expect(found.find((job) => job.celery_task_id === first.job_id)?.status).toBe("failed");
    expect(found.find((job) => job.celery_task_id === second.job_id)?.status).toBe("completed");
    expect(pageErrors).toEqual([]);
  });

  test("取消等待真实终态，旧题迟到结果不打开新题审阅", async ({ page, request, ai }) => {
    const { data, token, backend, pageErrors } = ai;
    await openAi(page, data, data.task_ids[0]);
    backend.hold();
    const queued = await runImage(page);
    await expect.poll(() => backend.requests.length).toBe(1);
    await expect(page.getByTestId("ai-prediction-primary-action")).toHaveText("取消本次请求");
    const cancel = page.waitForResponse(
      (response) =>
        /\/async-jobs\/[^/]+\/cancel$/.test(response.url()) &&
        response.request().method() === "POST",
    );
    await page.getByTestId("ai-prediction-primary-action").click();
    expect((await cancel).status()).toBe(200);
    await expect(page.getByTestId("ai-prediction-primary-action")).toHaveText("正在取消本次请求");
    await expectPhase(page, "running");
    // Navigation occurs through the existing task queue while the old model call is held.
    const nextTask = await json<{ display_id: string }>(
      await request.get(`${API_BASE}/api/v1/tasks/${data.task_ids[1]}`, { headers: auth(token) }),
    );
    await page.getByText(nextTask.display_id, { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`task=${data.task_ids[1]}`));
    await expectPhase(page, "idle");
    await page.getByTestId("workbench-ai-single").click();
    await panelCommand(page, "当前题 AI", "隐藏面板");
    backend.release();
    await expect
      .poll(
        async () =>
          (await jobs(request, data.project_id, token)).find(
            (job) => job.celery_task_id === queued.job_id,
          )?.status,
        { timeout: 30_000 },
      )
      .toBe("cancelled");
    await expect(page.locator('[data-workbench-panel="ai-task"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await page.getByTestId("workbench-ai-single").click();
    await expectPhase(page, "idle");
    await expect(page.getByTestId("ai-request-summary")).toHaveCount(0);
    expect(await predictions(request, data.task_ids[1], token)).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test("视频帧请求复用原执行器并保留 tracker 审阅范围", async ({ page, request, seed, ai }) => {
    const { data, token, backend, pageErrors } = ai;
    const video = await seed.videoTask(data.project_id);
    const tracker = await seed.trackerReview(video.task_id, data.admin_email);
    await openAi(page, data, video.task_id, true);
    await openContextToolbar(page, "tracker-review");
    const review = page.getByTestId("video-tracker-review-bar");
    await expect(review).toContainText("已审 0/20");
    const original = await annotations(request, video.task_id, token);
    backend.hold();
    const frameResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/predict-frame") && response.request().method() === "POST",
    );
    await page.getByTestId("ai-prediction-primary-action").click();
    await expect.poll(() => backend.requests.length).toBe(1);
    await expectPhase(page, "running");
    await expect(page.getByTestId("ai-request-summary")).toContainText("当前帧 F0");
    await page.getByTestId("ai-prediction-advanced-toggle").click();
    await expect(page.getByTestId("ai-prediction-run-next")).toBeDisabled();
    backend.release();
    const result = await json<{ frame_index: number; candidate_count: number }>(
      await frameResponse,
    );
    expect(result).toMatchObject({ frame_index: 0, candidate_count: 1 });
    await expectPhase(page, "review");
    await page.getByTestId("ai-prediction-primary-action").click();
    const [candidate] = await predictions(request, video.task_id, token);
    await page.getByTestId(`box-list-item-pred-${candidate.id}-0`).click();
    const saved = page.waitForResponse(
      (response) =>
        response.url().includes(`/predictions/${candidate.id}/accept`) &&
        response.request().method() === "POST",
    );
    await page.getByTestId("ai-candidate-accept").click();
    expect((await saved).status()).toBe(200);
    await page.reload();
    await openContextToolbar(page, "tracker-review");
    await expect(review).toContainText("已审 0/20", { timeout: 20_000 });
    const rows = await annotations(request, video.task_id, token);
    expect(rows.filter((row) => original.some((source) => source.id === row.id))).toEqual(original);
    expect(
      rows.find((row) => !original.some((source) => source.id === row.id))?.geometry,
    ).toMatchObject({ type: "video_bbox", frame_index: 0 });
    const trackerResult = await json<{ candidate_pending: number; candidate_accepted: number }>(
      await request.get(`${API_BASE}/api/v1/video-tracker-jobs/${tracker.job_id}/preview`, {
        headers: auth(token),
      }),
    );
    expect(trackerResult).toMatchObject({ candidate_pending: 20, candidate_accepted: 0 });
    // A second ordinary frame run produces its own review candidate; rejecting it leaves the
    // accepted frame annotation and the tracker decision ledger untouched.
    await page.getByTestId("workbench-ai-single").click();
    await expectPhase(page, "idle");
    await page.getByTestId("ai-prediction-primary-action").click();
    await expectPhase(page, "review");
    const repeated = (await predictions(request, video.task_id, token)).find(
      (item) => item.id !== candidate.id,
    )!;
    await page.getByTestId("ai-prediction-primary-action").click();
    await page.getByTestId(`box-list-item-pred-${repeated.id}-0`).click();
    const rejected = page.waitForResponse(
      (response) =>
        response.url().includes(`/predictions/${repeated.id}/reject`) &&
        response.request().method() === "POST",
    );
    await page.getByTestId("ai-candidate-reject").press("d");
    expect((await rejected).status()).toBe(204);
    await page.reload();
    expect(await annotations(request, video.task_id, token)).toEqual(rows);
    await openContextToolbar(page, "tracker-review");
    await expect(review).toContainText("已审 0/20", { timeout: 20_000 });
    expect(await jobs(request, data.project_id, token)).toEqual([]);
    expect(backend.requests).toHaveLength(2);
    expect(pageErrors).toEqual([]);
  });
});
