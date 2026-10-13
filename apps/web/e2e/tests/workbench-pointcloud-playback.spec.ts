import type { Page, Request } from "@playwright/test";
import { expect, test, type SeedAPI } from "../fixtures/seed";

const timeline = (page: Page) => page.getByTestId("three-d-scene-timeline");
const player = (page: Page) => page.locator("[data-scene-playback]");
const play = (page: Page) => page.getByTestId("scene-timeline-play");

async function prepare(page: Page, seed: SeedAPI) {
  const owned = await seed.owned();
  const data = await seed.seedLidar();
  await seed.injectToken(page, owned.admin_email);
  if (process.env.PLAYWRIGHT_POINTCLOUD_WEBGPU === "1") {
    await page.addInitScript(() =>
      localStorage.setItem("aap.experiment.pointCloudWebGpuRenderer", "1"),
    );
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  return data;
}

async function open(page: Page, projectId: string, taskId: string) {
  await page.goto(`/projects/${projectId}/annotate?task=${taskId}`);
  await expect(player(page)).toHaveAttribute("data-scene-frame-state", "ready", {
    timeout: 30_000,
  });
  await expect(play(page)).toBeEnabled();
}

function annotationWrite(request: Request) {
  return (
    request.method() !== "GET" &&
    /\/(annotations|propagate|interpolate|submit)(?:[/?]|$)/.test(new URL(request.url()).pathname)
  );
}

test("连续浏览等待真实点云，暂停恢复当前帧锁并保留 renderer", async ({ page, seed }) => {
  const data = await prepare(page, seed);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await open(page, data.lidar_project_id, data.lidar_task_ids[0]);
  const canvas = page.locator("[data-workbench-render-surface] > canvas");
  const original = await canvas.elementHandle();
  const viewport = page.getByTestId("pc-viewport");
  if (process.env.PLAYWRIGHT_POINTCLOUD_WEBGPU === "1") {
    await expect(page.getByTestId("pointcloud-renderer-backend")).toHaveAttribute(
      "data-backend",
      "webgpu",
    );
  }
  const writes: string[] = [];
  const locks: string[] = [];
  let observing = false;
  page.on("request", (request) => {
    if (!observing) return;
    if (annotationWrite(request)) writes.push(request.url());
    if (request.method() === "POST" && /\/lock(?:[/?]|$)/.test(request.url()))
      locks.push(request.url());
  });

  for (const rate of [1, 2, 4]) {
    // The last frame starts a new playback session from the first accessible frame.
    await page.getByLabel("Scene 浏览速率", { exact: true }).selectOption(String(rate));
    await expect(play(page)).toBeEnabled();
    observing = true;
    const started = Date.now();
    await play(page).click();
    await expect(player(page)).toHaveAttribute("data-scene-playback", "playing");
    await expect(page).toHaveURL(new RegExp(`task=${data.lidar_task_ids[1]}`));
    await expect(player(page)).toHaveAttribute("data-scene-playback", "paused", {
      timeout: 20_000,
    });
    observing = false;
    expect(Date.now() - started).toBeGreaterThanOrEqual(1000 / rate);
    await expect(player(page)).toHaveAttribute("data-scene-frame-state", "ready");
    expect(await canvas.evaluate((node, old) => node === old, original)).toBe(true);
    await expect(viewport).toHaveAttribute("data-pointcloud-renderer-count", "1");
  }
  expect(writes).toEqual([]);
  // Each completed session reacquires only its final frame, never an intermediate preview frame.
  expect(locks.every((url) => url.includes(data.lidar_task_ids[1]))).toBe(true);
  await expect
    .poll(async () => {
      const before = Number(await viewport.getAttribute("data-pointcloud-submit-count"));
      await page.waitForTimeout(300);
      return Number(await viewport.getAttribute("data-pointcloud-submit-count")) - before;
    })
    .toBe(0);
  expect(errors).toEqual([]);
});

test("慢点云加载中暂停，迟到资源只完成一次导航", async ({ page, seed }) => {
  const data = await prepare(page, seed);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  page.on("close", release);
  await page.route("**/*.pcd*", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("-1.pcd")) await gate;
    await route.continue().catch(() => undefined);
  });
  await open(page, data.lidar_project_id, data.lidar_task_ids[0]);
  await play(page).click();
  await expect(page).toHaveURL(new RegExp(`task=${data.lidar_task_ids[1]}`));
  await expect(player(page)).toHaveAttribute("data-scene-frame-state", "loading");
  await expect(play(page)).toBeEnabled();
  await play(page).click();
  await expect(player(page)).toHaveAttribute("data-scene-playback", "paused");
  release();
  await expect(player(page)).toHaveAttribute("data-scene-frame-state", "ready", {
    timeout: 20_000,
  });
  await page.waitForTimeout(1200);
  await expect(page).toHaveURL(new RegExp(`task=${data.lidar_task_ids[1]}`));
  await expect(player(page)).toHaveAttribute("data-scene-playback", "paused");
});

test("未完成 PSR 与在途保存阻止播放，编辑快捷键先暂停", async ({ page, seed }) => {
  const data = await prepare(page, seed);
  await open(page, data.lidar_project_id, data.lidar_task_ids[0]);
  await page
    .locator('[data-testid^="box-list-item-"]')
    .first()
    .click({ position: { x: 12, y: 16 } });
  await page.getByLabel("展开详情").click();
  const field = page.getByLabel("cx", { exact: true });
  await expect(field).toBeEnabled();
  await field.fill("");
  await expect(play(page)).toBeDisabled();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  page.on("close", release);
  let saving = false;
  await page.route("**/api/v1/tasks/*/annotations/*", async (route) => {
    if (route.request().method() === "PATCH") {
      saving = true;
      await gate;
    }
    await route.continue().catch(() => undefined);
  });
  await field.fill("2");
  await expect(play(page)).toBeDisabled();
  await expect.poll(() => saving).toBe(true);
  await expect(play(page)).toBeDisabled();
  release();
  await expect(play(page)).toBeEnabled();
  await play(page).click();
  await expect(player(page)).toHaveAttribute("data-scene-playback", "playing");
  await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
  await page.keyboard.press("Delete");
  await expect(player(page)).toHaveAttribute("data-scene-playback", "paused");
  await expect(page.locator('[data-testid^="box-list-item-"]')).toHaveCount(1);
  await expect(field).toBeEnabled();
});

test("容器宽度响应、主题与展开偏好不重建点云画布", async ({ page, seed }, testInfo) => {
  const data = await prepare(page, seed);
  await open(page, data.lidar_project_id, data.lidar_task_ids[0]);
  const canvas = page.locator("[data-workbench-render-surface] > canvas");
  const original = await canvas.elementHandle();
  await expect(page.getByTestId("scene-timeline-toggle")).toHaveAttribute("aria-expanded", "false");
  for (const width of [320, 480, 744]) {
    await timeline(page).evaluate((element, value) => {
      (element as HTMLElement).style.width = `${value}px`;
    }, width);
    await expect(play(page)).toBeVisible();
    const overflows = await timeline(page).evaluate(
      (element) => element.scrollWidth > element.clientWidth + 1,
    );
    expect(overflows, `timeline must fit ${width}px container`).toBe(false);
  }
  await timeline(page).evaluate((element) =>
    (element as HTMLElement).style.removeProperty("width"),
  );
  await page.getByTestId("scene-timeline-toggle").click();
  await expect(page.getByTestId("scene-timeline-virtual-canvas")).toBeVisible();
  for (const theme of ["light", "dark"]) {
    await page.evaluate(
      (value) => document.documentElement.setAttribute("data-theme", value),
      theme,
    );
    await page.waitForTimeout(300); // Let existing button color transitions settle.
    await page.screenshot({ path: testInfo.outputPath(`scene-timeline-${theme}.png`) });
  }
  for (const width of [1440, 1728, 1920]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(timeline(page)).toBeVisible();
    expect(await canvas.evaluate((node, old) => node === old, original)).toBe(true);
  }
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("heading", { name: "请切换到桌面端" })).toBeVisible();
});

test("点云错误立即暂停，重试成功仍保持暂停", async ({ page, seed }) => {
  const data = await prepare(page, seed);
  let failTarget = true;
  await page.route("**/*.pcd*", async (route) => {
    if (failTarget && new URL(route.request().url()).pathname.endsWith("-1.pcd")) {
      await route.fulfill({ status: 503, body: "temporary point cloud failure" });
    } else await route.continue();
  });
  await open(page, data.lidar_project_id, data.lidar_task_ids[0]);
  await play(page).click();
  await expect(page).toHaveURL(new RegExp(`task=${data.lidar_task_ids[1]}`));
  await expect(player(page)).toHaveAttribute("data-scene-frame-state", "error", {
    timeout: 20_000,
  });
  await expect(player(page)).toHaveAttribute("data-scene-playback", "paused");
  failTarget = false;
  await timeline(page).getByRole("button", { name: "重试", exact: true }).click();
  await expect(player(page)).toHaveAttribute("data-scene-frame-state", "ready", {
    timeout: 20_000,
  });
  await page.waitForTimeout(1000);
  await expect(player(page)).toHaveAttribute("data-scene-playback", "paused");
});
