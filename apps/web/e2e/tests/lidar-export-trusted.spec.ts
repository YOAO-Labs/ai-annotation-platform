import { expect, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

test.describe("trusted LiDAR export", () => {
  test("Chromium 实际页面要求显式相机并展示严格预检", async ({ page, seed, request }) => {
    const data = await seed.owned();
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);

    // seed.owned() scopes this fixture to the test namespace, so the lidar
    // project's display name is namespaced as well. Resolve that exact
    // project through the API boundary and target its own list row, rather
    // than matching a prefix shared with neighboring owned fixtures.
    const token = await seed.accessToken(data.admin_email);
    const projectResponse = await request.get(
      `${API_BASE}/api/v1/projects/${lidar.lidar_project_id}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(projectResponse.ok()).toBe(true);
    const { name: lidarProjectName } = (await projectResponse.json()) as { name: string };

    const consoleErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => consoleErrors.push(`pageerror: ${error.message}`));

    await page.goto("/projects");
    const row = page.locator("tr", { hasText: lidarProjectName });
    await expect(row).toBeVisible({ timeout: 10_000 });
    await row.getByTitle("更多操作").click();
    await page.getByRole("menuitem", { name: "导出标注数据" }).click();

    const dialog = page.getByRole("dialog", { name: "导出标注数据" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: /^nuScenes/ })).toBeEnabled();

    const kitti = dialog.getByRole("button", { name: /^KITTI 3D/ });
    const missingCameraResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          `/api/v1/projects/${lidar.lidar_project_id}/exports/lidar:preflight`,
    );
    await kitti.click();
    expect((await missingCameraResponse).status()).toBe(200);
    await expect(dialog.getByTestId("lidar-export-preflight")).toContainText("预检阻止");
    await expect(dialog.getByText("kitti_camera_required")).toBeVisible();
    await expect(dialog.getByRole("button", { name: "开始导出" })).toBeDisabled();

    const readyResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          `/api/v1/projects/${lidar.lidar_project_id}/exports/lidar:preflight` &&
        response.request().postData()?.includes("camera_front") === true,
    );
    await dialog.getByLabel("KITTI 投影相机").selectOption("camera_front");
    const response = await readyResponse;
    expect(response.status()).toBe(200);
    expect(await response.json()).toMatchObject({
      ready: true,
      selected_camera_role: "camera_front",
      checked_tasks: 2,
      issue_count: 0,
    });
    await expect(dialog.getByTestId("lidar-export-preflight")).toContainText("预检通过 · 2 帧");
    await expect(dialog.getByRole("button", { name: "开始导出" })).toBeEnabled();

    const fatal = consoleErrors.filter(
      (error) => !/favicon|net::ERR_|Download the React DevTools/i.test(error),
    );
    expect(fatal, `console errors:\n${fatal.join("\n")}`).toEqual([]);
  });
});
