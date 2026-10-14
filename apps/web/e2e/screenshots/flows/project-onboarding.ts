import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { applyScreenshotTheme, waitForScreenshotReady } from "../environment";
import { installRecordingWorkbenchLayout } from "./_workbench-layout";
import { moveTo, runProjectCreateExistingResources } from "./project-create-existing-resources";
import type { DrawWindow } from "./rotated-bbox";

export const PROJECT_ONBOARDING_RECORDING_NAME = "道路车辆标注入门演示";

export async function runProjectOnboarding(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onCreated: (projectId: string) => void,
): Promise<DrawWindow> {
  await page.goto("/login?next=/projects");
  await applyScreenshotTheme(page, "dark");
  const account = page.getByPlaceholder("输入账号或邮箱");
  const password = page.getByPlaceholder("••••••••");
  const acknowledge = page.getByRole("button", { name: "知道了", exact: true });
  await page.addLocatorHandler(
    acknowledge,
    async (button) => {
      await moveTo(page, button);
      await button.click();
    },
    { times: 1 },
  );
  await expect(account).toBeVisible();
  await expect(account).toHaveValue("");
  await waitForScreenshotReady(page);

  // Login is part of this asset's content window, rather than capture setup.
  const drawStartMs = Date.now();
  await page.waitForTimeout(900);
  await moveTo(page, account);
  await account.pressSequentially(catalog.users.admin.email, { delay: 45 });
  await moveTo(page, password);
  await password.pressSequentially("123456", { delay: 70 });
  const loginButton = page.getByRole("button", { name: "登录", exact: true });
  await moveTo(page, loginButton);
  const loggedIn = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/auth/login",
  );
  await loginButton.click();
  expect((await loggedIn).ok(), "Real account login must succeed").toBe(true);
  await page.waitForURL("**/projects");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await installRecordingWorkbenchLayout(page, "both", { common: { petEnabled: false } });

  let createdProjectId: string | undefined;
  await runProjectCreateExistingResources(
    page,
    catalog,
    (projectId) => {
      createdProjectId = projectId;
      onCreated(projectId);
    },
    { projectName: PROJECT_ONBOARDING_RECORDING_NAME, navigate: false },
  );
  if (!createdProjectId) throw new Error("[project-onboarding] No project was created");

  const openWorkbench = page
    .getByTestId("project-wizard")
    .getByRole("button", { name: "打开工作台", exact: true });
  await moveTo(page, openWorkbench);
  await openWorkbench.click();
  await page.waitForURL(`**/projects/${createdProjectId}/annotate*`);
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toBeVisible({ timeout: 20_000 });
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await expect(stage).toHaveAttribute("data-image-identity", /.+/);
  await expect(page.getByTestId("tool-btn-box")).toBeVisible();
  await expect(
    page.getByText(PROJECT_ONBOARDING_RECORDING_NAME, { exact: true }).first(),
  ).toBeVisible();
  await waitForScreenshotReady(page);
  await page.waitForTimeout(2_000);
  return { drawStartMs, drawEndMs: Date.now() };
}
