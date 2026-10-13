import { layoutCommand } from "../helpers/workbench-layout";
import { writeFile } from "node:fs/promises";
/**
 * v0.16.x · 点云工作台冒烟基线(P1)——拆 3D 整簇前的 Playwright 守护网地基。
 *
 * 缘由见 docs/plans/archive/2026-06-17-v0.16.x-pointcloud-e2e-baseline-for-3d-split.md:
 * usePsrEditor/usePointMask/usePointCloudSelection 因共享 scene+form+合并键盘 handler
 * 职责纠缠不可干净切分,jsdom 无 WebGL 单测不了 → 唯一能在拆分前后证明行为等价的是
 * Playwright 端到端。本 spec 是该网的"地基 + go/no-go 闸":验证 headless Chromium(经
 * ANGLE/SwiftShader 软渲染,见 playwright.config.ts 的 pointcloud project)能真正
 * 加载并渲染点云、无 console error。本闸不绿,后续交互断言(P2)无从谈起。
 *
 * 注:本 spec 由 `pointcloud` project 跑(带 WebGL 软渲染 launch args);默认 chromium
 * project 已 testIgnore 排除,避免无 GPU 跑挂。
 */
import { test, expect } from "../fixtures/seed";

test("point-mask visibility removes and restores painted points without changing indices", async ({
  page,
  request,
  seed,
}) => {
  test.setTimeout(120_000);
  const data = await seed.owned();
  const lidar = await seed.seedLidar();
  const taskId = lidar.lidar_task_ids[0];
  const pointIndices = Array.from({ length: lidar.lidar_point_count }, (_, index) => index);
  const annotation = await seed.createTaskAnnotation(taskId, data.admin_email, {
    annotation_type: "point_mask_3d",
    tool_unit_id: "point_mask_3d",
    class_name: "ground",
    geometry: {
      type: "point_mask_3d",
      point_indices: pointIndices,
      decimate_stride: 1,
      source_point_count: lidar.lidar_point_count,
    },
  });
  try {
    await seed.injectToken(page, data.admin_email);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${taskId}`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("pointcloud-renderer-backend")).toHaveAttribute(
      "data-backend",
      /webgl/,
    );
    const row = page.getByTestId(`box-list-item-${annotation.id}`);
    await expect(row).toBeVisible();
    await row.click({ position: { x: 20, y: 16 } });
    await expect(row).toHaveClass(/!border-brand/);
    const canvas = page.locator("[data-workbench-render-surface] > canvas");
    await expect(canvas).toHaveCount(1);
    const renderer = await canvas.evaluate((node) => {
      const gl = (node as HTMLCanvasElement).getContext("webgl2");
      const debug = gl?.getExtension("WEBGL_debug_renderer_info");
      return {
        renderer: gl?.getParameter(debug?.UNMASKED_RENDERER_WEBGL ?? gl.RENDERER),
        dpr: devicePixelRatio,
      };
    });
    const viewport = await page.getByTestId("pc-viewport").boundingBox();
    if (!viewport) throw new Error("point-cloud viewport is missing");
    // Compare only scene pixels, excluding changing list/count/selection controls.
    const clip = {
      x: viewport.x + viewport.width * 0.15,
      y: viewport.y + viewport.height * 0.2,
      width: viewport.width * 0.65,
      height: viewport.height * 0.55,
    };
    const capture = (path?: string) => page.screenshot({ clip, path });
    await page.mouse.move(5, 5);
    const before = await capture(test.info().outputPath("pointmask-visible.png"));
    const difference = async (other: Buffer) =>
      page.evaluate(
        async ([left, right]) => {
          const decode = async (url: string) => {
            const image = await createImageBitmap(await (await fetch(url)).blob());
            const surface = new OffscreenCanvas(image.width, image.height);
            const context = surface.getContext("2d")!;
            context.drawImage(image, 0, 0);
            const pixels = context.getImageData(0, 0, image.width, image.height).data;
            image.close();
            return pixels;
          };
          const [a, b] = await Promise.all([decode(left), decode(right)]);
          if (a.length !== b.length) return 1;
          let changed = 0;
          for (let index = 0; index < a.length; index += 4) {
            if (
              Math.abs(a[index] - b[index]) +
                Math.abs(a[index + 1] - b[index + 1]) +
                Math.abs(a[index + 2] - b[index + 2]) >
              30
            )
              changed += 1;
          }
          return changed / (a.length / 4);
        },
        [
          "data:image/png;base64," + before.toString("base64"),
          "data:image/png;base64," + other.toString("base64"),
        ],
      );
    await row.getByRole("button", { name: "更多操作", exact: true }).hover();
    const hide = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        response.url().endsWith(`/annotations/${annotation.id}`),
    );
    await row.getByRole("button", { name: "隐藏", exact: true }).click();
    expect((await hide).ok()).toBe(true);
    await expect(row.getByRole("button", { name: "显示", exact: true })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await page.mouse.move(5, 5);
    await expect.poll(async () => difference(await capture())).toBeGreaterThan(0.0001);
    await capture(test.info().outputPath("pointmask-hidden.png"));
    await row.getByRole("button", { name: "更多操作", exact: true }).hover();
    const show = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        response.url().endsWith(`/annotations/${annotation.id}`),
    );
    await row.getByRole("button", { name: "显示", exact: true }).click();
    expect((await show).ok()).toBe(true);
    await expect(row.getByRole("button", { name: "隐藏", exact: true })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await page.mouse.move(5, 5);
    await expect.poll(async () => difference(await capture())).toBeLessThan(0.005);
    await capture(test.info().outputPath("pointmask-restored.png"));
    const token = await seed.accessToken(data.admin_email);
    const response = await request.get(
      (process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010") +
        `/api/v1/tasks/${taskId}/annotations`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(response.ok()).toBe(true);
    const restored = (await response.json()).find(
      (item: { id: string }) => item.id === annotation.id,
    );
    expect(restored.is_hidden).toBe(false);
    expect(restored.geometry.point_indices).toEqual(pointIndices);
    const rendererInfo = JSON.stringify({
      backend: await page.getByTestId("pointcloud-renderer-backend").getAttribute("data-backend"),
      mode: "behavioral validation",
      sceneClip: clip,
      ...renderer,
      browser: page.context().browser()?.version(),
      viewport: { width: 1440, height: 900 },
      pointCount: lidar.lidar_point_count,
    });
    const rendererPath = test.info().outputPath("renderer.json");
    await writeFile(rendererPath, rendererInfo);
    await test.info().attach("renderer", { path: rendererPath, contentType: "application/json" });
  } finally {
    await seed.deleteTaskAnnotation(taskId, annotation.id, data.admin_email);
  }
});

test.describe("workbench pointcloud smoke (WebGL go/no-go)", () => {
  test("headless 加载并渲染 nuScenes 规模点云,四视图共享 renderer 且空闲停止提交", async ({
    page,
    seed,
  }) => {
    const data = await seed.owned();
    const lidar = await seed.seedLidar();
    // super_admin 可见全部项目/任务,免去 batch 可见性/分派的额外铺设。
    await seed.injectToken(page, data.admin_email);

    // 收集 console error / pageerror;WebGL 跑不起来时 Three.js 会在此爆。
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const sourceUrl = msg.location().url;
      consoleErrors.push(sourceUrl ? `${msg.text()} (${sourceUrl})` : msg.text());
    });
    page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err.message}`));

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);

    // 点云加载失败时状态栏出 "点云加载失败: ..."(WebGL/解码挂的早期信号)。
    await expect(page.getByText(/点云加载失败/)).toHaveCount(0);

    // 成功信号:loadPcd 完成 → stats 出数 → 状态栏渲染 seed 返回的真实点数。这是 PointCloudScene
    // 在 headless 真跑通 WebGL 的证据(渲染失败则 stats 永不 set,此断言超时)。
    const stats = page.getByTestId("pointcloud-stats");
    await expect(stats).toBeVisible({ timeout: 20_000 });
    await expect(stats).toContainText("点");
    expect(["nuscenes_mini", "nuscenes_profile"]).toContain(lidar.lidar_fixture_source);
    expect(lidar.lidar_point_count).toBeGreaterThan(30_000);
    await expect(stats).toContainText(lidar.lidar_point_count.toLocaleString());

    // 整个工作区只挂一个共享 Three renderer canvas；三视图自己的 2D overlay 不计入。
    const viewport = page.getByTestId("pc-viewport");
    await expect(page.locator("[data-workbench-render-surface] > canvas")).toHaveCount(1);
    await expect(viewport).toHaveAttribute("data-pointcloud-renderer-count", "1");

    const card = page.locator('[data-testid^="box-list-item-"]').first();
    await card.click({ position: { x: 12, y: 16 } });
    await layoutCommand(page, "框体精修");
    await expect(page.getByTestId("tri-view-renderer-panel")).toBeVisible();
    await expect(
      page.getByTestId("tri-view-renderer-panel").locator(":scope > canvas"),
    ).toHaveCount(0);
    await expect
      .poll(async () =>
        Number((await viewport.getAttribute("data-pointcloud-tri-pass-count")) ?? 0),
      )
      .toBeGreaterThan(0);

    // 先给 OrbitControls 阻尼一段稳定时间，再验证主/三视图都不再产生 renderer.render 提交。
    await page.waitForTimeout(300);
    await expect
      .poll(async () => {
        const before = Number(await viewport.getAttribute("data-pointcloud-submit-count"));
        await page.waitForTimeout(300);
        const after = Number(await viewport.getAttribute("data-pointcloud-submit-count"));
        return after - before;
      })
      .toBe(0);

    const rendererCanvas = page.locator("[data-workbench-render-surface] > canvas");
    const originalCanvas = await rendererCanvas.elementHandle();
    const originalContext = await rendererCanvas.evaluateHandle((canvas) =>
      (canvas as HTMLCanvasElement).getContext("webgl2"),
    );
    for (const preset of ["专注画布布局", "审核协作布局", "标准标注布局"]) {
      await layoutCommand(page, preset);
      expect(
        await rendererCanvas.evaluate((node, original) => node === original, originalCanvas),
      ).toBe(true);
      expect(
        await rendererCanvas.evaluate(
          (node, original) => (node as HTMLCanvasElement).getContext("webgl2") === original,
          originalContext,
        ),
      ).toBe(true);
      await expect(viewport).toHaveAttribute("data-pointcloud-renderer-count", "1");
    }
    await page.setViewportSize({ width: 1024, height: 800 });
    await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute(
      "data-compact",
      "true",
    );
    await page.setViewportSize({ width: 1280, height: 800 });
    await expect(page.locator("[data-workbench-workspace]")).toHaveAttribute(
      "data-compact",
      "false",
    );
    expect(
      await rendererCanvas.evaluate((node, original) => node === original, originalCanvas),
    ).toBe(true);
    expect(
      await originalContext.evaluate((context) => context !== null && !context.isContextLost()),
    ).toBe(true);

    // 过滤掉与本验证无关的已知噪声(如第三方资源 404 / favicon),只对真错误失败。
    const fatal = consoleErrors.filter(
      (e) => !/favicon|net::ERR_|Download the React DevTools/i.test(e),
    );
    expect(fatal, `console errors:\n${fatal.join("\n")}`).toEqual([]);
  });
});
