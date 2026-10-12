/**
 * 高清母版：创建项目并复用已有数据集与成员。
 */
import { expect, type Locator, type Page, type Response } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { movePointerAtRefreshRate } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

export const PROJECT_CREATE_RECORDING_NAME = "道路车辆质检推广演示";

const pointerByPage = new WeakMap<Page, { x: number; y: number }>();

export async function moveTo(page: Page, locator: Locator): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("[project-create-existing-resources] 目标控件不可见");
  const target = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await movePointerAtRefreshRate(page, pointerByPage.get(page) ?? { x: 110, y: 110 }, target, 220);
  pointerByPage.set(page, target);
}

function assertOk(response: Response, action: string): void {
  if (!response.ok()) {
    throw new Error(
      `[project-create-existing-resources] ${action}失败：HTTP ${response.status()} ${response.url()}`,
    );
  }
}

function exactAccountName(account: string): RegExp {
  const escaped = account.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|\\s)${escaped}(?:\\s|$)`);
}

export async function runProjectCreateExistingResources(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (projectId: string) => void,
  options: { projectName?: string; navigate?: boolean } = {},
): Promise<DrawWindow> {
  const projectNameValue = options.projectName ?? PROJECT_CREATE_RECORDING_NAME;
  const sourceProject = catalog.projects.image_demo;
  const dataset = Object.values(sourceProject.datasets)[0];
  if (!dataset) {
    throw new Error("[project-create-existing-resources] image_demo 缺少可复用的图片数据集");
  }

  if (options.navigate !== false) await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText(sourceProject.name, { exact: true }).first()).toBeVisible({
    timeout: 10_000,
  });

  const drawStartMs = Date.now();
  await page.waitForTimeout(900);

  const createEntry = page.getByRole("button", { name: "新建项目", exact: true });
  await moveTo(page, createEntry);
  await createEntry.click();

  const wizard = page.getByTestId("project-wizard");
  await expect(wizard).toBeVisible();
  await expect(wizard).toContainText("类型");

  const projectName = wizard.getByPlaceholder(/智能门店货架/);
  await moveTo(page, projectName);
  await projectName.click();
  await projectName.pressSequentially(projectNameValue, { delay: 45 });
  await expect(projectName).toHaveValue(projectNameValue);
  await page.waitForTimeout(750);

  const step1Next = wizard.getByRole("button", { name: /下一步/ });
  await moveTo(page, step1Next);
  await step1Next.click();
  await expect(wizard.getByPlaceholder("新增类别名（回车）")).toBeVisible();

  const classInput = wizard.getByPlaceholder("新增类别名（回车）");
  await moveTo(page, classInput);
  await classInput.click();
  await classInput.pressSequentially("car", { delay: 90 });
  await classInput.press("Enter");
  await expect(wizard.getByText("car", { exact: true })).toBeVisible();
  await page.waitForTimeout(900);

  const step2Next = wizard.getByRole("button", { name: /下一步/ });
  await moveTo(page, step2Next);
  await step2Next.click();
  await expect(wizard).toContainText("属性 schema 也按工具单位独立");
  await page.waitForTimeout(650);

  const step3Next = wizard.getByRole("button", { name: /下一步/ });
  await moveTo(page, step3Next);
  await step3Next.click();
  await expect(wizard.getByText("启用 AI 预标注", { exact: true })).toBeVisible();
  await page.waitForTimeout(650);

  const createButton = wizard.getByRole("button", { name: "创建", exact: true });
  await moveTo(page, createButton);
  const createdResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/projects",
    { timeout: 20_000 },
  );
  await createButton.click();
  const created = await createdResponse;
  assertOk(created, "创建项目");
  const createdBody = (await created.json()) as { id?: string; name?: string };
  if (!createdBody.id || createdBody.name !== projectNameValue) {
    throw new Error("[project-create-existing-resources] 创建结果与录制项目不匹配");
  }
  onCreated(createdBody.id);

  await expect(wizard.getByText(dataset.name, { exact: true })).toBeVisible({ timeout: 10_000 });
  const datasetChoice = wizard.getByRole("button").filter({ hasText: dataset.name });
  await moveTo(page, datasetChoice);
  await datasetChoice.click();
  await expect(wizard.getByRole("button", { name: "关联 1 个并继续" })).toBeVisible();
  await page.waitForTimeout(1_600);

  const linkButton = wizard.getByRole("button", { name: "关联 1 个并继续" });
  await moveTo(page, linkButton);
  const linkedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/datasets/${dataset.id}/link`,
    { timeout: 20_000 },
  );
  await linkButton.click();
  const linked = await linkedResponse;
  assertOk(linked, "关联已有数据集");
  const linkedBody = (await linked.json()) as { project_id?: string };
  if (linkedBody.project_id !== createdBody.id) {
    throw new Error("[project-create-existing-resources] 数据集未关联到本次创建的项目");
  }

  const annotator = wizard.getByRole("button", {
    name: exactAccountName(catalog.users.annotator.email),
  });
  const reviewer = wizard.getByRole("button", {
    name: exactAccountName(catalog.users.reviewer.email),
  });
  await expect(annotator).toBeVisible({ timeout: 10_000 });
  await expect(reviewer).toBeVisible({ timeout: 10_000 });

  await moveTo(page, annotator);
  await annotator.click();
  await moveTo(page, reviewer);
  await reviewer.click();
  const finishButton = wizard.getByRole("button", { name: "添加 2 位并完成" });
  await expect(finishButton).toBeVisible();
  await page.waitForTimeout(1_600);

  const memberResponses: Response[] = [];
  const captureMemberResponse = (response: Response) => {
    if (
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1/projects/${createdBody.id}/members`
    ) {
      memberResponses.push(response);
    }
  };
  page.on("response", captureMemberResponse);
  await moveTo(page, finishButton);
  await finishButton.click();
  await expect(wizard).toContainText("已关联 1 个数据集 · 已添加 2 位成员", {
    timeout: 20_000,
  });
  page.off("response", captureMemberResponse);
  if (memberResponses.length !== 2) {
    throw new Error(
      `[project-create-existing-resources] 应创建 2 条成员关系，实际捕获 ${memberResponses.length} 条`,
    );
  }
  memberResponses.forEach((response) => assertOk(response, "添加已有成员"));

  await expect(wizard.getByText(projectNameValue, { exact: true })).toBeVisible();
  await expect(wizard.getByRole("button", { name: "项目设置" })).toBeVisible();
  await page.waitForTimeout(2_000);

  return { drawStartMs, drawEndMs: Date.now() };
}
