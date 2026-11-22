import { openContextToolbar } from "../fixtures/context-toolbar";
import { panelCommand } from "../fixtures/workbench-panel-actions";
import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { expect, test as seededTest, type SeedData } from "../fixtures/seed";
import { startAiRequestBackend, type AiRequestRecord } from "../fixtures/ai-request-backend";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const imageAiTitle = "单图 AI";
type Backend = Awaited<ReturnType<typeof startAiRequestBackend>>;
type Job = {
  id: string;
  project_id: string;
  celery_task_id: string;
  status: string;
};
type QueuedJob = { job_id: string; celery_task_id: string; status: string; total_tasks: number };
type Prediction = {
  id: string;
  task_id: string;
  project_id: string;
  ml_backend_id: string;
  result: Array<{ shape_index: number; attributes?: Record<string, unknown> }>;
};
type Annotation = {
  id: string;
  task_id: string;
  parent_prediction_id: string | null;
  attributes: Record<string, unknown>;
  geometry: { type: string; frame_index?: number };
};
type FrameResult = { prediction_id: string; frame_index: number; candidate_count: number };

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
  return json<Annotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers: auth(token) }),
  );
}

async function expectJob(
  request: APIRequestContext,
  data: SeedData,
  token: string,
  queued: QueuedJob,
  status: string,
) {
  await expect
    .poll(
      async () =>
        (await jobs(request, data.project_id, token)).find((job) => job.id === queued.job_id),
      { timeout: 30_000 },
    )
    .toMatchObject({
      id: queued.job_id,
      project_id: data.project_id,
      celery_task_id: queued.celery_task_id,
      status,
    });
}

async function expectModelRequest(
  backend: Backend,
  index: number,
  taskId: string,
  responseStatus?: number,
) {
  await expect
    .poll(
      async () => {
        const requests = await backend.requests();
        return {
          count: requests.length,
          taskIds: requests[index]?.tasks.map((task) => task.id),
          responseStatus: requests[index]?.responseStatus,
        };
      },
      { timeout: 30_000 },
    )
    .toEqual({ count: index + 1, taskIds: [taskId], responseStatus });
}

async function expectCandidate(
  request: APIRequestContext,
  data: SeedData,
  taskId: string,
  token: string,
) {
  const candidates = await predictions(request, taskId, token);
  expect(candidates).toHaveLength(1);
  const [candidate] = candidates;
  expect(candidate).toMatchObject({
    task_id: taskId,
    project_id: data.project_id,
    ml_backend_id: data.ml_backend_id,
    result: [{ shape_index: 0, attributes: { color: "blue" } }],
  });
  return candidate;
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
async function runImage(page: Page, projectId: string, taskId: string) {
  const response = page.waitForResponse(
    (result) =>
      new URL(result.url()).pathname === `/api/v1/projects/${projectId}/preannotate` &&
      result.request().method() === "POST",
  );
  await page.getByTestId("ai-prediction-primary-action").click();
  const queued = await response;
  expect(queued.request().postDataJSON()).toMatchObject({
    execution_scope: "workbench",
    task_ids: [taskId],
  });
  const body = await json<QueuedJob>(queued);
  expect(body).toMatchObject({ status: "queued", total_tasks: 1 });
  expect(body.job_id).toBeTruthy();
  expect(body.celery_task_id).toBeTruthy();
  return body;
}

function frameResponse(page: Page, data: SeedData) {
  return page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}/predict-frame` &&
      response.request().method() === "POST",
  );
}

async function selectInspectorCandidate(page: Page, predictionId: string) {
  await page.getByTestId(`box-list-item-pred-${predictionId}-0`).click();
  const collapseCard = page.getByRole("button", { name: "收起浮窗", exact: true });
  if (await collapseCard.isVisible()) await collapseCard.click();
}

function decisionResponse(page: Page, taskId: string, predictionId: string, action: string) {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/v1/tasks/${taskId}/predictions/${predictionId}/${action}` &&
      url.searchParams.get("shape_index") === "0" &&
      response.request().method() === "POST"
    );
  });
}

const test = seededTest.extend<{
  ai: { data: SeedData; token: string; backend: Backend; pageErrors: string[] };
}>({
  ai: async ({ request, seed, page, owned: data }, provideFixture, testInfo) => {
    const token = await seed.accessToken(data.admin_email);
    const backend = startAiRequestBackend(seed, data);
    const detach = await backend.attach(request, {
      apiBase: API_BASE,
      projectId: data.project_id,
      token,
      defaultBackendId: data.ml_backend_id,
    });
    try {
      const project = await json<{ tool_bindings: Record<string, Record<string, unknown>> }>(
        await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, {
          headers: auth(token),
        }),
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
      await provideFixture({ data, token, backend, pageErrors });
    } finally {
      let finalJobs: Job[] = [];
      let calls: AiRequestRecord[] = [];
      try {
        try {
          await backend.failAll(false);
        } finally {
          await backend.release();
        }
        await expect
          .poll(
            async () => {
              [finalJobs, calls] = await Promise.all([
                jobs(request, data.project_id, token),
                backend.requests(),
              ]);
              return (
                finalJobs.every((job) =>
                  ["completed", "failed", "cancelled"].includes(job.status),
                ) && calls.every((call) => call.responseStatus !== undefined)
              );
            },
            { timeout: 30_000 },
          )
          .toBe(true);
      } finally {
        try {
          await writeFile(
            testInfo.outputPath("ai-request-metadata.json"),
            JSON.stringify({
              projectId: data.project_id,
              backendId: data.ml_backend_id,
              taskIds: [...new Set(calls.flatMap((entry) => entry.tasks.map((task) => task.id)))],
              jobs: finalJobs.map(({ id, celery_task_id, status }) => ({
                id,
                celery_task_id,
                status,
              })),
              modelResponses: calls.map(({ responseStatus }) => responseStatus),
            }),
          );
        } finally {
          await detach();
        }
      }
    }
  },
});

test.describe("当前题 AI 的真实请求归属与阶段", () => {
  test.skip(
    process.env.PLAYWRIGHT_AI_REQUEST_WORKER !== "1",
    "需要独立工作树的 API、Celery worker 和专用 broker，见 e2e/README.md",
  );
  test.setTimeout(100_000);
  test.use({ viewport: { width: 1440, height: 1080 }, actionTimeout: 15_000 });
  test.beforeAll(() => {
    expect(process.env.YOAO_WORKTREE_MODE).toBe("e2e");
    expect(process.env.PREANNOTATE_GPU_QUEUE).toBe("ml.cpu");
    expect(process.env.PREANNOTATE_CPU_QUEUE).toBe("ml.cpu");
  });

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
    await backend.hold();
    const queued = await runImage(page, data.project_id, taskId);
    await expectModelRequest(backend, 0, taskId);
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
    const floating = page.getByRole("dialog", { name: imageAiTitle, exact: true });
    await panelCommand(page, imageAiTitle, "浮动面板");
    await expect(floating).toBeVisible();
    await panelCommand(page, imageAiTitle, "停靠到右侧");
    await expect(floating).toBeHidden();
    await panelCommand(page, imageAiTitle, "隐藏面板");
    await expect(wrapper).toHaveAttribute("aria-hidden", "true");
    await expect(page.getByTestId("ai-prediction-popover")).toHaveCount(1);
    await page.getByTestId("workbench-ai-single").click();
    expect(await wrapper.evaluate((node, original) => node === original, identity)).toBe(true);
    await expect(summary).toHaveText(originalSummary, { useInnerText: true });
    await expectJob(request, data, token, queued, "running");
    expect(await predictions(request, taskId, token)).toEqual([]);
    await expect(page.getByRole("progressbar", { name: "本次请求进度" })).toHaveAttribute(
      "aria-valuenow",
      "0",
    );
    await backend.release();
    await expectModelRequest(backend, 0, taskId, 200);
    await expectJob(request, data, token, queued, "completed");
    await expectPhase(page, "review");
    await page.getByTestId("ai-prediction-primary-action").click();
    const candidate = await expectCandidate(request, data, taskId, token);
    await page.getByTestId("tool-btn-polygon").click();
    await selectInspectorCandidate(page, candidate.id);
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
    const saved = decisionResponse(page, taskId, candidate.id, "accept");
    await page.getByTestId("ai-candidate-accept").press("a");
    const response = await saved;
    expect(response.request().postDataJSON()).toMatchObject({
      attribute_overrides: { color: "red after review" },
    });
    const accepted = await json<Annotation[]>(response);
    expect(accepted).toHaveLength(1);
    expect(accepted[0]).toMatchObject({
      task_id: taskId,
      parent_prediction_id: candidate.id,
      attributes: { color: "red after review" },
    });
    await page.reload();
    const rows = await annotations(request, taskId, token);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: accepted[0].id,
      parent_prediction_id: candidate.id,
      attributes: { color: "red after review" },
    });
    await expect(page.getByTestId(`box-list-item-${rows[0].id}`)).toBeVisible({ timeout: 20_000 });
    expect(await backend.requests()).toHaveLength(1);
    expect(pageErrors).toEqual([]);
  });

  test("真实作业失败后重试原输入，下一轮配置不改写重试请求", async ({ page, request, ai }) => {
    const { data, token, backend, pageErrors } = ai;
    const taskId = data.task_ids[0];
    await openAi(page, data, taskId);
    await backend.failAll(true);
    const first = await runImage(page, data.project_id, taskId);
    await expectModelRequest(backend, 0, taskId, 503);
    await expectJob(request, data, token, first, "failed");
    expect(await predictions(request, taskId, token)).toEqual([]);
    await expectPhase(page, "error");
    await expect(page.getByTestId("ai-request-error")).toContainText("预标失败");
    await expect(
      page.getByTestId("ai-request-error").getByRole("button", { name: "复制完整错误信息" }),
    ).toHaveAttribute("title", /all_predictions_failed/);
    await expect(page.getByRole("button", { name: "按原配置重新运行" })).toBeEnabled();
    await page.getByTestId("ai-prediction-advanced-toggle").click();
    await page
      .getByTestId("ai-prediction-advanced")
      .getByTestId("ai-variant-size")
      .selectOption("large");
    await backend.failAll(false);
    await backend.hold();
    const second = await runImage(page, data.project_id, taskId);
    expect(second.job_id).not.toBe(first.job_id);
    expect(second.celery_task_id).not.toBe(first.celery_task_id);
    await expectModelRequest(backend, 1, taskId);
    await expectJob(request, data, token, second, "running");
    const calls = await backend.requests();
    expect(calls[1].context).toEqual(calls[0].context);
    await expect(page.getByTestId("ai-request-summary")).toContainText("small");
    await backend.release();
    await expectPhase(page, "review");
    await expectModelRequest(backend, 1, taskId, 200);
    await expectJob(request, data, token, first, "failed");
    await expectJob(request, data, token, second, "completed");
    await expectCandidate(request, data, taskId, token);
    expect(await annotations(request, taskId, token)).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test("取消等待真实终态，旧题迟到结果不打开新题审阅", async ({ page, request, ai }) => {
    const { data, token, backend, pageErrors } = ai;
    const [taskId, nextTaskId] = data.task_ids;
    const before = await annotations(request, taskId, token);
    const nextBefore = await annotations(request, nextTaskId, token);
    await openAi(page, data, taskId);
    await backend.hold();
    const queued = await runImage(page, data.project_id, taskId);
    await expectModelRequest(backend, 0, taskId);
    await expectJob(request, data, token, queued, "running");
    await expect(page.getByTestId("ai-prediction-primary-action")).toHaveText("取消本次请求");
    const cancel = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/api/v1/async-jobs/${queued.job_id}/cancel` &&
        response.request().method() === "POST",
    );
    await page.getByTestId("ai-prediction-primary-action").click();
    expect(await json(await cancel)).toEqual({
      id: queued.job_id,
      status: "cancel_requested",
    });
    await expectJob(request, data, token, queued, "running");
    await expect(page.getByTestId("ai-prediction-primary-action")).toHaveText("正在取消本次请求");
    await expectPhase(page, "running");
    const nextTask = await json<{ display_id: string }>(
      await request.get(`${API_BASE}/api/v1/tasks/${nextTaskId}`, { headers: auth(token) }),
    );
    await page.getByText(nextTask.display_id, { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`task=${nextTaskId}`));
    await expectPhase(page, "idle");
    await page.getByTestId("workbench-ai-single").click();
    await panelCommand(page, imageAiTitle, "隐藏面板");
    await backend.release();
    await expectModelRequest(backend, 0, taskId, 200);
    await expectJob(request, data, token, queued, "cancelled");
    await expect(page.locator('[data-workbench-panel="ai-task"]')).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await page.getByTestId("workbench-ai-single").click();
    await expectPhase(page, "idle");
    await expect(page.getByTestId("ai-request-summary")).toHaveCount(0);
    expect(await predictions(request, taskId, token)).toEqual([]);
    expect(await predictions(request, nextTaskId, token)).toEqual([]);
    expect(await annotations(request, taskId, token)).toEqual(before);
    expect(await annotations(request, nextTaskId, token)).toEqual(nextBefore);
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
    await backend.hold();
    const firstFrame = frameResponse(page, data);
    await page.getByTestId("ai-prediction-primary-action").click();
    await expectModelRequest(backend, 0, video.task_id);
    await expectPhase(page, "running");
    await expect(page.getByTestId("ai-request-summary")).toContainText("当前帧 F0");
    await page.getByTestId("ai-prediction-advanced-toggle").click();
    await expect(page.getByTestId("ai-prediction-run-next")).toBeDisabled();
    await backend.release();
    const result = await json<FrameResult>(await firstFrame);
    await expectModelRequest(backend, 0, video.task_id, 200);
    expect(result).toMatchObject({ frame_index: 0, candidate_count: 1 });
    await expectPhase(page, "review");
    await page.getByTestId("ai-prediction-primary-action").click();
    const candidate = await expectCandidate(request, data, video.task_id, token);
    expect(candidate.id).toBe(result.prediction_id);
    await selectInspectorCandidate(page, candidate.id);
    const saved = decisionResponse(page, video.task_id, candidate.id, "accept");
    await page.getByTestId("ai-candidate-accept").click();
    const accepted = await json<Annotation[]>(await saved);
    expect(accepted).toHaveLength(1);
    expect(accepted[0]).toMatchObject({
      task_id: video.task_id,
      parent_prediction_id: candidate.id,
      geometry: { type: "video_bbox", frame_index: 0 },
    });
    await page.reload();
    await openContextToolbar(page, "tracker-review");
    await expect(review).toContainText("已审 0/20", { timeout: 20_000 });
    const rows = await annotations(request, video.task_id, token);
    expect(rows.filter((row) => original.some((source) => source.id === row.id))).toEqual(original);
    expect(rows).toHaveLength(original.length + 1);
    expect(rows.find((row) => row.id === accepted[0].id)).toMatchObject({
      parent_prediction_id: candidate.id,
      geometry: { type: "video_bbox", frame_index: 0 },
    });
    const trackerResult = await json<{ candidate_pending: number; candidate_accepted: number }>(
      await request.get(`${API_BASE}/api/v1/video-tracker-jobs/${tracker.job_id}/preview`, {
        headers: auth(token),
      }),
    );
    expect(trackerResult).toMatchObject({ candidate_pending: 20, candidate_accepted: 0 });
    await page.getByTestId("workbench-ai-single").click();
    await expectPhase(page, "idle");
    const secondFrame = frameResponse(page, data);
    await page.getByTestId("ai-prediction-primary-action").click();
    await expectPhase(page, "review");
    const repeated = await json<FrameResult>(await secondFrame);
    await expectModelRequest(backend, 1, video.task_id, 200);
    expect(repeated).toMatchObject({ frame_index: 0, candidate_count: 1 });
    expect(repeated.prediction_id).not.toBe(candidate.id);
    expect(await predictions(request, video.task_id, token)).toEqual(
      expect.arrayContaining([expect.objectContaining({ id: repeated.prediction_id })]),
    );
    await page.getByTestId("ai-prediction-primary-action").click();
    await selectInspectorCandidate(page, repeated.prediction_id);
    const rejected = decisionResponse(page, video.task_id, repeated.prediction_id, "reject");
    await page.getByTestId("ai-candidate-reject").press("d");
    expect((await rejected).status()).toBe(204);
    await page.reload();
    expect(await annotations(request, video.task_id, token)).toEqual(rows);
    expect(
      (await predictions(request, video.task_id, token)).some(
        (item) => item.id === repeated.prediction_id,
      ),
    ).toBe(false);
    await openContextToolbar(page, "tracker-review");
    await expect(review).toContainText("已审 0/20", { timeout: 20_000 });
    expect(await jobs(request, data.project_id, token)).toEqual([]);
    expect(await backend.requests()).toHaveLength(2);
    expect(pageErrors).toEqual([]);
  });
});
