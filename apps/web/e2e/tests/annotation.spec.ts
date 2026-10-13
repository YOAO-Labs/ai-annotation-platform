/**
 * Annotation smoke and real bbox creation: draw, choose a class, persist and reload.
 * Seed endpoints prepare prerequisites only; they never replace a failed UI save.
 */
import { test, expect } from "../fixtures/seed";
import type { Page } from "@playwright/test";

/**
 * smart-point / smart-box / exemplar 现按产出几何归属到 region 单位 (ToolDock 三层门控之层3);
 * 种子 E2E Demo Project 为 image-det, tool_bindings 仅启用 bbox, 故这些 AI 工具会被隐藏。
 * 拦截项目详情响应, 注入一个启用的 region 单位, 让交互工具在工具栏可见 (仅影响调用它的用例)。
 */
async function enableRegionUnit(page: Page): Promise<void> {
  await page.route(
    (url) => /\/api\/v1\/projects\/[^/]+$/.test(url.pathname),
    async (route) => {
      if (route.request().method() !== "GET") {
        await route.fallback();
        return;
      }
      const resp = await route.fetch();
      const json = (await resp.json()) as {
        tool_bindings?: Record<string, unknown>;
      };
      json.tool_bindings = {
        ...(json.tool_bindings ?? {}),
        region: { enabled: true, classes: [{ name: "object", order: 0 }] },
      };
      await route.fulfill({
        status: resp.status(),
        contentType: "application/json",
        body: JSON.stringify(json),
      });
    },
  );
}

test.describe("annotation workbench", () => {
  test("annotator 登录 → /annotate 路由可达（smoke）", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.injectToken(page, data.annotator_email);
    await page.goto("/annotate");
    await expect(page).toHaveURL(/\/annotate/);
    await page.waitForLoadState("networkidle");
  });

  for (const saveFails of [false, true]) {
    test(
      saveFails
        ? "bbox 业务拒绝不产生已保存标注，刷新仍为空"
        : "bbox 真实绘制、选类、落库并刷新恢复",
      async ({ page, seed }) => {
        const data = await seed.owned();
        const taskId = data.task_ids[0];
        await seed.advanceTask({
          taskId,
          toStatus: "pending",
          annotatorEmail: data.annotator_email,
        });
        await seed.injectToken(page, data.annotator_email);
        const headers = { Authorization: `Bearer ${await seed.accessToken(data.annotator_email)}` };
        const annotationPath = `/api/v1/tasks/${taskId}/annotations`;
        const readAnnotations = async () => {
          const response = await page.request.get(annotationPath, { headers });
          expect(response.ok(), await response.text()).toBe(true);
          return response.json();
        };
        if (saveFails) {
          await page.route(
            (url) => url.pathname === annotationPath,
            async (route) => {
              if (route.request().method() !== "POST") return route.fallback();
              await route.fulfill({
                status: 422,
                json: { detail: "E2E annotation validation failure" },
              });
            },
          );
        }
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
        const stage = page.getByTestId("workbench-stage");
        await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
        const bboxBtn = page.getByTestId("tool-btn-box");
        await bboxBtn.click();
        await expect(bboxBtn).toHaveAttribute("aria-pressed", "true");
        expect(await readAnnotations()).toEqual([]);

        // Existing media geometry accounts for letterboxing and changing panel sizes.
        const points = await stage.evaluate((node) => {
          const rect = node.getBoundingClientRect();
          const width = Number(node.getAttribute("data-media-width"));
          const height = Number(node.getAttribute("data-media-height"));
          const x = rect.x + Number(node.getAttribute("data-media-x"));
          const y = rect.y + Number(node.getAttribute("data-media-y"));
          return {
            width,
            height,
            start: { x: Math.round(x + width * 0.25), y: Math.round(y + height * 0.25) },
            end: { x: Math.round(x + width * 0.55), y: Math.round(y + height * 0.55) },
          };
        });
        expect(points.width).toBeGreaterThan(0);
        expect(points.height).toBeGreaterThan(0);
        for (const point of [points.start, points.end]) {
          expect(
            await stage.evaluate((node, at) => {
              const target = document.elementFromPoint(at.x, at.y);
              return target instanceof HTMLCanvasElement && node.contains(target);
            }, point),
            "绘制坐标必须命中画布，不能被面板遮挡",
          ).toBe(true);
        }
        await page.mouse.move(points.start.x, points.start.y);
        await page.mouse.down();
        await expect(stage).toHaveAttribute("data-drag-kind", "draw");
        await page.mouse.move(points.end.x, points.end.y, { steps: 8 });
        await page.mouse.up();
        const picker = page.getByTestId("class-picker-popover");
        await expect(picker).toBeVisible();
        const [savedResponse] = await Promise.all([
          page.waitForResponse(
            (response) =>
              new URL(response.url()).pathname === annotationPath &&
              response.request().method() === "POST",
          ),
          picker.locator("span").filter({ hasText: /^car$/ }).click(),
        ]);
        expect(savedResponse.request().postDataJSON()).toMatchObject({
          annotation_type: "bbox",
          class_name: "car",
        });
        expect(savedResponse.status()).toBe(saveFails ? 422 : 201);
        if (saveFails) {
          await expect(stage).toHaveAttribute("data-user-box-count", "0");
          expect(await readAnnotations()).toEqual([]);
          await page.reload();
          await expect(stage).toHaveAttribute("data-image-ready", "true");
          await expect(stage).toHaveAttribute("data-user-box-count", "0");
          expect(await readAnnotations()).toEqual([]);
          return;
        }
        const annotation = await savedResponse.json();
        expect(annotation).toMatchObject({
          id: expect.any(String),
          task_id: taskId,
          annotation_type: "bbox",
          class_name: "car",
          geometry: { type: "bbox" },
        });
        for (const [key, expected] of Object.entries({ x: 0.25, y: 0.25, w: 0.3, h: 0.3 })) {
          expect(annotation.geometry[key]).toBeCloseTo(expected, 2);
        }
        await expect(picker).toBeHidden();
        await expect(stage).toHaveAttribute("data-user-box-count", "1");
        expect(await readAnnotations()).toEqual([
          expect.objectContaining({
            id: annotation.id,
            class_name: "car",
            geometry: annotation.geometry,
          }),
        ]);
        await page.reload();
        await expect(stage).toHaveAttribute("data-image-ready", "true");
        await expect(stage).toHaveAttribute("data-user-box-count", "1");
        expect(await readAnnotations()).toEqual([
          expect.objectContaining({
            id: annotation.id,
            class_name: "car",
            geometry: annotation.geometry,
          }),
        ]);
      },
    );
  }

  /**
   * v0.10.2 · Prompt-first ToolDock + capability 协商.
   * mock /setup 返回 grounded-sam2 (point/bbox/text), 断言:
   *   ① smart-point / smart-box 可点 (v0.14.18 · text-prompt 已归批量线, 不在工具栏)
   *   ② exemplar 工具置灰 (aria-disabled="true")
   *   ③ AIToolDrawer 在 smart-point 激活时出现
   *   ④ 点击 stage 触发 /interactive-annotating, body.context.type === "point"
   */
  test("Prompt-first · grounded-sam2 capability → smart-point dispatch", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    await seed.injectToken(page, data.annotator_email);

    // mock /setup → grounded-sam2 (无 exemplar)
    await page.route(/\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/setup/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          name: "grounded-sam2",
          version: "0.9.0",
          is_interactive: true,
          labels: [],
          supported_prompts: ["point", "bbox", "text"],
          supported_text_outputs: ["box", "mask", "both"],
          params: {
            type: "object",
            properties: {
              box_threshold: {
                type: "number",
                minimum: 0,
                maximum: 1,
                default: 0.25,
                title: "Box 阈值",
              },
            },
          },
        }),
      });
    });

    let interactiveCalls = 0;
    const state: { lastBody: { context?: { type?: string } } | null } = { lastBody: null };
    await page.route(
      /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/interactive-annotating/,
      async (route, req) => {
        interactiveCalls += 1;
        try {
          state.lastBody = req.postDataJSON() as { context?: { type?: string } };
        } catch {
          /* noop */
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            result: [
              {
                type: "polygonlabels",
                value: {
                  points: [
                    [0.3, 0.3],
                    [0.6, 0.3],
                    [0.6, 0.6],
                    [0.3, 0.6],
                  ],
                  polygonlabels: ["object"],
                },
                score: 0.95,
              },
            ],
            score: 0.95,
            inference_time_ms: 42,
          }),
        });
      },
    );

    // 交互工具 (smart-*) 归 region 单位, 种子项目仅 bbox, 需启用 region 使其可见
    await enableRegionUnit(page);
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    // ① 交互 AI 工具按钮可见 (text-prompt 已归批量线, 工具栏不再有)
    const pointBtn = page.getByTestId("tool-btn-smart-point");
    const boxBtn = page.getByTestId("tool-btn-smart-box");
    const exemplarBtn = page.getByTestId("tool-btn-exemplar");
    await expect(pointBtn).toBeVisible({ timeout: 10_000 });
    await expect(boxBtn).toBeVisible();
    await expect(exemplarBtn).toBeVisible();

    // ② exemplar 工具置灰
    await expect(exemplarBtn).toHaveAttribute("aria-disabled", "true");

    // ③ 激活 smart-point → 画布顶部交互工具栏出现 (v0.18.25 AIToolDrawer 退役改 InteractiveToolBar)
    await pointBtn.click();
    await expect(pointBtn).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("interactive-tool-capsule")).toBeVisible();

    // ④ 点击 stage → dispatch context.type === "point"
    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");
    await page.mouse.click(box.x + box.width * 0.4, box.y + box.height * 0.4);
    await page.waitForTimeout(300);

    expect(interactiveCalls).toBeGreaterThanOrEqual(1);
    expect(state.lastBody?.context?.type).toBe("point");
  });

  /**
   * v0.10.2 · sam3 capability → exemplar 工具可用; smart-point 置灰; 拖框 → exemplar dispatch.
   */
  test("Prompt-first · sam3 capability → exemplar dispatch", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    await seed.injectToken(page, data.annotator_email);

    await page.route(/\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/setup/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          name: "sam3-backend",
          version: "0.10.0",
          is_interactive: true,
          labels: [],
          supported_prompts: ["bbox", "text", "exemplar"],
          supported_text_outputs: ["box", "mask", "both"],
          params: { type: "object", properties: {} },
        }),
      });
    });

    let interactiveCalls = 0;
    const state: { lastBody: { context?: { type?: string } } | null } = { lastBody: null };
    await page.route(
      /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/interactive-annotating/,
      async (route, req) => {
        interactiveCalls += 1;
        try {
          state.lastBody = req.postDataJSON() as { context?: { type?: string } };
        } catch {
          /* noop */
        }
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            result: [
              {
                type: "polygonlabels",
                value: {
                  points: [
                    [0.2, 0.2],
                    [0.4, 0.2],
                    [0.4, 0.4],
                    [0.2, 0.4],
                  ],
                  polygonlabels: ["object"],
                },
                score: 0.88,
              },
            ],
            score: 0.88,
            inference_time_ms: 55,
          }),
        });
      },
    );

    // 同上: exemplar / smart-point 归 region 单位, 需启用 region 使其在工具栏可见
    await enableRegionUnit(page);
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    // smart-point 置灰; exemplar 可用
    const pointBtn = page.getByTestId("tool-btn-smart-point");
    const exemplarBtn = page.getByTestId("tool-btn-exemplar");
    await expect(pointBtn).toBeVisible({ timeout: 10_000 });
    await expect(pointBtn).toHaveAttribute("aria-disabled", "true");
    await expect(exemplarBtn).not.toHaveAttribute("aria-disabled", "true");

    await exemplarBtn.click();
    await expect(exemplarBtn).toHaveAttribute("aria-pressed", "true");

    // 拖框 → exemplar
    // 注意：选中 AI 工具会打开 AIToolDrawer 悬浮参数面板，覆盖画布左侧约 1/3
    // （v0.10.x「AI 参数迁至悬浮面板」）。拖框起点须落在面板右侧的空白画布区，
    // 否则 pointerdown 命中面板而非 Konva canvas，samProbe 不会触发。
    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");
    const sx = box.x + box.width * 0.55;
    const sy = box.y + box.height * 0.4;
    const ex = box.x + box.width * 0.8;
    const ey = box.y + box.height * 0.65;
    await page.mouse.move(sx, sy);
    await page.mouse.down();
    await page.mouse.move(ex, ey, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(300);

    expect(interactiveCalls).toBeGreaterThanOrEqual(1);
    expect(state.lastBody?.context?.type).toBe("exemplar");
  });
});
