import { panelCommand } from "../fixtures/workbench-panel-actions";
import { layoutCommand } from "./workbench-layout";
import type { Page, Request } from "@playwright/test";
import type {
  WorkspaceContext,
  WorkspaceEnvelope,
  WorkspaceSnapshot,
} from "../../src/pages/Workbench/layout/workbenchLayoutSnapshot";
import { expect, test } from "../fixtures/seed";

// Shared by workbench-pointcloud-layout-matrix.spec.ts (3D, pointcloud
// project) and workbench-layout-matrix.spec.ts (image/video, chromium
// project). Keep both files' titles identical so sharding and extended-suite
// selection stay stable.

const VIEWPORTS = [
  { width: 1366, height: 900 },
  { width: 1920, height: 1080 },
] as const;

// A post-reload layout restore can stall the renderer main thread for tens of
// seconds on loaded CI runners before the manifest query even fires (observed
// ~28s on a nightly layout-stress run); the identity assertions themselves are
// instant once the stage mounts, so @stress scenarios get a generous budget.
const STRESS_STAGE_TIMEOUT_MS = 90_000;

export const workspace = (page: Page) => page.locator("[data-workbench-workspace]");

export async function savedContext(
  page: Page,
  context: WorkspaceContext,
): Promise<WorkspaceEnvelope> {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  const response = await page.request.get("/api/v1/auth/me/preferences", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).workbench.layout.workspace?.contexts[context];
}

export async function rememberCanvas(page: Page, kind: "image" | "video" | "3d", timeout = 30_000) {
  const wrapper = page.locator('[data-workbench-panel="canvas"]');
  const stage = page.getByTestId(
    kind === "image" ? "workbench-stage" : kind === "video" ? "video-konva-stage" : "pc-viewport",
  );
  const canvas =
    kind === "3d"
      ? page.locator("[data-workbench-render-surface] > canvas")
      : stage.locator(".konvajs-content > canvas").first();
  // The stage host mounts only after its payload resolves (the video host
  // awaits its manifest), and the canvas follows in the same render pass.
  // Waiting for the host first keeps slow cold starts from failing here with
  // an opaque "canvas not found".
  await expect(stage).toBeVisible({ timeout });
  await expect(canvas).toBeVisible({ timeout });
  if (kind === "3d") await expect(page.getByTestId("pointcloud-stats")).toBeVisible();
  const originalWrapper = await wrapper.elementHandle();
  const originalStage = await stage.elementHandle();
  const originalCanvas = await canvas.elementHandle();
  const originalContext =
    kind === "3d"
      ? await canvas.evaluateHandle((node) => (node as HTMLCanvasElement).getContext("webgl2"))
      : undefined;
  return async () => {
    await expect(wrapper).toHaveCount(1);
    await expect(stage).toBeVisible();
    expect(await wrapper.evaluate((node, original) => node === original, originalWrapper)).toBe(
      true,
    );
    expect(await stage.evaluate((node, original) => node === original, originalStage)).toBe(true);
    expect(await canvas.evaluate((node, original) => node === original, originalCanvas)).toBe(true);
    if (originalContext) {
      expect(
        await canvas.evaluate(
          (node, original) => (node as HTMLCanvasElement).getContext("webgl2") === original,
          originalContext,
        ),
      ).toBe(true);
      expect(
        await originalContext.evaluate((context) => context !== null && !context.isContextLost()),
      ).toBe(true);
      await expect(stage).toHaveAttribute("data-pointcloud-renderer-count", "1");
      await expect(page.locator("[data-workbench-render-surface] > canvas")).toHaveCount(1);
    }
  };
}

/** Read rendered groups, not a cached preference or a private Dockview API. */
export async function renderedGroups(page: Page) {
  return workspace(page)
    .locator(".dv-groupview")
    .evaluateAll((elements) =>
      elements
        .flatMap((element) => {
          const rect = element.getBoundingClientRect();
          const tabs = Array.from(
            element.querySelectorAll<HTMLElement>(".dv-tab[data-tab-panel-id]"),
          );
          if (!rect.width || !rect.height || !tabs.length) return [];
          return [
            {
              tabs: tabs.map((tab) => tab.dataset.tabPanelId!),
              active: tabs.find((tab) => tab.getAttribute("aria-selected") === "true")?.dataset
                .tabPanelId,
              floating: element.classList.contains("dv-groupview-floating"),
              rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
            },
          ];
        })
        .sort((left, right) => left.tabs.join(",").localeCompare(right.tabs.join(","))),
    );
}

export async function expectRenderedGroups(
  page: Page,
  expected: Awaited<ReturnType<typeof renderedGroups>>,
) {
  await expect
    .poll(
      async () => {
        const current = await renderedGroups(page);
        return current.map(({ rect: _rect, ...group }) => group);
      },
      { timeout: 20_000 },
    )
    .toEqual(expected.map(({ rect: _rect, ...group }) => group));
  await expect
    .poll(
      async () => {
        const current = await renderedGroups(page);
        if (current.length !== expected.length) return Infinity;
        return Math.max(
          ...current.flatMap((group, index) =>
            (["x", "y", "width", "height"] as const).map((key) =>
              Math.abs(group.rect[key] - expected[index].rect[key]),
            ),
          ),
        );
      },
      { timeout: 20_000 },
    )
    .toBeLessThanOrEqual(1);
}

export type LayoutMatrixKind = "image" | "video" | "3d";

export function registerWorkbenchLayoutMatrixTests(kinds: readonly LayoutMatrixKind[]) {
  // Two complete action cycles cover transitions; repetition belongs to @stress.
  for (const reorders of [10, 50]) {
    for (const mode of ["annotate", "review"] as const) {
      for (const kind of kinds) {
        test(
          `${mode}:${kind} 连续${reorders + 4}次面板重排保留画布，跨视口和紧凑模式后刷新恢复`,
          { tag: reorders === 50 ? "@stress" : "@layout" },
          async ({ page, seed }) => {
            test.setTimeout(reorders === 50 ? 180_000 : 90_000);
            const stageTimeout = reorders === 50 ? STRESS_STAGE_TIMEOUT_MS : undefined;
            const context: WorkspaceContext = `${mode}:${kind}`;
            const data = await seed.owned();
            let projectId = data.project_id;
            let taskId = data.task_ids[0];
            if (kind === "video") taskId = (await seed.videoTask(projectId)).task_id;
            if (kind === "3d") {
              const lidar = await seed.seedLidar();
              projectId = lidar.lidar_project_id;
              taskId = lidar.lidar_task_ids[0];
            }
            if (mode === "review")
              await seed.advanceTask({
                taskId,
                toStatus: "review",
                annotatorEmail: data.annotator_email,
                reviewerEmail: data.admin_email,
              });
            await seed.injectToken(page, data.admin_email);
            const initialViewport = VIEWPORTS[mode === "annotate" ? 0 : 1];
            const finalViewport = VIEWPORTS[mode === "annotate" ? 1 : 0];
            await page.setViewportSize(initialViewport);
            await page.goto(`/projects/${projectId}/${mode}?task=${taskId}`);

            let latestSubmitted: WorkspaceSnapshot | undefined;
            const writes: string[][] = [];
            const inFlight = new Set<Request>();
            page.on("request", (request) => {
              if (
                request.method() !== "PATCH" ||
                new URL(request.url()).pathname !== "/api/v1/auth/me/preferences"
              )
                return;
              const contexts = request.postDataJSON()?.workbench?.layout?.workspace?.contexts;
              if (!contexts) return;
              writes.push(Object.keys(contexts));
              latestSubmitted = contexts[context]?.snapshot;
              inFlight.add(request);
            });
            page.on("requestfinished", (request) => inFlight.delete(request));
            page.on("requestfailed", (request) => inFlight.delete(request));

            await layoutCommand(page, "标准标注布局");
            const sameCanvas = await rememberCanvas(page, kind, stageTimeout);
            const commands = [
              "停靠到左侧",
              "停靠到右侧",
              "停靠到底部",
              "与标注详情合并为标签",
              "浮动面板",
            ];
            for (let operation = 0; operation < reorders; operation += 1) {
              await test.step(`拖动重排 ${operation + 1}`, async () => {
                if (operation === Math.floor(reorders / 2))
                  await page.setViewportSize(finalViewport);
                // Leave a tab group and a floating group in the final saved tree.
                await panelCommand(
                  page,
                  operation === reorders - 1 ? "类别面板" : "讨论",
                  commands[operation % commands.length],
                );
                await sameCanvas();
              });
            }
            for (const command of ["停靠到左侧", "停靠到右侧", "停靠到底部", "停靠到左侧"]) {
              await panelCommand(page, "任务队列", command);
              await sameCanvas();
            }
            // The owner debounces by 300 ms; wait for the last command and its serial request.
            await page.waitForTimeout(650);
            await expect.poll(() => inFlight.size).toBe(0);
            expect(latestSubmitted).toBeDefined();
            await expect
              .poll(async () => (await savedContext(page, context))?.snapshot)
              .toEqual(latestSubmitted);
            const saved = await savedContext(page, context);
            expect(saved.schemaVersion).toBe(5);
            expect(writes.length).toBeGreaterThan(0);
            expect(writes.every((keys) => keys.length === 1 && keys[0] === context)).toBe(true);
            const desktopGroups = await renderedGroups(page);
            expect(
              desktopGroups.some((group) => group.tabs.join(",") === "inspector,discussion"),
            ).toBe(true);
            expect(
              desktopGroups.some((group) => group.floating && group.tabs.includes("class-palette")),
            ).toBe(true);

            const writesBeforeCompact = writes.length;
            await page.setViewportSize({ width: 1024, height: finalViewport.height });
            await expect(workspace(page)).toHaveAttribute("data-compact", "true");
            await layoutCommand(page, "任务队列");
            await sameCanvas();
            await layoutCommand(page, "讨论");
            await sameCanvas();
            await page.setViewportSize(finalViewport);
            await expect(workspace(page)).toHaveAttribute("data-compact", "false");
            await sameCanvas();
            await expectRenderedGroups(page, desktopGroups);
            await page.waitForTimeout(650);
            expect(writes.length).toBe(writesBeforeCompact);
            expect(await savedContext(page, context)).toEqual(saved);

            await page.reload();
            const restoredCanvas = await rememberCanvas(page, kind, stageTimeout);
            await restoredCanvas();
            await expect(page.getByRole("button", { name: "布局", exact: true })).toBeVisible();
            await expect(workspace(page)).toHaveAttribute("data-compact", "false");
            await expect(
              page.getByText("保存的布局无法恢复，请从布局菜单重置。", { exact: true }),
            ).toHaveCount(0);
            await expectRenderedGroups(page, desktopGroups);
            expect(await savedContext(page, context)).toEqual(saved);
          },
        );
      }
    }
  }
}
