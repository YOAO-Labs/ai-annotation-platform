import { openContextToolbar } from "../../fixtures/context-toolbar";
/**
 * 高清母版：项目启用批量后端并把它设为主后端后，工作台仍按 prompt
 * 把交互工具路由到支持该能力的 SAM 后端。
 *
 * 流程走真实项目设置、正式 API 与真实工作台能力解析；调用方在 finally
 * 重建 screenshots profile，避免主后端与启用关系泄漏到后续录制。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";
import {
  installRecordingWorkbenchLayout,
  waitForRecordingWorkbenchLayout,
} from "./_workbench-layout";

const BATCH_BACKEND_NAME = "yolo-backend";
const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

async function moveTo(page: Page, locator: Locator, durationMs = 300): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[project-ml-routing] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(
    page,
    pointerByPage.get(page) ?? { x: 96, y: 118 },
    target,
    durationMs,
  );
  pointerByPage.set(page, target);
}

function isEnablementRequest(url: string, projectId: string): boolean {
  const path = new URL(url).pathname;
  return (
    path.startsWith(`/api/v1/projects/${projectId}/ml-backends/`) && path.endsWith("/enablement")
  );
}

async function installSanitizedAvailableBackends(page: Page, projectId: string): Promise<void> {
  await page.route(`**/api/v1/projects/${projectId}/ml-backends/available`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    const upstream = await route.fetch();
    if (!upstream.ok()) {
      await route.fulfill({ response: upstream });
      return;
    }
    const body = (await upstream.json()) as {
      items?: Array<{ backend?: { name?: string; url?: string } }>;
    };
    for (const item of body.items ?? []) {
      const name = item.backend?.name;
      if (!item.backend || !name) continue;
      const slug = name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "");
      item.backend.url = `https://ml.example.invalid/${slug || "backend"}`;
    }
    await route.fulfill({ response: upstream, json: body });
  });
}

export async function runProjectMlRouting(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DrawWindow> {
  const project = catalog.projects.image_demo;
  const interactiveBackend = project.ml_backend;
  if (
    !interactiveBackend?.is_interactive ||
    !interactiveBackend.capabilities.supported_prompts?.includes("point")
  ) {
    throw new Error("[project-ml-routing] image_demo 主后端必须声明 point 交互能力");
  }

  await installSanitizedAvailableBackends(page, project.id);
  await page.goto(`/projects/${project.id}/settings?section=ml-backends`);
  await installRecordingWorkbenchLayout(page, "none");
  await expect(page.getByRole("heading", { name: project.name, exact: true })).toBeVisible({
    timeout: 10_000,
  });
  const quota = page.getByTestId("ml-backend-quota");
  await expect(quota).toContainText("已启用 1 /", { timeout: 10_000 });

  // drawTrim 会保留动作前 0.4 秒；先让刚挂载的设置页稳定，避免预卷带入骨架屏。
  await page.waitForTimeout(800);
  const drawStartMs = Date.now();
  await page.waitForTimeout(2_200);

  const manageButton = page.getByRole("button", { name: /管理 backend/ });
  await moveTo(page, manageButton);
  await manageButton.click();
  const dialog = page.getByRole("dialog", { name: "管理项目 ML backend" });
  await expect(dialog).toBeVisible();
  const batchToggle = dialog.getByRole("checkbox", {
    name: `启用 ${BATCH_BACKEND_NAME}`,
  });
  await expect(batchToggle).toBeVisible({ timeout: 10_000 });
  await expect(batchToggle).not.toBeChecked();
  await page.waitForTimeout(2_400);

  const enablementResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" && isEnablementRequest(response.url(), project.id),
    { timeout: 15_000 },
  );
  await moveTo(page, batchToggle);
  await batchToggle.click();
  const enabled = await enablementResponse;
  if (!enabled.ok()) {
    throw new Error(`[project-ml-routing] 启用批量后端失败: HTTP ${enabled.status()}`);
  }
  const enabledBody = (await enabled.json()) as { backend?: { id?: string; name?: string } };
  const batchBackendId = enabledBody.backend?.id;
  if (!batchBackendId || enabledBody.backend?.name !== BATCH_BACKEND_NAME) {
    throw new Error("[project-ml-routing] 启用响应未返回预期的 yolo-backend");
  }
  await expect(batchToggle).toBeChecked();
  await expect(page.getByText(`已启用「${BATCH_BACKEND_NAME}」`, { exact: true })).toBeVisible();
  await page.waitForTimeout(2_400);

  const closeDialog = dialog.getByRole("button", { name: "关闭" });
  await moveTo(page, closeDialog);
  await closeDialog.click();
  await expect(dialog).toBeHidden();

  const batchRow = page.getByRole("row").filter({ hasText: BATCH_BACKEND_NAME }).first();
  await expect(batchRow).toBeVisible({ timeout: 10_000 });
  await expect(quota).toContainText("已启用 2 /");
  await page.waitForTimeout(2_400);

  const setMain = batchRow.getByRole("button", { name: "设为主后端", exact: true });
  const projectResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname === `/api/v1/projects/${project.id}`,
    { timeout: 15_000 },
  );
  await moveTo(page, setMain);
  await setMain.click();
  const updated = await projectResponse;
  if (!updated.ok()) {
    throw new Error(`[project-ml-routing] 设置项目主后端失败: HTTP ${updated.status()}`);
  }
  await expect(
    page.getByText(`已设为项目主后端「${BATCH_BACKEND_NAME}」`, { exact: true }),
  ).toBeVisible();
  const mainBackendSelect = page.locator("select").first();
  await expect(mainBackendSelect).toHaveValue(batchBackendId);
  await expect(batchRow.getByText("主后端", { exact: true })).toBeVisible();

  // 在同一 page 打开工作台，确保设置和后续能力分流都进入同一份录制。
  const openWorkbench = page.getByRole("button", { name: "打开工作台", exact: true });
  await moveTo(page, openWorkbench);
  await openWorkbench.click();
  await page.waitForURL(new RegExp(`/projects/${project.id}/annotate`));
  const stage = page.getByTestId("workbench-stage");
  await stage.waitFor({ state: "visible", timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await waitForRecordingWorkbenchLayout(page, "none");
  await page.waitForTimeout(2_300);

  const singleTaskAi = page.getByTestId("workbench-ai-single");
  await moveTo(page, singleTaskAi);
  await singleTaskAi.click();
  const aiPanel = page.getByTestId("ai-prediction-popover");
  await expect(aiPanel).toBeVisible();
  await aiPanel.getByTestId("ai-prediction-advanced-toggle").click();
  const batchSelector = aiPanel.getByLabel("本次后端");
  await expect(batchSelector).toHaveValue(batchBackendId);
  await expect(batchSelector.locator("option:checked")).toHaveText(
    `${BATCH_BACKEND_NAME}（项目主后端）`,
  );
  await page.waitForTimeout(3_000);

  const closeAiPanel = aiPanel.getByTitle("关闭当前题 AI");
  await moveTo(page, closeAiPanel);
  await closeAiPanel.click();
  await expect(aiPanel).toBeHidden();

  const smartPoint = page.getByTestId("tool-btn-smart-point");
  await expect(smartPoint).toBeEnabled();
  await moveTo(page, smartPoint);
  await smartPoint.click();
  await openContextToolbar(page, "interactive");
  await page.getByTestId("interactive-toolbar-advanced-toggle").click();
  const interactiveToolbar = page.getByTestId("interactive-toolbar");
  await expect(interactiveToolbar).toBeVisible();
  const interactiveSelector = interactiveToolbar.getByTestId("ai-tool-backend-select");
  await expect(interactiveSelector).toBeDisabled();
  await expect(interactiveSelector).toHaveValue(interactiveBackend.name);
  await expect(interactiveSelector.locator("option:checked")).toHaveText(interactiveBackend.name);
  await page.waitForTimeout(4_200);

  return { drawStartMs, drawEndMs: Date.now() };
}
