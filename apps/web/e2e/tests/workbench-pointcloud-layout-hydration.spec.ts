import { openLayoutSettings } from "../helpers/workbench-layout";
import { createWorkspacePreset } from "../../src/pages/Workbench/layout/workbenchLayoutPresets";
import { expect, test } from "../fixtures/seed";

const DESKTOP = { width: 1440, height: 900 };

test("3D 本地树等待首次权威回灌，复用原生画布与 WebGL context，后续 refetch 不覆盖布局", async ({
  page,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const lidar = await seed.seedLidar();
  await seed.injectToken(page, data.admin_email);
  await page.setViewportSize(DESKTOP);

  const local = { schemaVersion: 5, snapshot: createWorkspacePreset("standard", DESKTOP) };
  const remote = { schemaVersion: 5, snapshot: createWorkspacePreset("review", DESKTOP) };
  const later = { schemaVersion: 5, snapshot: createWorkspacePreset("focus", DESKTOP) };
  const storageKey = await page.evaluate((envelope) => {
    const auth = JSON.parse(localStorage.getItem("auth-storage")!);
    const key = `workbench.${auth.state.user.id}.workspace.annotate:3d`;
    localStorage.setItem(key, JSON.stringify(envelope));
    return key;
  }, local);

  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  let preferenceGets = 0;
  const workspaceWrites: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/api/v1/auth/me/preferences", async (route) => {
    const request = route.request();
    if (request.method() === "PATCH") {
      if (request.postDataJSON()?.workbench?.layout?.workspace)
        workspaceWrites.push(request.postData()!);
      await route.continue();
      return;
    }
    if (request.method() !== "GET") {
      await route.continue();
      return;
    }
    const ordinal = ++preferenceGets;
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    const preferences = await response.json();
    preferences.workbench.layout.workspace = {
      engine: "dockview@8",
      contexts: { "annotate:3d": ordinal === 1 ? remote : later },
    };
    if (ordinal === 1) await firstGate;
    await route.fulfill({ response, json: preferences });
  });

  try {
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${lidar.lidar_task_ids[0]}`);
    const stats = page.getByTestId("pointcloud-stats");
    await expect(stats).toBeVisible({ timeout: 20_000 });
    await expect(stats).toContainText(lidar.lidar_point_count.toLocaleString());
    await expect(page.getByText(/点云加载失败/)).toHaveCount(0);
    const viewport = page.getByTestId("pc-viewport");
    const canvas = page.locator("[data-workbench-render-surface] > canvas");
    const wrapper = page.locator('[data-workbench-panel="canvas"]');
    const queue = page.locator('[data-workbench-panel="task-queue"]');
    const palette = page.locator('[data-workbench-panel="class-palette"]');
    await expect(canvas).toHaveCount(1);
    await expect(canvas).toBeVisible();
    await expect(queue).toBeVisible();
    await expect(palette).toBeVisible();
    expect(preferenceGets).toBe(1);
    expect(
      await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), storageKey),
    ).toEqual(local);

    const originalWrapper = await wrapper.elementHandle();
    const originalViewport = await viewport.elementHandle();
    const originalCanvas = await canvas.elementHandle();
    const originalContext = await canvas.evaluateHandle((node) =>
      (node as HTMLCanvasElement).getContext("webgl2"),
    );
    const sameRenderer = async () => {
      await expect(canvas).toHaveCount(1);
      await expect(viewport).toHaveAttribute("data-pointcloud-renderer-count", "1");
      expect(await wrapper.evaluate((node, original) => node === original, originalWrapper)).toBe(
        true,
      );
      expect(await viewport.evaluate((node, original) => node === original, originalViewport)).toBe(
        true,
      );
      expect(await canvas.evaluate((node, original) => node === original, originalCanvas)).toBe(
        true,
      );
      expect(
        await canvas.evaluate(
          (node, original) => (node as HTMLCanvasElement).getContext("webgl2") === original,
          originalContext,
        ),
      ).toBe(true);
      expect(
        await originalContext.evaluate((context) => context !== null && !context.isContextLost()),
      ).toBe(true);
    };
    await sameRenderer();

    await page.getByRole("button", { name: "布局", exact: true }).click();
    for (const name of ["标准标注布局", "专注画布布局"]) {
      await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
    }
    await page.keyboard.press("Escape");
    const settings = await openLayoutSettings(page);
    await expect(settings.getByRole("status")).toHaveText("正在加载设置…");
    await expect(settings.getByRole("region", { name: "工作台布局" })).toHaveCount(0);
    await settings.getByRole("button", { name: "关闭设置", exact: true }).click();
    const queueBounds = await queue.boundingBox();
    await page
      .getByRole("tab", { name: "任务队列", exact: true })
      .dragTo(page.getByRole("tab", { name: "标注详情", exact: true }));
    expect(await queue.boundingBox()).toEqual(queueBounds);
    // Give the 300 ms debounce an opportunity to expose any wrongly accepted mutation.
    await page.waitForTimeout(450);
    expect(workspaceWrites).toEqual([]);
    expect(
      await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), storageKey),
    ).toEqual(local);
    await sameRenderer();

    const initialResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname === "/api/v1/auth/me/preferences",
    );
    releaseFirst();
    await initialResponse;
    await expect(queue).toBeHidden();
    await expect(palette).toBeHidden();
    await expect
      .poll(() => page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), storageKey))
      .toEqual(remote);
    await sameRenderer();
    await page.getByRole("button", { name: "布局", exact: true }).click();
    await expect(page.getByRole("button", { name: "标准标注布局", exact: true })).toBeEnabled();
    await page.keyboard.press("Escape");
    const hydratedBounds = await wrapper.boundingBox();

    // Expire the shared query without advancing animation/lock timers, then use
    // a real browser reconnect to request a different tree through React Query.
    await page.clock.setFixedTime(Date.now() + 6 * 60_000);
    const refetched = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname === "/api/v1/auth/me/preferences",
    );
    await page.context().setOffline(true);
    await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);
    await page.context().setOffline(false);
    const refetchResponse = await refetched;
    expect(
      (await refetchResponse.json()).workbench.layout.workspace.contexts["annotate:3d"],
    ).toEqual(later);
    expect(preferenceGets).toBeGreaterThanOrEqual(2);
    await page.waitForTimeout(450);
    await expect(queue).toBeHidden();
    await expect(palette).toBeHidden();
    expect(await wrapper.boundingBox()).toEqual(hydratedBounds);
    expect(
      await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), storageKey),
    ).toEqual(remote);
    await sameRenderer();
    expect(workspaceWrites).toEqual([]);
    expect(pageErrors).toEqual([]);
  } finally {
    releaseFirst();
    await page.context().setOffline(false);
  }
});
