/**
 * M3 · 流程录制：AI 预标注（选项目 → 选批次 → 发起预标注 → 查看 job）。
 *
 * 输出：outputs/flows/ai-preannotate.gif
 */
import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import type { DrawWindow } from "./rotated-bbox";

interface BatchPredictionEvidence {
  id: string;
  ml_backend_id?: string | null;
  model_version?: string | null;
  source?: string | null;
  result?: Array<Record<string, unknown>>;
}

interface BatchAsyncJobEvidence {
  id: string;
  kind: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  celery_task_id: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface AiPreannotateEvidence {
  endpoint: string;
  job_id: string;
  task_id: string;
  selected_backend_id: string;
  job: {
    id: string;
    kind: string;
    status: BatchAsyncJobEvidence["status"];
    celery_task_id: string | null;
    payload_keys: string[];
    result_keys: string[];
    result: {
      success_count: number;
      failed_count: number;
      duration_ms: number | null;
    };
    payload_secret_free: boolean;
  };
  predictions: {
    endpoint: string;
    ids: string[];
    count: number;
    shape_count: number;
    source: string[];
    model_versions: string[];
    result_sha256: string;
  };
  api: Array<{ method: string; path: string; status: number }>;
  console: { errors: string[] };
}

export interface AiPreannotateWindow extends DrawWindow {
  evidence: AiPreannotateEvidence;
}

const SAFE_JOB_VALUE_PATTERN =
  /(?:x-amz-|signature=|presigned|signed_url|secret_key|authorization|bearer\s)/i;

function assertSecretFreeJobData(job: BatchAsyncJobEvidence): void {
  const serialized = JSON.stringify({ payload: job.payload, result: job.result });
  if (SAFE_JOB_VALUE_PATTERN.test(serialized)) {
    throw new Error("[ai-preannotate] async job payload/result contains a signed URL or secret");
  }
}

export async function runAiPreannotate(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<AiPreannotateWindow> {
  const apiBase = (process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010").replace(/\/$/, "");
  const apiEvidence: Array<{ method: string; path: string; status: number }> = [];
  const consoleErrors: string[] = [];
  const onResponse = (response: import("@playwright/test").Response) => {
    const url = new URL(response.url());
    if (
      url.pathname === "/api/v1/projects/" + catalog.projects.image_demo.id + "/preannotate" ||
      url.pathname ===
        "/api/v1/tasks/" + catalog.projects.image_demo.tasks.spare_1.id + "/predictions" ||
      url.pathname === "/api/v1/async-jobs"
    ) {
      apiEvidence.push({
        method: response.request().method(),
        path: url.pathname,
        status: response.status(),
      });
    }
  };
  const onConsole = (message: import("@playwright/test").ConsoleMessage) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  };
  const onPageError = (error: Error) => consoleErrors.push(error.message);
  page.on("response", onResponse);
  page.on("console", onConsole);
  page.on("pageerror", onPageError);

  try {
    const project = catalog.projects.image_demo;
    const task = project.tasks.spare_1;
    const token = await page.evaluate(() => localStorage.getItem("token"));
    if (!token) throw new Error("[ai-preannotate] 缺少隔离录制用户 token");
    const headers = { Authorization: `Bearer ${token}` };
    const predictionPath = `/api/v1/tasks/${task.id}/predictions`;
    const baselineResponse = await page.request.get(`${apiBase}${predictionPath}`, { headers });
    apiEvidence.push({ method: "GET", path: predictionPath, status: baselineResponse.status() });
    if (!baselineResponse.ok()) {
      throw new Error(`[ai-preannotate] 读取预测基线失败: HTTP ${baselineResponse.status()}`);
    }
    const baselinePredictions = (await baselineResponse.json()) as BatchPredictionEvidence[];
    const baselinePredictionIds = new Set(baselinePredictions.map((prediction) => prediction.id));

    // ── Step 1：进入 AI 预标注入口 ───────────────────────────────
    await page.goto("/ai-pre");
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(800);
    const drawStartMs = Date.now();

    // ── Step 2：选择项目 ─────────────────────────────────────────
    await page.getByText(project.name, { exact: true }).first().click();
    await page.getByRole("heading", { name: project.name }).waitFor({ timeout: 5000 });
    await page.waitForTimeout(900);

    // ── Step 3：真实环境优先选可批量运行的 YOLO 后端 ──────────────
    // image_demo 的主后端是交互式 SAM3（batchable=false），它适合工作台智能工具，
    // 不适合批量预标。录制环境同时启用 YOLO 时显式切换，避免按钮因源模型不可批量而禁用。
    // 配置面板里可能同时存在源模型和隐藏的下游阶段选择器；直接从可用 option 反查
    // 所属 select，比依赖 CSS module 包装层中的 label 文本更稳定。
    const backendSelect = page
      .locator("select:visible")
      .filter({ has: page.locator('option:has-text("yolo-backend")') })
      .first();
    let yoloValue: string;
    if (await backendSelect.count()) {
      await expect(backendSelect).toBeVisible({ timeout: 10_000 });
      const yoloOption = backendSelect
        .locator("option")
        .filter({ hasText: "yolo-backend" })
        .first();
      const selectedValue = await yoloOption.getAttribute("value");
      if (!selectedValue) throw new Error("[ai-preannotate] yolo-backend 选项缺少 value");
      yoloValue = selectedValue;
      await backendSelect.selectOption(yoloValue);
      await expect(backendSelect).toHaveValue(yoloValue);
    } else {
      // With backendRequirements=none the flow-owned setup enables YOLO before
      // navigation, so the page renders the project backend badge instead of a
      // backend selector. Read the enabled registry id through the public API.
      await expect(page.getByText("yolo-backend", { exact: true }).first()).toBeVisible({
        timeout: 10_000,
      });
      const availablePath = `/api/v1/projects/${project.id}/ml-backends/available`;
      const availableResponse = await page.request.get(`${apiBase}${availablePath}`, { headers });
      apiEvidence.push({ method: "GET", path: availablePath, status: availableResponse.status() });
      if (!availableResponse.ok()) {
        throw new Error(
          `[ai-preannotate] 读取可用 backend 失败: HTTP ${availableResponse.status()}`,
        );
      }
      const available = (await availableResponse.json()) as {
        items?: Array<{ enabled?: boolean; backend?: { id?: string; name?: string } }>;
      };
      const yolo = available.items?.find(
        (item) => item.enabled && item.backend?.name === "yolo-backend" && item.backend.id,
      );
      if (!yolo?.backend?.id) {
        throw new Error("[ai-preannotate] 项目未启用 yolo-backend");
      }
      yoloValue = yolo.backend.id;
    }
    await page.waitForTimeout(900);

    // ── Step 4：选择批次 ─────────────────────────────────────────
    const batchRow = page.locator("li").filter({ hasText: project.batches.active.display_id });
    await batchRow.getByRole("checkbox").click();
    // 类别筛选现在是可选的模型原生类别白名单；截图 stub 不预热时按
    // “检出全部类别”运行，避免录制流程依赖具体模型的 model.names。
    await page.waitForTimeout(900);

    // ── Step 5：点击发起预标注 ───────────────────────────────────
    const startBtn = page.getByRole("button", { name: /跑预标（1 批）/ });
    await startBtn.waitFor({ state: "visible", timeout: 5000 });
    await expect(startBtn).toBeEnabled({ timeout: 15_000 });
    const dispatchResponse = page.waitForResponse(
      (response) => {
        const url = new URL(response.url());
        return (
          response.request().method() === "POST" &&
          url.pathname === `/api/v1/projects/${project.id}/preannotate`
        );
      },
      { timeout: 15_000 },
    );
    await startBtn.click();
    const dispatched = await dispatchResponse;
    if (!dispatched.ok()) {
      throw new Error(`[ai-preannotate] 预标注派发失败: HTTP ${dispatched.status()}`);
    }
    const dispatchedBody = (await dispatched.json()) as { job_id?: string };
    if (!dispatchedBody.job_id) {
      throw new Error("[ai-preannotate] 派发响应缺少 job_id");
    }
    const jobId = dispatchedBody.job_id;

    // The trigger returns the Celery task id, while the async-jobs list exposes
    // the persisted terminal row. Match both IDs so a stale completed row can
    // never satisfy this recording.
    let terminalJob: BatchAsyncJobEvidence | null = null;
    const jobListPath = "/api/v1/async-jobs";
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `${apiBase}${jobListPath}?kind=batch_predict&project_id=${project.id}&limit=50&offset=0`,
            { headers },
          );
          apiEvidence.push({ method: "GET", path: jobListPath, status: response.status() });
          if (!response.ok()) throw new Error(`async-jobs: HTTP ${response.status()}`);
          const body = (await response.json()) as { items?: BatchAsyncJobEvidence[] };
          terminalJob = body.items?.find((item) => item.celery_task_id === jobId) ?? null;
          return terminalJob?.status ?? "pending";
        },
        { timeout: 120_000, intervals: [1_000, 2_000, 3_000] },
      )
      .toMatch(/^(completed|failed|cancelled)$/);
    if (!terminalJob) throw new Error("[ai-preannotate] 未找到本次 batch_predict async job");
    assertSecretFreeJobData(terminalJob);
    expect(terminalJob.status).toBe("completed");
    const successCount = Number(terminalJob.result.success_count ?? 0);
    const failedCount = Number(terminalJob.result.failed_count ?? 0);
    expect(successCount).toBeGreaterThan(0);
    expect(failedCount).toBe(0);

    let predictions: BatchPredictionEvidence[] = [];
    await expect
      .poll(
        async () => {
          const response = await page.request.get(`${apiBase}${predictionPath}`, { headers });
          apiEvidence.push({ method: "GET", path: predictionPath, status: response.status() });
          if (!response.ok()) throw new Error(`predictions: HTTP ${response.status()}`);
          predictions = (await response.json()) as BatchPredictionEvidence[];
          return predictions.filter((prediction) => !baselinePredictionIds.has(prediction.id))
            .length;
        },
        { timeout: 30_000, intervals: [500, 1_000, 2_000] },
      )
      .toBeGreaterThan(0);
    const newPredictions = predictions.filter(
      (prediction) => !baselinePredictionIds.has(prediction.id),
    );
    expect(newPredictions.length).toBeGreaterThanOrEqual(Math.min(successCount, 1));
    for (const prediction of newPredictions) {
      expect(prediction.source).toBe("ml_backend");
      expect(prediction.model_version).toBeTruthy();
      expect(prediction.result?.length ?? 0).toBeGreaterThan(0);
      if (prediction.ml_backend_id) expect(prediction.ml_backend_id).toBe(yoloValue);
    }

    await page.waitForTimeout(1_500);

    // ── Step 6：查看历史列表 ─────────────────────────────────────
    await page.getByRole("button", { name: "历史 job" }).click();
    await page.waitForURL(/\/ai-pre\/jobs\?project_id=/, { timeout: 5000 });
    await page.waitForLoadState("networkidle");
    // API 已把本次任务核对到 completed；页面这里只确认同一项目最新行显示完成。
    const completedRow = page.locator("tbody tr").filter({ hasText: project.name }).first();
    await completedRow.waitFor({ state: "visible", timeout: 10_000 });
    await completedRow
      .locator("span.bg-status-positive-soft")
      .filter({ hasText: /^已完成$/ })
      .waitFor({ timeout: 10_000 });
    await page.waitForTimeout(1_200);

    // 终态不是链路终点：打开 job 详情核对成功数，再进入工作台查看落到任务上的候选。
    await completedRow.click();
    const jobDialog = page.getByRole("dialog", { name: "后台任务详情" });
    await jobDialog.waitFor({ timeout: 10_000 });
    await expect(jobDialog).toContainText(/成功\s+[1-9]\d*\s*\/\s*失败\s+0/);
    await page.waitForTimeout(1_500);
    await page.getByRole("button", { name: "关闭" }).click();

    await completedRow.getByTitle("去工作台").click();
    await page.waitForURL(/\/projects\/[^/]+\/annotate/, { timeout: 10_000 });
    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
    await page.waitForFunction(
      () =>
        Number(
          document
            .querySelector('[data-testid="workbench-stage"]')
            ?.getAttribute("data-ai-box-count"),
        ) > 0,
      undefined,
      { timeout: 15_000 },
    );
    await page.waitForTimeout(2_000);
    const predictionIds = newPredictions.map((prediction) => prediction.id);
    if (consoleErrors.length > 0) {
      throw new Error(`[ai-preannotate] 浏览器 console/page 错误: ${consoleErrors.join(" | ")}`);
    }
    return {
      drawStartMs,
      drawEndMs: Date.now(),
      evidence: {
        endpoint: `POST /api/v1/projects/${project.id}/preannotate`,
        job_id: jobId,
        task_id: task.id,
        selected_backend_id: yoloValue,
        job: {
          id: terminalJob.id,
          kind: terminalJob.kind,
          status: terminalJob.status,
          celery_task_id: terminalJob.celery_task_id,
          payload_keys: Object.keys(terminalJob.payload).sort(),
          result_keys: Object.keys(terminalJob.result).sort(),
          result: {
            success_count: successCount,
            failed_count: failedCount,
            duration_ms:
              typeof terminalJob.result.duration_ms === "number"
                ? terminalJob.result.duration_ms
                : null,
          },
          payload_secret_free: true,
        },
        predictions: {
          endpoint: `GET ${predictionPath}`,
          ids: predictionIds,
          count: newPredictions.length,
          shape_count: newPredictions.reduce(
            (total, prediction) => total + (prediction.result?.length ?? 0),
            0,
          ),
          source: [...new Set(newPredictions.map((prediction) => prediction.source ?? "unknown"))],
          model_versions: [
            ...new Set(newPredictions.map((prediction) => prediction.model_version ?? "unknown")),
          ],
          result_sha256: createHash("sha256").update(JSON.stringify(newPredictions)).digest("hex"),
        },
        api: apiEvidence,
        console: { errors: consoleErrors },
      },
    };
  } finally {
    page.off("response", onResponse);
    page.off("console", onConsole);
    page.off("pageerror", onPageError);
  }
}
