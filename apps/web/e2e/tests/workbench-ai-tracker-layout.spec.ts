import { panelCommand } from "../fixtures/workbench-panel-actions";
import { layoutCommand, openLayoutSettings } from "../helpers/workbench-layout";
import type { Page } from "@playwright/test";
import { expect, test } from "../fixtures/seed";

async function savedVideoWorkspace(page: Page) {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  const response = await page.request.get("/api/v1/auth/me/preferences", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).workbench.layout.workspace.contexts["annotate:video"];
}

test("AI 范围预览仅随实际可见的追踪面板显示", async ({ page, seed }, testInfo) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("response", (response) => {
    if (response.url().includes("/api/") && response.status() >= 400)
      errors.push(`${response.status()} ${new URL(response.url()).pathname}`);
  });
  const data = await seed.owned();
  const video = await seed.videoTask(data.project_id);
  // The shared seed binds an unreachable mock backend; this layout test needs no inference.
  const token = await seed.accessToken(data.admin_email);
  const disabled = await page.request.put(
    `/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}/enablement`,
    { headers: { Authorization: `Bearer ${token}` }, data: { enabled: false } },
  );
  expect(disabled.ok(), await disabled.text()).toBe(true);
  const clearedDefault = await page.request.patch(`/api/v1/projects/${data.project_id}`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { ml_backend_id: null },
  });
  expect(clearedDefault.ok(), await clearedDefault.text()).toBe(true);
  await seed.injectToken(page, data.admin_email);
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${video.task_id}`);
  const stage = page.getByTestId("video-konva-stage");
  await expect(stage).toBeVisible({ timeout: 30_000 });
  await layoutCommand(page, "视频追踪布局");
  const tracker = page.getByTestId("video-tracker-propagate-dialog");
  const wrapper = page.locator('[data-workbench-panel="video-tracker"]');
  const range = page.getByTestId("video-propagate-range");
  await expect(tracker).toBeVisible();
  await stage.hover();
  await expect(range).toBeVisible();
  const toggle = page.getByTestId("video-timeline-toggle");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(page.getByTestId("video-timeline-lane-propagation")).toBeVisible();
  await page.getByRole("button", { name: "下一帧", exact: true }).click();
  const frame = await stage.getAttribute("data-video-frame-index");
  await page.getByRole("tab", { name: "标注详情", exact: true }).click();
  await expect(wrapper).toHaveAttribute("aria-hidden", "true");
  await expect(tracker).toHaveCount(1);
  await expect(range).toHaveCount(0);
  await expect(page.getByTestId("video-timeline-lane-propagation")).toHaveCount(0);
  await stage.hover();
  await page.getByRole("button", { name: "下一帧", exact: true }).click();
  await expect(stage).not.toHaveAttribute("data-video-frame-index", frame!);
  await expect(range).toHaveCount(0);
  const pausedFrame = await stage.getAttribute("data-video-frame-index");
  await page.getByRole("button", { name: "播放 / 暂停", exact: true }).click();
  await expect(stage).not.toHaveAttribute("data-video-frame-index", pausedFrame!);
  await page.getByRole("button", { name: "播放 / 暂停", exact: true }).click();
  await expect(range).toHaveCount(0);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(range).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("hidden-tracker.png") });

  await page.getByRole("tab", { name: "视频追踪", exact: true }).click();
  await stage.hover();
  await expect(range).toBeVisible();
  await page.getByTestId("video-tool-btn-select").click();
  await stage.click({ position: { x: 20, y: 20 } });
  await expect(range).toBeVisible();
  await panelCommand(page, "视频追踪", "隐藏面板");
  await expect(range).toHaveCount(0);
  await page.getByTestId("workbench-ai-tracker").click();
  await stage.hover();
  await expect(range).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("visible-tracker.png") });
  expect(errors).toEqual([]);
});

test("图片 AI 审阅预设显示单例面板，图片上下文不暴露视频追踪", async ({ page, seed }) => {
  const data = await seed.owned();
  await seed.injectToken(page, data.admin_email);
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
  await expect(page.getByTestId("workbench-stage")).toBeVisible({ timeout: 30_000 });

  const aiWrapper = page.locator('[data-workbench-panel="ai-task"]');
  const identity = await aiWrapper.elementHandle();
  const settings = await openLayoutSettings(page);
  await expect(settings.getByRole("button", { name: "图片 AI 审阅", exact: true })).toBeVisible();
  await settings.locator("summary").filter({ hasText: "面板与高级布局" }).click();
  await expect(settings.getByRole("button", { name: "当前题 AI", exact: true })).toBeVisible();
  await expect(settings.getByRole("button", { name: "视频追踪", exact: true })).toHaveCount(0);
  await settings.getByRole("button", { name: "图片 AI 审阅", exact: true }).click();
  await expect(settings.getByRole("button", { name: "图片 AI 审阅", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await settings.getByRole("button", { name: "关闭设置", exact: true }).click();

  await expect(page.getByTestId("ai-prediction-popover")).toBeVisible();
  expect(await aiWrapper.evaluate((node, original) => node === original, identity)).toBe(true);
  await panelCommand(page, "当前题 AI", "隐藏面板");
  await expect(aiWrapper).toHaveAttribute("aria-hidden", "true");
  await expect(page.getByTestId("ai-prediction-popover")).toHaveCount(1);
  await page.getByTestId("workbench-ai-single").click();
  await expect(page.getByTestId("ai-prediction-popover")).toBeVisible();
  expect(await aiWrapper.evaluate((node, original) => node === original, identity)).toBe(true);
});

test("AI 与视频追踪使用同一 Dockview 工作区，隐藏和预设不重建业务内容", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const video = await seed.videoTask(data.project_id);
  await seed.injectToken(page, data.admin_email);
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${video.task_id}`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 30_000 });

  await page.getByTestId("workbench-ai-tracker").click();
  const tracker = page.getByTestId("video-tracker-propagate-dialog");
  await expect(tracker).toBeVisible();
  await tracker.getByTestId("tracker-direction-backward").click();
  await expect(tracker.getByTestId("tracker-direction-backward")).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  const trackerWrapper = page.locator('[data-workbench-panel="video-tracker"]');
  const trackerIdentity = await trackerWrapper.elementHandle();

  await panelCommand(page, "视频追踪", "隐藏面板");
  await expect(trackerWrapper).toHaveAttribute("aria-hidden", "true");
  await expect(tracker).toHaveCount(1);
  await page.getByTestId("workbench-ai-tracker").click();
  await expect(tracker).toBeVisible();
  expect(
    await trackerWrapper.evaluate((node, original) => node === original, trackerIdentity),
  ).toBe(true);
  await expect(tracker.getByTestId("tracker-direction-backward")).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await page.getByTestId("workbench-ai-single").click();
  const ai = page.getByTestId("ai-prediction-popover");
  await expect(ai).toBeVisible();
  await expect(tracker).toBeVisible();
  const aiWrapper = page.locator('[data-workbench-panel="ai-task"]');
  const aiIdentity = await aiWrapper.elementHandle();

  await layoutCommand(page, "视频追踪布局");
  await expect(tracker).toBeVisible();
  await expect(aiWrapper).toHaveAttribute("aria-hidden", "true");
  await expect(ai).toHaveCount(1);
  expect(
    await trackerWrapper.evaluate((node, original) => node === original, trackerIdentity),
  ).toBe(true);
  await page.getByTestId("workbench-ai-single").click();
  await expect(ai).toBeVisible();
  expect(await aiWrapper.evaluate((node, original) => node === original, aiIdentity)).toBe(true);

  await page.waitForTimeout(650);
  await expect
    .poll(async () => (await savedVideoWorkspace(page)).schemaVersion, { timeout: 20_000 })
    .toBe(5);
  const saved = await savedVideoWorkspace(page);
  expect(Object.keys(saved.snapshot.layout.panels).sort()).toEqual([
    "ai-task",
    "camera-view",
    "canvas",
    "class-palette",
    "discussion",
    "inspector",
    "task-queue",
    "tri-view",
    "video-tracker",
  ]);
  expect(saved.snapshot.visibilityIntent).toEqual({
    "ai-task": "shown",
    "camera-view": "hidden",
    "tri-view": "hidden",
    "video-tracker": "shown",
  });
});
