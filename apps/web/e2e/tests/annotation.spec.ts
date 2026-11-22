import { test, expect } from "../fixtures/seed";
import type { Page } from "@playwright/test";

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

  test("Prompt-first · grounded-sam2 capability → smart-point dispatch", async ({ page, seed }) => {
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

    await enableRegionUnit(page);
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    const pointBtn = page.getByTestId("tool-btn-smart-point");
    const boxBtn = page.getByTestId("tool-btn-smart-box");
    const exemplarBtn = page.getByTestId("tool-btn-exemplar");
    await expect(pointBtn).toBeVisible({ timeout: 10_000 });
    await expect(boxBtn).toBeVisible();
    await expect(exemplarBtn).toBeVisible();

    await expect(exemplarBtn).toHaveAttribute("aria-disabled", "true");

    await pointBtn.click();
    await expect(pointBtn).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("interactive-tool-capsule")).toBeVisible();

    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");
    await page.mouse.click(box.x + box.width * 0.4, box.y + box.height * 0.4);
    await page.waitForTimeout(300);

    expect(interactiveCalls).toBeGreaterThanOrEqual(1);
    expect(state.lastBody?.context?.type).toBe("point");
  });

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

    await enableRegionUnit(page);
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    const pointBtn = page.getByTestId("tool-btn-smart-point");
    const exemplarBtn = page.getByTestId("tool-btn-exemplar");
    await expect(pointBtn).toBeVisible({ timeout: 10_000 });
    await expect(pointBtn).toHaveAttribute("aria-disabled", "true");
    await expect(exemplarBtn).not.toHaveAttribute("aria-disabled", "true");

    await exemplarBtn.click();
    await expect(exemplarBtn).toHaveAttribute("aria-pressed", "true");

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
