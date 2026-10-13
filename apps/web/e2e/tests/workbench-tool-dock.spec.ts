import { canvasBottomDivider, panelCommand } from "../fixtures/workbench-panel-actions";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, type SeedAPI } from "../fixtures/seed";
import { launchNativeBrowserZoom } from "../fixtures/native-browser-zoom";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

async function prepare(request: APIRequestContext, seed: SeedAPI) {
  const data = await seed.owned();
  const { task_id: videoTaskId } = await seed.videoTask(data.project_id);
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const projectResponse = await request.get(`${API_BASE}/api/v1/projects/${data.project_id}`, {
    headers,
  });
  expect(projectResponse.ok()).toBe(true);
  const project = await projectResponse.json();
  const toolBindings = { ...project.tool_bindings };
  for (const unit of ["region", "rotated_bbox", "polyline", "keypoint"])
    toolBindings[unit] = { ...toolBindings.bbox, enabled: true, attribute_schema: {} };
  const updated = await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
    headers,
    data: { tool_bindings: toolBindings },
  });
  expect(updated.ok(), await updated.text()).toBe(true);
  for (const taskId of [data.task_ids[0], videoTaskId])
    await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  return { ...data, videoTaskId, headers };
}

async function standardLayout(page: Page) {
  await page.getByRole("button", { name: "布局", exact: true }).click();
  await page.getByRole("button", { name: "标准标注布局", exact: true }).click();
}

async function accessibleTools(page: Page) {
  const dock = page.getByTestId("tool-dock");
  const ids = await dock
    .locator("[data-tool-dock-entry]")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-tool-dock-entry")).filter((id) => id !== "more"),
    );
  if (await page.getByTestId("tool-dock-more").isVisible()) {
    await page.getByTestId("tool-dock-more").click();
    const menu = page.getByTestId("tool-dock-menu");
    await expect(menu).toBeVisible();
    await expect
      .poll(() => menu.evaluate((el) => el.contains(el.ownerDocument.activeElement)))
      .toBe(true);
    ids.push(
      ...(await menu
        .getByRole("menuitemradio")
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-testid")!.replace("tool-overflow-item-", "")),
        )),
    );
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("tool-dock-more")).toBeFocused();
  }
  expect(new Set(ids).size).toBe(ids.length);
  return ids.sort();
}

test("图片与视频在三种高度和原生页面缩放下保留全部工具及当前工具", async ({
  request,
  seed,
  baseURL,
}, testInfo) => {
  test.setTimeout(150_000);
  const data = await prepare(request, seed);
  const measurements: unknown[] = [];
  let expectedImage: Array<string | null> | undefined;
  const expectedVideo: Partial<Record<"frame" | "track", Array<string | null>>> = {};
  for (const height of [768, 900, 1080]) {
    // Headless Chromium reserves 87 physical pixels for browser chrome at 100%.
    // Assert the real CSS viewport below so changes to that inset fail visibly.
    const browser = await launchNativeBrowserZoom({
      baseURL,
      windowSize: { width: 1920, height: height + 87 },
    });
    try {
      const { page } = browser;
      page.setDefaultTimeout(10_000);
      await seed.injectToken(page, data.annotator_email, baseURL);
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      for (const video of [false, true]) {
        await page.goto(
          `/projects/${data.project_id}/annotate?task=${video ? data.videoTaskId : data.task_ids[0]}`,
        );
        await standardLayout(page);
        await browser.setZoom(1);
        const stage = page.getByTestId(video ? "video-konva-stage" : "workbench-stage");
        await expect(stage).toBeVisible();
        const originalStage = await stage.elementHandle();
        const tail = video ? "mask-track" : "mask";
        if (video) await page.getByRole("button", { name: "轨迹范围", exact: true }).click();
        const button = page.getByTestId(`${video ? "video-tool" : "tool"}-btn-${tail}`);
        if (await button.isVisible()) await button.click();
        else {
          await page.getByTestId("tool-dock-more").click();
          await page.getByTestId(`tool-overflow-item-${tail}`).click();
        }
        for (const zoom of [1, 1.25, 1.5]) {
          await browser.setZoom(zoom);
          await expect
            .poll(() => page.evaluate(() => window.devicePixelRatio))
            .toBeCloseTo(zoom, 2);
          const metric = await page.evaluate(() => ({
            width: innerWidth,
            height: innerHeight,
            dpr: devicePixelRatio,
            cssZoom: getComputedStyle(document.documentElement).zoom,
          }));
          expect(metric.height).toBeCloseTo(Math.floor(height / zoom), -1);
          expect(metric.cssZoom).toBe("1");
          expect(await browser.getZoom()).toBeCloseTo(zoom, 2);
          measurements.push({ video, heightAt100Percent: height, zoom, ...metric });
          await expect(button).toHaveAttribute("aria-pressed", "true");
          await expect(button).toBeInViewport();
          const dock = page.getByTestId("tool-dock");
          await expect
            .poll(() => dock.evaluate((el) => el.scrollHeight <= el.clientHeight + 1))
            .toBe(true);
          if (video) {
            for (const scope of ["frame", "track"] as const) {
              await page
                .getByRole("button", {
                  name: scope === "frame" ? "单帧范围" : "轨迹范围",
                  exact: true,
                })
                .click();
              await expect(page.getByTestId("video-tool-scope")).toHaveAttribute(
                "data-scope",
                scope,
              );
              const active = page.getByTestId(
                `video-tool-btn-${scope === "frame" ? "mask" : "mask-track"}`,
              );
              if (await active.isVisible()) await active.click();
              else {
                await page.getByTestId("tool-dock-more").click();
                await page
                  .getByTestId(`tool-overflow-item-${scope === "frame" ? "mask" : "mask-track"}`)
                  .click();
              }
              await expect(active).toHaveAttribute("aria-pressed", "true");
              await expect(active).toBeInViewport();
              const ids = await accessibleTools(page);
              expectedVideo[scope] ??= ids;
              expect(ids).toEqual(expectedVideo[scope]);
              await expect
                .poll(() => dock.evaluate((el) => el.scrollHeight <= el.clientHeight + 1))
                .toBe(true);
            }
          } else {
            const ids = await accessibleTools(page);
            expectedImage ??= ids;
            expect(ids).toEqual(expectedImage);
          }
          expect(await stage.evaluate((node, original) => node === original, originalStage)).toBe(
            true,
          );
        }
        // Closing the menu restores focus to More; normal tool hotkeys resume there.
        if (await page.getByTestId("tool-dock-more").isVisible()) {
          await page.getByTestId("tool-dock-more").press("v");
          await expect(
            page.getByTestId(`${video ? "video-tool" : "tool"}-btn-select`),
          ).toHaveAttribute("aria-pressed", "true");
        }
      }
      expect(pageErrors).toEqual([]);
    } finally {
      await browser.close();
    }
  }
  expect(expectedImage!.length).toBeGreaterThanOrEqual(12);
  expect(expectedVideo.frame).not.toContain("mask-track");
  expect(expectedVideo.track).not.toContain("mask");
  expect(new Set([...expectedVideo.frame!, ...expectedVideo.track!]).size).toBe(15);
  await testInfo.attach("native-browser-zoom", {
    body: JSON.stringify(measurements, null, 2),
    contentType: "application/json",
  });
});

for (const video of [false, true]) {
  test(`${video ? "视频" : "图片"}更多菜单、面板缩放保留几何草稿且不穿透点击`, async ({
    page,
    request,
    seed,
  }) => {
    test.setTimeout(90_000);
    page.setDefaultTimeout(15_000);
    await page.setViewportSize({ width: 1440, height: 768 });
    const data = await prepare(request, seed);
    const taskId = video ? data.videoTaskId : data.task_ids[0];
    await seed.injectToken(page, data.annotator_email);
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await standardLayout(page);
    const stage = page.getByTestId(video ? "video-konva-stage" : "workbench-stage");
    await expect(stage.locator(".konvajs-content > canvas").first()).toBeVisible();
    const originalStage = await stage.elementHandle();
    const originalCanvas = await stage.locator(".konvajs-content > canvas").first().elementHandle();
    await page.getByTestId(`${video ? "video-tool" : "tool"}-btn-polygon`).click();
    const bounds = await stage.boundingBox();
    if (!bounds) throw new Error("Stage has no bounds");
    for (const [x, y] of [
      [0.3, 0.3],
      [0.6, 0.3],
      [0.5, 0.55],
    ]) {
      await page.mouse.click(bounds.x + bounds.width * x, bounds.y + bounds.height * y);
      // Konva detects consecutive canvas clicks as double-clicks, even at different points.
      await page.waitForTimeout(450);
    }
    const writes: string[] = [];
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname === `/api/v1/tasks/${taskId}/annotations`
      )
        writes.push(request.url());
    });
    const dock = page.getByTestId("tool-dock");
    if (video) {
      const beforeTimeline = (await dock.boundingBox())!;
      const ids = await accessibleTools(page);
      await page.getByRole("button", { name: "展开时间轴详情" }).click();
      await expect(page.getByTestId("video-timeline-details")).toBeVisible();
      await page.evaluate(() =>
        Promise.allSettled(
          document
            .getAnimations()
            .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
            .map((animation) => animation.finished),
        ),
      );
      expect((await dock.boundingBox())!.height).toBeCloseTo(beforeTimeline.height);
      expect((await dock.boundingBox())!.y).toBeCloseTo(beforeTimeline.y);
      expect(await accessibleTools(page)).toEqual(ids);
      await page.getByRole("button", { name: "收起时间轴详情" }).click();
      await page.evaluate(() =>
        Promise.allSettled(
          document
            .getAnimations()
            .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
            .map((animation) => animation.finished),
        ),
      );
      expect((await dock.boundingBox())!.y).toBeCloseTo(beforeTimeline.y);
    }
    await panelCommand(page, "讨论", "停靠到底部");
    await expect(page.getByText("布局恢复失败", { exact: false })).toHaveCount(0);
    expect(
      await page
        .locator("[data-workbench-workspace], .dv-split-view-container")
        .evaluateAll(
          (nodes) => nodes.filter((node) => node.scrollLeft !== 0 || node.scrollTop !== 0).length,
        ),
    ).toBe(0);
    const divider = await canvasBottomDivider(page);
    const beforeDrag = (await dock.boundingBox())!.height;
    await page.mouse.move(divider.x, divider.y);
    await page.mouse.down();
    await page.mouse.move(divider.x, divider.y - (video ? 150 : 65), { steps: 8 });
    await page.mouse.up();
    // Native docking splits the canvas in half; its minimum height can leave
    // less than 40px to shrink. Verify resizing and the resulting overflow below.
    await expect.poll(async () => (await dock.boundingBox())!.height).toBeLessThan(beforeDrag);
    const more = page.getByTestId("tool-dock-more");
    await expect(more).toBeVisible();
    await more.press("ArrowDown");
    const menu = page.getByTestId("tool-dock-menu");
    await expect(menu).toBeVisible();
    await page.keyboard.press("End");
    await page.keyboard.press("Home");
    await page.keyboard.press("b");
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
    expect(writes).toEqual([]);
    await expect(page.getByTestId(`${video ? "video-tool" : "tool"}-btn-polygon`)).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await more.click();
    await expect(menu.getByTestId("tool-overflow-item-smart-point")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await expect(menu.getByTestId("tool-overflow-item-smart-point")).toHaveAccessibleDescription(
      "当前后端不支持此交互模式",
    );
    const currentBounds = (await stage.boundingBox())!;
    await page.mouse.click(
      currentBounds.x + currentBounds.width * 0.8,
      currentBounds.y + currentBounds.height * 0.3,
    );
    await expect(menu).toBeHidden();
    await more.click();
    await page.setViewportSize({ width: 1440, height: 1080 });
    await expect(menu).toBeHidden();
    expect(await stage.evaluate((node, original) => node === original, originalStage)).toBe(true);
    expect(
      await stage
        .locator(".konvajs-content > canvas")
        .first()
        .evaluate((node, original) => node === original, originalCanvas),
    ).toBe(true);
    // Move focus away from the menu trigger without adding a fourth geometry point.
    await page.getByRole("button", { name: "适应", exact: true }).click();
    await page.keyboard.press("Enter");
    const picker = page.getByTestId("class-picker-popover");
    await expect(picker).toBeVisible();
    if (await more.isVisible()) {
      await more.click();
      await page.keyboard.press("Escape");
      await expect(menu).toBeHidden();
      await expect(more).toBeFocused();
      await expect(picker).toBeVisible();
    }
    await picker.getByText("car", { exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(picker).toBeHidden();
    await page.reload();
    const saved = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: data.headers,
    });
    expect(saved.ok()).toBe(true);
    const annotations = await saved.json();
    expect(annotations).toHaveLength(1);
    expect(annotations[0].geometry.points).toHaveLength(3);
    expect(pageErrors).toEqual([]);
  });
}
