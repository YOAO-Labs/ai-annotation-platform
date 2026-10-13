import { panelCommand } from "../fixtures/workbench-panel-actions";
import { layoutCommand } from "../helpers/workbench-layout";
import {
  expectRenderedGroups,
  registerWorkbenchLayoutMatrixTests,
  renderedGroups,
  rememberCanvas,
  savedContext,
  workspace,
} from "../helpers/workbench-layout-matrix";
import { expect, test } from "../fixtures/seed";

// This filename selects the pointcloud project (SwiftShader software WebGL),
// so it hosts only the 3D scenarios. Image and video scenarios run in
// workbench-layout-matrix.spec.ts under the chromium project: their canvases
// need no WebGL, and software GL made CI stress reloads starve the renderer.

test("3D 自由布局保留共享 renderer，三视图移出画布与相机整组切换后恢复宽度", async ({
  page,
  seed,
}) => {
  test.setTimeout(120_000);
  const data = await seed.owned();
  const lidar = await seed.seedLidar();
  await seed.injectToken(page, data.admin_email);
  const desktop = { width: 1920, height: 1080 };
  await page.setViewportSize(desktop);
  await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
  await layoutCommand(page, "标准标注布局");
  const sameCanvas = await rememberCanvas(page, "3d");
  const tri = page.getByTestId("tri-view-renderer-panel");
  const triPanel = page.locator('[data-workbench-panel="tri-view"]');
  const gallery = page.locator('[data-workbench-panel="camera-view"]');
  const floatMenus = page.getByRole("button", { name: /布局菜单$/ });
  await page
    .locator('[data-testid^="box-list-item-"]')
    .first()
    .click({ position: { x: 12, y: 16 } });
  await layoutCommand(page, "框体精修");
  await expect(tri).toBeVisible();
  const originalTri = await tri.elementHandle();
  await panelCommand(page, "三视图精修", "停靠到左侧");
  await expect
    .poll(async () => {
      const triBounds = await tri.boundingBox();
      const canvasBounds = await page.getByTestId("pc-viewport").boundingBox();
      return triBounds!.x + triBounds!.width - canvasBounds!.x;
    })
    .toBeLessThanOrEqual(1);
  await expect
    .poll(async () =>
      Number(await page.getByTestId("pc-viewport").getAttribute("data-pointcloud-tri-pass-count")),
    )
    .toBeGreaterThan(0);
  await sameCanvas();

  // Real tab drag changes topology; selecting the sibling must suspend the hidden tri viewport.
  await page
    .getByRole("tab", { name: "三视图精修", exact: true })
    .dragTo(page.getByRole("tab", { name: "标注详情", exact: true }));
  await expect
    .poll(async () =>
      (await renderedGroups(page)).some(
        (group) => group.tabs.includes("tri-view") && group.tabs.includes("inspector"),
      ),
    )
    .toBe(true);
  await page.getByRole("tab", { name: "标注详情", exact: true }).click();
  await expect(triPanel).toHaveAttribute("aria-hidden", "true");
  await page.getByRole("tab", { name: "三视图精修", exact: true }).click();
  await expect(triPanel).toHaveAttribute("aria-hidden", "false");
  await panelCommand(page, "三视图精修", "浮动面板");
  await expect
    .poll(
      async () =>
        (await renderedGroups(page)).find((group) => group.tabs.includes("tri-view"))?.floating,
    )
    .toBe(true);
  await sameCanvas();
  await page.getByRole("button", { name: "隐藏三视图精修", exact: true }).press("Enter");
  await expect(tri).toBeHidden();
  await expect(triPanel).toHaveCount(1);
  await layoutCommand(page, "三视图精修");
  expect(await tri.evaluate((node, original) => node === original, originalTri)).toBe(true);
  await expect(triPanel).toHaveAttribute("aria-hidden", "false");
  await sameCanvas();

  await layoutCommand(page, "传感器融合");
  await expect(tri).toBeHidden();
  await expect(floatMenus.first()).toBeVisible();
  const cameraCount = await floatMenus.count();
  await layoutCommand(page, "全部相机停靠");
  await expect(floatMenus).toHaveCount(0);
  await expect(gallery).toHaveAttribute("aria-hidden", "false");
  await expect(gallery.locator("[data-camera-dock-panel] section")).toHaveCount(cameraCount);
  const initialGallery = (await renderedGroups(page)).find((group) =>
    group.tabs.includes("camera-view"),
  )!;
  const workBounds = await workspace(page).boundingBox();
  expect(Math.abs(initialGallery.rect.width - workBounds!.width * 0.15)).toBeLessThanOrEqual(1);

  const sash = await workspace(page)
    .locator(".dv-sash")
    .evaluateAll((elements, boundary) => {
      const rectangles = elements.map((element) => element.getBoundingClientRect());
      const match = rectangles.find(
        (rect) =>
          rect.height > rect.width &&
          rect.width > 0 &&
          Math.abs(rect.left + rect.width / 2 - boundary) < 5,
      );
      return match ? { x: match.left + match.width / 2, y: match.top + match.height / 2 } : null;
    }, initialGallery.rect.x);
  expect(sash, "图库左侧应有可拖动的分隔条").not.toBeNull();
  await page.mouse.move(sash!.x, sash!.y);
  await page.mouse.down();
  await page.mouse.move(sash!.x - 80, sash!.y, { steps: 8 });
  await page.mouse.up();
  await expect
    .poll(
      async () =>
        (await renderedGroups(page)).find((group) => group.tabs.includes("camera-view"))!.rect
          .width,
    )
    .toBeGreaterThan(initialGallery.rect.width + 40);
  const resized = (await renderedGroups(page)).find((group) =>
    group.tabs.includes("camera-view"),
  )!.rect;
  await page.getByRole("button", { name: "悬浮显示", exact: true }).click();
  await expect(gallery).toHaveAttribute("aria-hidden", "true");
  await expect(floatMenus).toHaveCount(cameraCount);
  await layoutCommand(page, "全部相机停靠");
  await expect(gallery).toHaveAttribute("aria-hidden", "false");
  await expect
    .poll(async () => {
      const current = (await renderedGroups(page)).find((group) =>
        group.tabs.includes("camera-view"),
      )!.rect;
      return Math.max(
        ...(["x", "y", "width", "height"] as const).map((key) =>
          Math.abs(current[key] - resized[key]),
        ),
      );
    })
    .toBeLessThanOrEqual(1);
  await sameCanvas();

  await panelCommand(page, "任务队列", "停靠到左侧");
  const arranged = await renderedGroups(page);
  await layoutCommand(page, "专注画布布局");
  await expect(gallery).toHaveAttribute("aria-hidden", "true");
  await layoutCommand(page, "恢复画布布局");
  await expect(gallery).toHaveAttribute("aria-hidden", "false");
  await expectRenderedGroups(page, arranged);
  await page.setViewportSize({ width: 1024, height: desktop.height });
  await expect(workspace(page)).toHaveAttribute("data-compact", "true");
  await page.setViewportSize(desktop);
  await expect(workspace(page)).toHaveAttribute("data-compact", "false");
  await sameCanvas();
  await expectRenderedGroups(page, arranged);
  // A docked camera flag can still belong to the snapshot before the final rearrangement.
  // Wait for the complete local tree to reach the server before testing cold restoration.
  const local = await page.evaluate(() => {
    const { state } = JSON.parse(localStorage.getItem("auth-storage")!);
    return JSON.parse(localStorage.getItem(`workbench.${state.user.id}.workspace.annotate:3d`)!);
  });
  expect(local.schemaVersion).toBe(5);
  expect(local.snapshot.cameraPresentation).toBe("docked");
  await expect.poll(() => savedContext(page, "annotate:3d")).toEqual(local);
  await page.reload();
  await (
    await rememberCanvas(page, "3d")
  )();
  await expectRenderedGroups(page, arranged);
  await expect(gallery).toHaveAttribute("aria-hidden", "false");
  await expect(floatMenus).toHaveCount(0);
});

registerWorkbenchLayoutMatrixTests(["3d"]);
