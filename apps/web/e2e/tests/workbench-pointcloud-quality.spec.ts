import type { APIRequestContext, Page } from "@playwright/test";

import { expect, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

async function fixtureContext(
  request: APIRequestContext,
  taskId: string,
  token: string,
): Promise<{ annotationId: string; sceneId: string; frameIndex: number }> {
  const headers = { Authorization: `Bearer ${token}` };
  const annotations = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
    headers,
  });
  expect(annotations.ok()).toBeTruthy();
  const annotationId = ((await annotations.json()) as Array<{ id: string }>)[0]?.id;
  expect(annotationId).toBeTruthy();
  const manifest = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/point-cloud/manifest`, {
    headers,
  });
  expect(manifest.ok()).toBeTruthy();
  const body = (await manifest.json()) as { scene_id: string; frame_index: number };
  return { annotationId, sceneId: body.scene_id, frameIndex: body.frame_index };
}

async function installQualityRoutes(
  page: Page,
  context: {
    projectId: string;
    taskId: string;
    annotationId: string;
    sceneId: string;
    frameIndex: number;
  },
) {
  const runId = "00000000-0000-4000-8000-000000000101";
  const issueId = "00000000-0000-4000-8000-000000000102";
  const now = "2026-08-26T00:00:00Z";
  const issue = {
    id: issueId,
    run_id: runId,
    last_seen_run_id: runId,
    project_id: context.projectId,
    scene_id: context.sceneId,
    task_id: context.taskId,
    annotation_id: context.annotationId,
    annotation_version: 1,
    scene_track_id: null,
    track_revision: null,
    related_annotation_ids: [context.annotationId],
    source_versions: { [context.annotationId]: 1 },
    class_name: "car",
    code: "ground_clearance",
    rule_version: 1,
    severity: "warning",
    status: "open",
    frame_start: context.frameIndex,
    frame_end: context.frameIndex,
    metric: { clearance_m: 0.71, ground_z: -0.03 },
    threshold: { floating_m: 0.45 },
    evidence: { ground_sample_count: 48 },
    locator: {
      scene_id: context.sceneId,
      frame_index: context.frameIndex,
      task_id: context.taskId,
      annotation_id: context.annotationId,
      scene_track_id: null,
      camera: null,
      auxiliary_layers: ["ground"],
    },
    suggested_command: "show_ground_layer",
    resolution_reason: null,
    resolved_by_id: null,
    resolved_at: null,
    review_verdict: null,
    review_note: null,
    reviewed_by_id: null,
    reviewed_at: null,
    created_at: now,
    updated_at: now,
  };
  const run = {
    id: runId,
    project_id: context.projectId,
    async_job_id: "00000000-0000-4000-8000-000000000103",
    status: "completed",
    progress_pct: 100,
    scope_json: { scope: "scene_ids", scene_ids: [context.sceneId] },
    config_revision: 1,
    config_digest: "a".repeat(64),
    source_snapshot_digest: "b".repeat(64),
    summary: { issue_count: 1 },
    error_message: null,
    created_at: now,
    completed_at: now,
    reused: false,
  };
  const requests: Array<{ kind: string; body: Record<string, unknown> }> = [];
  const evaluation = {
    id: "00000000-0000-4000-8000-000000000104",
    project_id: context.projectId,
    created_by_id: "00000000-0000-4000-8000-000000000105",
    baseline_config_revision: 1,
    baseline_config_digest: "a".repeat(64),
    candidate_config_digest: "c".repeat(64),
    cutoff_at: now,
    sample_count: 2,
    summary: {
      changed_targets: [
        {
          code: "low_point_count",
          class_name: null,
          status: "promote",
          reasons: [],
          baseline: { decidable_count: 2, observed_false_positive_rate: 0.5 },
          candidate: { observed_false_positive_rate: 0, confirmed_retention: 1 },
        },
      ],
    },
    gate_status: "promote",
    gate_reasons: [],
    promoted_by_id: null,
    promoted_at: null,
    promoted_config_revision: null,
    created_at: now,
  };

  await page.route("**/api/v1/projects/*/point-cloud-quality/issues**", async (route) => {
    await route.fulfill({ status: 200, json: { items: [issue], total: 1 } });
  });
  await page.route("**/api/v1/projects/*/point-cloud-quality/runs", async (route) => {
    requests.push({
      kind: "run",
      body: route.request().postDataJSON() as Record<string, unknown>,
    });
    await route.fulfill({ status: 202, json: run });
  });
  await page.route("**/api/v1/projects/*/point-cloud-quality/runs/*", async (route) => {
    await route.fulfill({ status: 200, json: run });
  });
  await page.route("**/api/v1/point-cloud-quality/issues/*", async (route) => {
    requests.push({
      kind: "disposition",
      body: route.request().postDataJSON() as Record<string, unknown>,
    });
    const body = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      json: { ...issue, ...body, review_verdict: body.review_verdict ?? null },
    });
  });
  await page.route("**/api/v1/projects/*/point-cloud-quality/evaluations**", async (route) => {
    const request = route.request();
    if (request.url().endsWith("/promote")) {
      requests.push({ kind: "promote", body: {} });
      await route.fulfill({
        status: 200,
        json: { ...evaluation, promoted_at: now, promoted_config_revision: 2 },
      });
      return;
    }
    if (request.method() === "POST") {
      requests.push({
        kind: "evaluation",
        body: request.postDataJSON() as Record<string, unknown>,
      });
      await route.fulfill({ status: 201, json: evaluation });
      return;
    }
    await route.fulfill({ status: 200, json: { items: [], total: 0 } });
  });
  await page.route("**/api/v1/feedbacks", async (route) => {
    requests.push({
      kind: "feedback",
      body: route.request().postDataJSON() as Record<string, unknown>,
    });
    await route.fulfill({ status: 200, json: { id: "feedback-quality-e2e" } });
  });
  return { issueId, requests };
}

test.describe("workbench point-cloud quality", () => {
  test("quality filters query the current Scene without scanning or changing annotations", async ({
    page,
    request,
    seed,
  }) => {
    const data = await seed.owned();
    const lidar = await seed.seedLidar();
    const token = await seed.accessToken(data.admin_email);
    const taskId = lidar.lidar_task_ids[0];
    const context = await fixtureContext(request, taskId, token);
    const headers = { Authorization: `Bearer ${token}` };
    const annotationUrl = `${API_BASE}/api/v1/tasks/${taskId}/annotations`;
    const beforeResponse = await request.get(annotationUrl, { headers });
    expect(beforeResponse.ok()).toBe(true);
    const before = await beforeResponse.json();
    const writes: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/point-cloud-quality/") && req.method() !== "GET")
        writes.push(req.url());
    });
    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("scene-timeline-toggle").click();
    await page.getByTestId("scene-quality-open").click();
    const panel = page.getByTestId("point-cloud-quality-panel");
    await expect(panel).toBeVisible();
    const filtered = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname.endsWith("/point-cloud-quality/issues") &&
        url.searchParams.get("status") === "resolved" &&
        url.searchParams.get("severity") === "warning" &&
        url.searchParams.get("code") === "projection_residual"
      );
    });
    await panel.getByLabel("质量问题状态").selectOption("resolved");
    await panel.getByLabel("质量问题严重级别").selectOption("warning");
    await panel.getByLabel("质量问题规则").selectOption("projection_residual");
    const response = await filtered;
    expect(response.ok(), await response.text()).toBe(true);
    expect(new URL(response.url()).searchParams.get("scene_id")).toBe(context.sceneId);
    expect(new URL(response.url()).searchParams.has("task_id")).toBe(false);
    await expect(panel).toContainText("已加载警告 0");
    expect(writes).toEqual([]);
    const afterResponse = await request.get(annotationUrl, { headers });
    expect(afterResponse.ok()).toBe(true);
    expect(await afterResponse.json()).toEqual(before);
  });

  test("nuScenes 时间轴标记、定位、处置与 3D 讨论锚点形成闭环", async ({ page, request, seed }) => {
    const data = await seed.owned();
    const lidar = await seed.seedLidar();
    const token = await seed.accessToken(data.admin_email);
    const taskId = lidar.lidar_task_ids[0];
    const context = await fixtureContext(request, taskId, token);
    const routes = await installQualityRoutes(page, {
      projectId: lidar.lidar_project_id,
      taskId,
      ...context,
    });
    await seed.injectToken(page, data.admin_email);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("scene-timeline-toggle").click();
    await expect(page.getByTestId(`scene-timeline-quality-${context.frameIndex}`)).toBeVisible();

    await page.getByTestId("scene-quality-open").click();
    const panel = page.getByTestId("point-cloud-quality-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("穿地或悬浮");
    await panel.getByRole("button", { name: "定位", exact: true }).click();
    await expect(page.getByTestId(`box-list-item-${context.annotationId}`)).toHaveClass(
      /border-brand/,
    );

    await panel.getByText("扫描当前 Scene").click();
    await expect.poll(() => routes.requests.some((entry) => entry.kind === "run")).toBeTruthy();
    expect(routes.requests.find((entry) => entry.kind === "run")?.body).toEqual({
      scope: "scene_ids",
      scene_ids: [context.sceneId],
    });

    await panel.getByText("其他判定").click();
    await panel.getByPlaceholder("填写判定依据").fill("已确认为稀疏回波");
    await panel.getByRole("button", { name: "确认", exact: true }).click();
    await expect
      .poll(() => routes.requests.find((entry) => entry.kind === "disposition")?.body)
      .toEqual({
        status: "wont_fix",
        reason: "已确认为稀疏回波",
        review_verdict: "false_positive",
        review_note: "已确认为稀疏回波",
      });

    await panel.getByText("讨论").click();
    await panel.getByPlaceholder("记录判断或 @ 协作者").fill("请复核地面估计");
    await panel.getByText("发送").click();
    await expect
      .poll(() => routes.requests.find((entry) => entry.kind === "feedback")?.body)
      .toMatchObject({
        anchor_type: "point_cloud",
        project_id: lidar.lidar_project_id,
        task_id: taskId,
        annotation_id: context.annotationId,
        anchor_position: {
          frame: context.frameIndex,
          point_cloud_quality_issue_id: routes.issueId,
          scene_id: context.sceneId,
          auxiliary_layers: ["ground"],
        },
      });

    await panel.getByText("规则治理").click();
    await panel.getByLabel("最少点数").fill("4");
    await panel.getByText("生成候选评估").click();
    await expect
      .poll(() => routes.requests.find((entry) => entry.kind === "evaluation")?.body)
      .toMatchObject({
        candidate_config: {
          config_revision: 1,
          thresholds: { minimum_points: 4 },
        },
      });
    await expect(panel.getByText("保留 100%")).toBeVisible();
    await panel.getByText("晋级为项目配置").click();
    await expect.poll(() => routes.requests.some((entry) => entry.kind === "promote")).toBeTruthy();
  });
});
