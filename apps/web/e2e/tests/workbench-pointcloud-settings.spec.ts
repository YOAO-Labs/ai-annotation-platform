import { test, expect } from "../fixtures/seed";

test("点云设置盖过精修面板，阻断工具键并保存点径", async ({ page, seed }, info) => {
  test.setTimeout(60_000);
  const data = await seed.owned();
  const lidar = await seed.seedLidar();
  await seed.injectToken(page, data.admin_email);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
  await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
  await page
    .locator('[data-testid^="box-list-item-"]')
    .first()
    .click({ position: { x: 12, y: 16 } });
  const refinement = page.getByRole("button", { name: "框体精修" });
  if (await refinement.isVisible()) await refinement.click();
  await expect(page.getByTestId("tri-view-renderer-panel")).toBeVisible();
  await page.getByRole("button", { name: "工作台设置", exact: true }).click();
  const dialog = page.getByTestId("workbench-settings-dialog");
  await dialog.getByRole("tab", { name: "画布与视角", exact: true }).click();
  const size = dialog.getByTestId("setting-field-pointcloud.pointSize").getByRole("slider");
  await expect(dialog.getByTestId("setting-field-image.cssImageFilter")).toHaveCount(1);
  await expect(dialog.getByTestId("setting-field-video.autoFitOnResize")).toHaveCount(1);
  await size.scrollIntoViewIfNeeded();
  await expect(size).toBeVisible();
  const parent = await dialog
    .getByTestId("setting-field-pointcloud.colorizeWithCamera")
    .boundingBox();
  const child = await dialog.getByTestId("setting-field-pointcloud.colorizeContrast").boundingBox();
  expect(child!.x + child!.width).toBeLessThanOrEqual(parent!.x + parent!.width);
  await page.screenshot({
    path: info.outputPath("settings-pointcloud.png"),
    animations: "disabled",
  });
  await dialog.focus();
  await page.keyboard.press("b");
  await page.keyboard.press("Delete");
  await expect(page.getByTestId("three-d-tool-btn-select")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator('[data-testid^="box-list-item-"]')).toHaveCount(1);
  await size.focus();
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/me/preferences") && response.request().method() === "PATCH",
  );
  await page.keyboard.press("End");
  const value = await size.inputValue();
  await page.mouse.click(12, 450);
  await expect(dialog).toBeHidden();
  expect((await saved).ok()).toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "工作台设置", exact: true }).click();
  await dialog.getByRole("tab", { name: "画布与视角", exact: true }).click();
  await expect(size).toHaveValue(value);
});
