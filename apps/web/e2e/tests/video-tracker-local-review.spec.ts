import { expectStableContextCapsule, openContextToolbar } from "../fixtures/context-toolbar";
import type { APIRequestContext, Page, Response } from "@playwright/test";

import { expect, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

interface AnnotationDto {
  id: string;
  version: number;
  geometry: {
    keyframes?: Array<{ frame_index: number; source?: string }>;
  };
}

function auth(token: string) {
  return { Authorization: `Bearer ${token}` };
}

async function annotations(request: APIRequestContext, taskId: string, token: string) {
  const response = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
    headers: auth(token),
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as AnnotationDto[];
}

async function setWindow(page: Page, from: number, to: number) {
  const start = page.getByTestId("tracker-review-from-frame");
  const end = page.getByTestId("tracker-review-to-frame");
  if (from > Number(await end.inputValue())) {
    await end.fill(String(to));
    await start.fill(String(from));
  } else {
    await start.fill(String(from));
    await end.fill(String(to));
  }
}

function decisionResponse(response: Response) {
  return (
    response.url().includes("/video-tracker-jobs/") &&
    response.url().endsWith("/decisions") &&
    response.request().method() === "POST"
  );
}

test("Tracker 可按目标/帧窗局部接受拒绝并二次确认人工帧", async ({
  page,
  request,
  seed,
}, testInfo) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  try {
    const video = await seed.videoTask(data.project_id);
    const fixture = await seed.trackerReview(video.task_id, data.admin_email);
    const token = await seed.accessToken(data.admin_email);
    const before = await annotations(request, video.task_id, token);
    // Staged tracker results are deterministic fixtures; no inference is requested.
    const configured = await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
      headers: auth(token),
      data: { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null },
    });
    expect(configured.ok(), await configured.text()).toBe(true);
    const disabled = await request.delete(
      `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
      { headers: auth(token) },
    );
    expect(disabled.status(), await disabled.text()).toBe(204);
    const pageErrors: string[] = [];
    const consoleErrors: Array<{ url: string; message: string }> = [];
    const apiErrors: Array<{ status: number; decision: boolean; reason?: string }> = [];
    const pendingApiErrors: Promise<void>[] = [];
    const decisions: Record<string, unknown>[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") {
        consoleErrors.push({ url: message.location().url, message: message.text() });
      }
    });
    page.on("response", (response) => {
      if (response.status() >= 400 && new URL(response.url()).pathname.startsWith("/api/v1/")) {
        pendingApiErrors.push(
          (async () => {
            const body = await response.json().catch(() => null);
            apiErrors.push({
              status: response.status(),
              decision: decisionResponse(response),
              reason: body?.detail?.reason,
            });
          })(),
        );
      }
    });
    page.on("request", (item) => {
      if (
        item.method() === "POST" &&
        item.url().endsWith(`/video-tracker-jobs/${fixture.job_id}/decisions`)
      ) {
        decisions.push(item.postDataJSON());
      }
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await seed.injectToken(page, data.admin_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${video.task_id}`);

    const review = page.getByTestId("video-tracker-review-bar");
    await expectStableContextCapsule(page, "tracker-review");
    await openContextToolbar(page, "tracker-review");
    await expect(review).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("tracker-review-job").selectOption(fixture.job_id);
    await expect(review).toContainText("已审 0/20");
    const contextBar = page.getByTestId("video-track-context-bar");
    await expect(contextBar).toBeVisible();
    const contextBounds = await contextBar.boundingBox();
    const reviewBounds = await review.boundingBox();
    expect(contextBounds).not.toBeNull();
    expect(reviewBounds).not.toBeNull();
    expect(reviewBounds!.y).toBeGreaterThanOrEqual(contextBounds!.y + contextBounds!.height);

    // 仅接受 A 的 F10-F15；B 与窗口外必须保持不变。
    await page.getByTestId("tracker-review-instance-B").uncheck();
    await setWindow(page, 10, 15);
    const accepted = page.waitForResponse(
      (response) => decisionResponse(response) && response.status() === 200,
    );
    await page.getByTestId("tracker-review-accept").click();
    await accepted;
    await expect(review).toContainText("已审 6/20");
    await expect(page.getByTestId("tracker-review-instance-A")).toBeChecked();
    await expect(page.getByTestId("tracker-review-instance-B")).not.toBeChecked();
    await expect(page.getByTestId("tracker-review-from-frame")).toHaveValue("10");
    await expect(page.getByTestId("tracker-review-to-frame")).toHaveValue("15");
    await expect(page.getByTestId("tracker-review-scope-summary")).toContainText("所选待审 0");
    await expect(page.getByTestId("tracker-review-accept")).toBeDisabled();

    let rows = await annotations(request, video.task_id, token);
    const sourceA = rows.find((item) => item.id === fixture.source_annotation_ids[0]);
    const sourceB = rows.find((item) => item.id === fixture.source_annotation_ids[1]);
    expect(sourceA?.geometry.keyframes?.map((item) => item.frame_index)).toEqual([
      10, 11, 12, 13, 14, 15, 16,
    ]);
    expect(sourceB?.geometry.keyframes?.map((item) => item.frame_index)).toEqual([9]);
    expect(sourceA?.geometry.keyframes?.find((item) => item.frame_index === 16)).toEqual(
      before.find((item) => item.id === fixture.source_annotation_ids[0])?.geometry.keyframes?.[0],
    );

    // 部分状态可跨刷新恢复。
    await page.reload();
    await openContextToolbar(page, "tracker-review");
    await expect(review).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("tracker-review-job").selectOption(fixture.job_id);
    await expect(review).toContainText("已审 6/20");

    // 仅拒绝 B 的 F10-F15；annotation 保持字节语义不变。
    await page.getByTestId("tracker-review-instance-A").uncheck();
    await page.getByTestId("tracker-review-instance-B").check();
    await setWindow(page, 10, 15);
    const rejected = page.waitForResponse(
      (response) => decisionResponse(response) && response.status() === 200,
    );
    await page.getByTestId("tracker-review-discard").click();
    await rejected;
    await expect(review).toContainText("已审 12/20");
    await expect(page.getByTestId("tracker-review-instance-A")).not.toBeChecked();
    await expect(page.getByTestId("tracker-review-instance-B")).toBeChecked();
    await page.getByTestId("tracker-review-remaining-16-19").click();
    await expect(page.getByTestId("video-konva-stage")).toHaveAttribute(
      "data-video-frame-index",
      "16",
    );
    await expect(page.getByTestId("tracker-review-from-frame")).toHaveValue("10");
    await expect(page.getByTestId("tracker-review-to-frame")).toHaveValue("15");
    await expect(page.getByTestId("tracker-review-instance-A")).not.toBeChecked();
    await expect(page.getByTestId("tracker-review-instance-B")).toBeChecked();
    rows = await annotations(request, video.task_id, token);
    expect(
      rows
        .find((item) => item.id === fixture.source_annotation_ids[1])
        ?.geometry.keyframes?.map((item) => item.frame_index),
    ).toEqual([9]);

    // A/F16 是人工关键帧：第一次 409，确认后同 selector 以 override=true 成功。
    await page.getByTestId("tracker-review-instance-B").uncheck();
    // F3 retains explicit targets after revision; selecting A is a separate intent.
    await page.getByTestId("tracker-review-instance-A").check();
    await setWindow(page, 16, 16);
    const statuses: number[] = [];
    page.on("response", (response) => {
      if (decisionResponse(response)) statuses.push(response.status());
    });
    await page.getByTestId("tracker-review-accept").click();
    const overrideDialog = page.getByRole("alertdialog");
    await expect(overrideDialog).toBeVisible();
    await overrideDialog.getByRole("button", { name: "覆盖", exact: true }).click();
    await expect.poll(() => statuses, { timeout: 15_000 }).toEqual([409, 200]);
    await expect(review).toContainText("已审 13/20");

    rows = await annotations(request, video.task_id, token);
    const overridden = rows.find((item) => item.id === fixture.source_annotation_ids[0]);
    expect(overridden?.geometry.keyframes?.find((item) => item.frame_index === 16)?.source).toBe(
      "prediction",
    );

    const preview = await request.get(
      `${API_BASE}/api/v1/video-tracker-jobs/${fixture.job_id}/preview`,
      { headers: auth(token) },
    );
    expect(preview.ok(), await preview.text()).toBe(true);
    const finalPreview = await preview.json();
    expect(finalPreview).toMatchObject({
      status: "partially_reviewed",
      candidate_pending: 7,
      candidate_accepted: 7,
      candidate_rejected: 6,
    });
    expect(decisions).toHaveLength(4);
    expect(decisions[2]).toMatchObject({
      instance_ids: ["A"],
      from_frame: 16,
      to_frame: 16,
      override_manual: false,
    });
    expect(decisions[3]).toEqual({ ...decisions[2], override_manual: true });
    await page.reload();
    await openContextToolbar(page, "tracker-review");
    await expect(review).toBeVisible({ timeout: 20_000 });
    await expect(review).toContainText("已审 13/20");
    expect(await annotations(request, video.task_id, token)).toEqual(rows);
    await Promise.all(pendingApiErrors);
    expect(apiErrors).toEqual([
      { status: 409, decision: true, reason: "manual_keyframe_protected" },
    ]);
    expect(pageErrors).toEqual([]);
    expect(
      consoleErrors.filter(
        (error) =>
          !(
            error.url.endsWith(`/video-tracker-jobs/${fixture.job_id}/decisions`) &&
            /^Failed to load resource:.*status of 409\b/.test(error.message)
          ),
      ),
    ).toEqual([]);
    await testInfo.attach("tracker-local-review-evidence.json", {
      contentType: "application/json",
      body: JSON.stringify(
        {
          taskId: video.task_id,
          fixture,
          browser: page.context().browser()?.version(),
          decisions,
          statuses,
          consoleErrors,
          annotations: rows,
          preview: finalPreview,
        },
        null,
        2,
      ),
    });
  } finally {
    try {
      await page.goto("about:blank");
    } finally {
      await seed.owned();
    }
  }
});
