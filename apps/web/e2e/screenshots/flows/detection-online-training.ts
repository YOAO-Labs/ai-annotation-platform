import { expect, type Page } from "@playwright/test";
import { createHash } from "node:crypto";

interface Project {
  id: string;
  display_id: string;
  name: string;
}
interface Dataset {
  id: string;
  name: string;
  status: string;
  task: string;
  validation_digest?: string;
}
interface Run {
  id: string;
  name: string;
  status: string;
  error_message?: string;
  initialization_model_id?: string;
}
interface FrozenDataset extends Dataset {
  manifest: {
    samples: Array<{ task_id: string; split: string }>;
    summary: Record<string, number>;
    validation_digest: string;
  };
}
interface RunDetail {
  run: Run;
  attempts: Array<{ id: string; status: string; error_message?: string }>;
  evaluation?: unknown;
}

/** Private COCO8 demonstration. The caller owns and later destroys the isolated runtime. */
export async function runDetectionOnlineTraining(page: Page, options: { marketing: boolean }) {
  page.setDefaultTimeout(20_000);
  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  const target = new URL(process.env.SCREENSHOT_DATABASE_URL ?? "");
  if (!target.pathname.endsWith("_e2e") && !target.pathname.endsWith("_test"))
    throw new Error("Detection demonstration requires a disposable capture database");
  const token = await page.evaluate(() => localStorage.getItem("token"));
  if (!token) throw new Error("Capture authentication missing");
  const headers = { Authorization: `Bearer ${token}` };
  const calls: Array<{ method: string; path: string; status: number }> = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/v1/") && response.request().method() !== "GET")
      calls.push({ method: response.request().method(), path, status: response.status() });
  });
  async function api<T>(path: string): Promise<T> {
    const response = await page.request.get(`${apiBase}/api/v1${path}`, { headers });
    if (!response.ok()) throw new Error(`Demo read ${path}: HTTP ${response.status()}`);
    return response.json() as Promise<T>;
  }
  const hold = (seconds = 2) => page.waitForTimeout(options.marketing ? seconds * 1000 : 250);
  const chapters: Array<{ title: string; at_epoch_ms: number }> = [];
  async function chapter(title: string, subtitle: string) {
    console.log(`[detection-demo] ${title}`);
    chapters.push({ title, at_epoch_ms: Date.now() });
    await page.evaluate(
      ({ title, subtitle }) => {
        let element = document.getElementById("recording-chapter");
        if (!element) {
          element = document.createElement("aside");
          element.id = "recording-chapter";
          element.setAttribute("aria-hidden", "true");
          element.style.cssText =
            "position:fixed;bottom:16px;left:240px;z-index:2147483646;pointer-events:none;max-width:660px;padding:12px 18px;border-radius:10px;background:rgba(12,16,22,.94);color:#f5f7fa;border:1px solid rgba(255,255,255,.2);font:14px/1.6 system-ui;box-shadow:0 8px 30px rgba(0,0,0,.3)";
          document.body.appendChild(element);
        }
        element.replaceChildren();
        const heading = document.createElement("strong");
        heading.textContent = title;
        const caption = document.createElement("div");
        caption.textContent = subtitle;
        caption.style.cssText = "font-size:12px;color:#c9d2df";
        element.append(heading, caption);
      },
      { title, subtitle },
    );
    await hold(2.5);
  }
  const projects = await api<Project[]>("/projects");
  const project = projects.find((item) => item.display_id === "P-COCO8-TRAIN");
  if (!project) throw new Error("Run the online-training seed in the isolated capture environment");
  const pid = project.id;
  const projectPath = `/projects/${pid}`;
  const datasets = await api<{ items: Dataset[] }>(
    `${projectPath}/training-datasets?task=detect&limit=100`,
  );
  const baseline = datasets.items.find(
    (item) => item.status === "ready" && item.name.includes("初始数据"),
  );
  if (!baseline) throw new Error("The pinned 4 train / 4 val baseline is missing");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(11, 19);

  await page.goto(`${projectPath}/learning?tab=data`);
  await expect(page.getByRole("heading", { name: project.name, exact: true })).toBeVisible();
  await expect(
    page.getByRole("link", { name: `查看详情：${baseline.name}`, exact: true }),
  ).toBeVisible();
  const drawStartMs = Date.now();
  await chapter(
    "01 / 准备检测训练数据",
    "COCO8 · 8 张已审核图片 · 4 张训练 / 4 张验证；本视频用于演示流程。",
  );
  await page.getByRole("button", { name: "准备新版本", exact: true }).click();
  const datasetDialog = page.getByRole("dialog", { name: "新建数据版本", exact: true });
  await datasetDialog.getByLabel("版本名称", { exact: true }).fill(`检测首轮数据-${stamp}`);
  await datasetDialog.getByLabel("继承数据划分").selectOption(baseline.id);
  await hold(2);
  await datasetDialog.getByRole("button", { name: "检查训练数据", exact: true }).click();
  await expect(datasetDialog.getByText("数据检查通过", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await hold(2);
  const versionResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `/api/v1${projectPath}/training-datasets`,
  );
  await datasetDialog.getByRole("button", { name: "创建数据版本", exact: true }).click();
  const versionCreated = await versionResponse;
  expect(versionCreated.ok()).toBe(true);
  const version = (await versionCreated.json()) as Dataset;
  await expect
    .poll(
      async () => (await api<Dataset>(`${projectPath}/training-datasets/${version.id}`)).status,
      { timeout: 90_000, intervals: [1000, 2000] },
    )
    .toBe("ready");
  const frozen = await api<FrozenDataset>(`${projectPath}/training-datasets/${version.id}`);
  await page.goto(`${projectPath}/learning/data/${version.id}`);
  await hold(3);

  await page.goto(`${projectPath}/learning/new`);
  const dialog = page.getByRole("dialog", { name: "新建训练实验", exact: true });
  await expect(dialog).toBeVisible();
  await chapter(
    "02 / 配置并开始真实训练",
    "选择目标检测、YOLO11n 和预训练权重；打开逐轮训练集评估。",
  );
  await dialog.getByLabel("实验任务").selectOption("detect");
  await dialog.getByLabel("实验名称", { exact: true }).fill(`detect-在线学习演示-${stamp}`);
  await dialog.getByLabel("训练数据").selectOption(version.id);
  await dialog.getByLabel("训练器").selectOption("ultralytics");
  await dialog.getByLabel("模型家族").selectOption("yolo11");
  await dialog.getByLabel("基础模型").selectOption("yolo11n");
  await hold(3);
  await dialog.getByRole("button", { name: "下一步", exact: true }).click();
  await dialog.locator("#training-parameter-epochs").fill("8");
  await dialog.locator("#training-parameter-batch").fill("2");
  await dialog.getByLabel("搜索训练参数", { exact: true }).fill("evaluate_train");
  await dialog.locator("#training-parameter-evaluate_train").check();
  await hold(2);
  await dialog.getByLabel("搜索训练参数", { exact: true }).fill("");
  await dialog.getByLabel("仅看已修改", { exact: true }).check();
  await hold(3);
  await dialog.getByRole("button", { name: "下一步", exact: true }).click();
  await dialog.getByRole("button", { name: "检查训练配置", exact: true }).click();
  await expect(dialog.getByText("训练检查通过 · 请核对最终配置", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await hold(3);
  const runResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `/api/v1${projectPath}/training-runs`,
  );
  await dialog.getByRole("button", { name: "开始训练", exact: true }).click();
  const created = await runResponse;
  expect(created.ok()).toBe(true);
  const run = (await created.json()) as Run;
  await expect(dialog).not.toBeVisible({ timeout: 15_000 });
  await page.goto(`${projectPath}/learning/experiments/${run.id}`);
  await chapter(
    "03 / 观察训练与验证",
    "真实执行器逐轮回传 Precision、Recall、mAP 和损失；训练集与验证集分别展示。",
  );
  await expect
    .poll(
      async () => {
        const detail = await api<RunDetail>(`${projectPath}/training-runs/${run.id}`);
        if (["failed", "cancelled"].includes(detail.run.status))
          throw new Error(JSON.stringify(detail.attempts));
        return detail.run.status;
      },
      { timeout: 240_000, intervals: [1500, 3000] },
    )
    .toBe("succeeded");
  const detail = await api<RunDetail>(`${projectPath}/training-runs/${run.id}`);
  const attempt = detail.attempts.find((item) => item.status === "succeeded");
  if (!attempt) throw new Error("No successful execution");
  const trainChart = page.getByText("训练表现", { exact: true });
  await expect(trainChart).toBeVisible({ timeout: 20_000 });
  await trainChart.evaluate((element) => element.scrollIntoView({ block: "start" }));
  await hold(5);
  await page
    .getByText("验证表现", { exact: true })
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await hold(5);
  await page.getByRole("tab", { name: "产物与记录", exact: true }).click();
  await hold(4);

  await page.goto(
    `${projectPath}/learning?tab=models&register_run=${run.id}&register_attempt=${attempt.id}`,
  );
  await chapter(
    "04 / 将训练结果回流为项目模型",
    "直接引用已冻结的最佳权重，绑定同名类别；无需下载、重新上传。",
  );
  const modelDialog = page.getByRole("dialog", { name: "从训练结果添加模型", exact: true });
  await expect(modelDialog).toBeVisible();
  await modelDialog.getByLabel("模型名称", { exact: true }).fill(`YOLO11n 检测演示-${stamp}`);
  await expect(modelDialog.getByRole("button", { name: "按同名绑定", exact: true })).toBeEnabled();
  await modelDialog.getByRole("button", { name: "按同名绑定", exact: true }).click();
  await hold(3);
  const modelResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `/api/v1${projectPath}/trained-models`,
  );
  await modelDialog.getByRole("button", { name: "从训练结果添加模型", exact: true }).click();
  const registered = await modelResponse;
  expect(registered.ok()).toBe(true);
  const model = (await registered.json()) as { id: string; name: string };
  await expect(modelDialog).not.toBeVisible();
  await page.goto(`${projectPath}/learning/models/${model.id}`);
  await chapter(
    "05 / 加载试用与默认预标",
    "核对固定验证图片的真实预测结果，再明确选择该检测模型用于后续预标注。",
  );
  const trialResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname.endsWith(`/trained-models/${model.id}/deploy`),
    { timeout: 180_000 },
  );
  await page.getByRole("button", { name: "加载模型并试用", exact: true }).click();
  const trial = await trialResponse;
  if (!trial.ok()) throw new Error(`Model trial HTTP ${trial.status()}: ${await trial.text()}`);
  const deployment = (await trial.json()) as { id: string; status: string; trial?: unknown };
  expect(deployment.status).toBe("ready");
  const trialConfirmation = page.getByLabel("已查看固定验证样本，确认用于后续人工试用", {
    exact: true,
  });
  await trialConfirmation.scrollIntoViewIfNeeded();
  await hold(6);
  await trialConfirmation.check();
  await page.getByRole("button", { name: "设为默认预标模型", exact: true }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toBeVisible();
  await hold(2);
  await confirm.getByRole("button", { name: "设为默认", exact: true }).click();
  await expect(page.getByRole("button", { name: "已设为默认预标", exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await hold(3);

  const tasks = await api<{ items: Array<{ id: string; display_id: string; file_name: string }> }>(
    `/tasks?project_id=${pid}&limit=100`,
  );
  const nextTask = tasks.items.find((item) => item.display_id === "T-DEMO-DETECT-ITERATE");
  if (!nextTask)
    throw new Error("Prepare the unlabelled next-round road photograph before recording");
  await page.goto(`${projectPath}/learning?tab=models`);
  await chapter(
    "06 / 用回流模型预标下一张图片",
    "新增的道路图片未参加首轮训练；使用刚设定的默认检测模型产生真实候选。",
  );
  const predictionPanel = page.getByRole("region", { name: "使用默认预标模型", exact: true });
  await predictionPanel
    .getByRole("checkbox", { name: `${nextTask.display_id} · ${nextTask.file_name}`, exact: true })
    .check();
  await hold(2);
  const predictionResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `/api/v1${projectPath}/preannotate`,
  );
  await predictionPanel.getByRole("button", { name: "用默认模型预标注", exact: true }).click();
  const dispatched = await predictionResponse;
  expect(dispatched.ok()).toBe(true);
  const predictionJob = (await dispatched.json()) as { job_id: string };
  await expect
    .poll(
      async () => {
        const job = await api<{ status: string; error_message?: string }>(
          `/async-jobs/${predictionJob.job_id}`,
        );
        if (["failed", "cancelled"].includes(job.status)) throw new Error(JSON.stringify(job));
        return job.status;
      },
      { timeout: 180_000, intervals: [1000, 2000] },
    )
    .toBe("completed");
  const predictions = await api<
    Array<{ id: string; model_version: string; result: Array<{ shape_index: number }> }>
  >(`/tasks/${nextTask.id}/predictions`);
  expect(predictions.length).toBeGreaterThan(0);
  expect(predictions.some((prediction) => prediction.result.length > 0)).toBe(true);
  await hold(3);
  await page.goto(`${projectPath}/annotate?task=${nextTask.id}`);
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 30_000 });
  await chapter(
    "07 / 人工复核、采纳和修正",
    "候选只是建议；逐项查看后采纳，微调目标框并提交。修正会进入下一轮冻结快照。",
  );
  const candidates = page.locator('[data-testid^="box-list-item-pred-"]');
  await expect(candidates.first()).toBeVisible({ timeout: 20_000 });
  const candidateCount = Number(await stage.getAttribute("data-ai-box-count"));
  expect(candidateCount).toBeGreaterThan(0);
  expect(candidateCount).toBeLessThanOrEqual(
    predictions.reduce((count, prediction) => count + prediction.result.length, 0),
  );
  const acceptedIds: string[] = [];
  for (let i = 0; i < candidateCount; i++) {
    await page.getByTitle("跳转到 AI 待审", { exact: true }).click();
    await candidates.first().click();
    await hold(i === 0 ? 3 : 1);
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    const acceptedResponse = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        /\/predictions\/[^/]+\/accept$/.test(new URL(r.url()).pathname),
    );
    await page.keyboard.press("A");
    const accepted = await acceptedResponse;
    expect(accepted.ok()).toBe(true);
    const body = (await accepted.json()) as Array<{ id: string }>;
    acceptedIds.push(...body.map((item) => item.id));
    expect(body).toHaveLength(1);
    await expect(stage).toHaveAttribute("data-ai-box-count", String(candidateCount - i - 1));
  }
  expect(acceptedIds.length).toBeGreaterThan(0);
  await page.getByTitle("跳转到人工标注", { exact: true }).click();
  await page.getByTestId(`box-list-item-${acceptedIds[0]}`).click();
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const before = await api<Array<{ id: string; geometry: unknown }>>(
    `/tasks/${nextTask.id}/annotations`,
  );
  const changedResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "PATCH" &&
      new URL(r.url()).pathname.endsWith(`/annotations/${acceptedIds[0]}`),
  );
  await page.keyboard.press("ArrowRight");
  const changed = await changedResponse;
  expect(changed.ok()).toBe(true);
  await hold(3);
  const after = await api<Array<{ id: string; geometry: unknown }>>(
    `/tasks/${nextTask.id}/annotations`,
  );
  expect(after.find((item) => item.id === acceptedIds[0])?.geometry).not.toEqual(
    before.find((item) => item.id === acceptedIds[0])?.geometry,
  );
  await page.getByTestId("workbench-submit").click();
  await expect
    .poll(async () => (await api<{ status: string }>(`/tasks/${nextTask.id}`)).status, {
      timeout: 20_000,
    })
    .toBe("review");
  await hold(2);

  await page.goto(`${projectPath}/learning?tab=data`);
  await chapter(
    "08 / 冻结人工确认后的下一版数据",
    "负责人明确确认 9 张图片，继承首轮划分；已冻结的旧版本始终保留。",
  );
  await page.getByRole("button", { name: "准备新版本", exact: true }).click();
  await datasetDialog.getByLabel("版本名称", { exact: true }).fill(`检测人工修正回流-${stamp}`);
  await datasetDialog.getByLabel("标注纳入方式").selectOption("manager_confirmed");
  for (const task of tasks.items) {
    await datasetDialog
      .getByRole("checkbox", { name: `${task.display_id} · ${task.file_name}`, exact: true })
      .check();
  }
  await datasetDialog.getByLabel("继承数据划分").selectOption(version.id);
  await hold(3);
  await datasetDialog.getByRole("button", { name: "检查训练数据", exact: true }).click();
  await expect(datasetDialog.getByText("数据检查通过", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  const nextVersionResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `/api/v1${projectPath}/training-datasets`,
  );
  await datasetDialog.getByRole("button", { name: "创建数据版本", exact: true }).click();
  const nextVersionCreated = await nextVersionResponse;
  expect(nextVersionCreated.ok()).toBe(true);
  const nextVersion = (await nextVersionCreated.json()) as Dataset;
  await expect
    .poll(
      async () => (await api<Dataset>(`${projectPath}/training-datasets/${nextVersion.id}`)).status,
      { timeout: 90_000, intervals: [1000, 2000] },
    )
    .toBe("ready");
  const nextFrozen = await api<FrozenDataset>(`${projectPath}/training-datasets/${nextVersion.id}`);
  const addedSample = nextFrozen.manifest.samples.find((sample) => sample.task_id === nextTask.id);
  expect(addedSample?.split).toBe("train");
  expect(nextFrozen.manifest.validation_digest).toBe(frozen.manifest.validation_digest);
  await page.goto(
    `${projectPath}/learning/data/${nextVersion.id}?data_view=compare&data_baseline=${version.id}`,
  );
  await hold(4);

  await page.goto(`${projectPath}/learning/new`);
  await chapter(
    "09 / 使用项目模型继续下一轮训练",
    "选择人工确认后的新版本，用首轮项目模型初始化；新实验从第 0 轮开始。",
  );
  await dialog.getByLabel("实验任务").selectOption("detect");
  await dialog.getByLabel("实验名称", { exact: true }).fill(`detect-人工回流第二轮-${stamp}`);
  await dialog.getByLabel("训练数据").selectOption(nextVersion.id);
  await dialog.getByLabel("训练器").selectOption("ultralytics");
  await dialog.getByLabel("模型家族").selectOption("yolo11");
  await dialog.getByLabel("基础模型").selectOption("yolo11n");
  await dialog.getByLabel("初始化权重").selectOption(model.id);
  await expect(dialog.getByRole("button", { name: "下一步", exact: true })).toBeEnabled({
    timeout: 30_000,
  });
  await hold(3);
  await dialog.getByRole("button", { name: "下一步", exact: true }).click();
  await dialog.locator("#training-parameter-epochs").fill("5");
  await dialog.locator("#training-parameter-batch").fill("2");
  await dialog.getByLabel("搜索训练参数", { exact: true }).fill("evaluate_train");
  await dialog.locator("#training-parameter-evaluate_train").check();
  await dialog.getByRole("button", { name: "下一步", exact: true }).click();
  await dialog.getByRole("button", { name: "检查训练配置", exact: true }).click();
  await expect(dialog.getByText("训练检查通过 · 请核对最终配置", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await hold(4);
  const secondRunResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `/api/v1${projectPath}/training-runs`,
  );
  await dialog.getByRole("button", { name: "开始训练", exact: true }).click();
  const secondRunCreated = await secondRunResponse;
  expect(secondRunCreated.ok()).toBe(true);
  const secondRun = (await secondRunCreated.json()) as Run;
  await expect(dialog).not.toBeVisible();
  await page.goto(`${projectPath}/learning/experiments/${secondRun.id}`);
  await chapter(
    "10 / 观察回流后的第二轮训练",
    "使用首轮项目权重与修正后的数据，等待第二轮真实执行完成。",
  );
  await expect
    .poll(
      async () => {
        const current = await api<RunDetail>(`${projectPath}/training-runs/${secondRun.id}`);
        if (["failed", "cancelled"].includes(current.run.status))
          throw new Error(JSON.stringify(current.attempts));
        return current.run.status;
      },
      { timeout: 240_000, intervals: [1500, 3000] },
    )
    .toBe("succeeded");
  const secondDetail = await api<RunDetail>(`${projectPath}/training-runs/${secondRun.id}`);
  expect(secondDetail.run.initialization_model_id).toBe(model.id);
  await chapter(
    "完成 / 检测在线学习闭环",
    "两轮真实训练已完成；人工修正进入训练集，验证集保持一致。采用新模型前仍需评估与试用。",
  );
  await page
    .getByText("训练表现", { exact: true })
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await hold(5);
  await page
    .getByText("验证表现", { exact: true })
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await hold(5);

  const events = await api<{ items: unknown[] }>(
    `${projectPath}/training-runs/${run.id}/events?attempt_id=${attempt.id}`,
  );
  return {
    drawStartMs,
    drawEndMs: Date.now(),
    evidence: {
      project_id: pid,
      dataset_id: version.id,
      run_id: run.id,
      attempt_id: attempt.id,
      model_id: model.id,
      deployment_id: deployment.id,
      prediction_job_id: predictionJob.job_id,
      prediction_ids: predictions.map((p) => p.id),
      accepted_annotation_ids: acceptedIds,
      next_dataset_id: nextVersion.id,
      second_run_id: secondRun.id,
      second_initialization_model_id: secondDetail.run.initialization_model_id,
      first_dataset_summary: frozen.manifest.summary,
      next_dataset_summary: nextFrozen.manifest.summary,
      validation_digest: frozen.manifest.validation_digest,
      added_sample_split: addedSample?.split,
      training_events: events.items,
      training_events_sha256: createHash("sha256")
        .update(JSON.stringify(events.items))
        .digest("hex"),
      chapters,
      api: calls,
      console_errors: consoleErrors,
      private_development_demo: true,
    },
  };
}
