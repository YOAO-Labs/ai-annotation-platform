import { test, expect } from "../fixtures/seed";
import { waitForApiWrite } from "../helpers/api-response";

test.describe("workbench pointcloud tools (键盘 handler 守护)", () => {
  test.setTimeout(90_000);

  test("B 键一次激活 → 连续创建 20 个框 → 保存期串行且 V 退出", async ({
    page,
    owned: data,
    seed,
  }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    await expect(page.getByText(/·\s*1\s*框/)).toBeVisible();

    let releaseFirstRequest: (() => void) | null = null;
    const firstRequestGate = new Promise<void>((resolve) => {
      releaseFirstRequest = resolve;
    });
    let boxPostCount = 0;
    await page.route("**/api/v1/tasks/*/annotations", async (route) => {
      if (route.request().method() === "POST") {
        boxPostCount += 1;
        if (boxPostCount === 1) await firstRequestGate;
      }
      await route.continue();
    });

    await page.locator("body").click();
    await page.keyboard.press("b");
    const creationStatus = page.getByTestId("three-d-creation-status");
    await expect(creationStatus).toHaveAttribute("data-phase", "armed");
    await expect(creationStatus).toContainText(/连续建框.*点击放置.*拖框拟合.*V\/Esc 退出/);
    await expect(page.getByTestId("three-d-tool-btn-box")).toHaveAttribute("aria-pressed", "true");

    const canvas = page.getByTestId("pc-viewport");
    await expect(canvas).toBeVisible();
    const box = await canvas.boundingBox();
    if (!box) throw new Error("canvas boundingBox 不可用");
    const point = {
      x: box.x + box.width * 0.5,
      y: box.y + box.height * 0.55,
    };
    const firstResponse = page.waitForResponse(
      (response) => response.request().method() === "POST" && /\/annotations$/.test(response.url()),
      { timeout: 10_000 },
    );
    await page.mouse.click(point.x, point.y);
    await expect(creationStatus).toHaveAttribute("data-phase", "saving");
    await page.mouse.click(point.x + 24, point.y);
    await page.waitForTimeout(250);
    expect(boxPostCount, "保存期间的第二次 pointer 不得发 POST").toBe(1);
    releaseFirstRequest?.();
    const first = await firstResponse;
    const body = first.request().postDataJSON() as { geometry?: { type?: string } };
    expect(body.geometry?.type).toBe("box_3d");
    await expect(creationStatus).toHaveAttribute("data-phase", "armed");

    await page.getByRole("button", { name: "俯视" }).click();
    await page.waitForTimeout(250);
    expect(boxPostCount, "点击 viewport 控件不得创建框").toBe(1);
    const fittedResponse = page.waitForResponse(
      (response) => response.request().method() === "POST" && /\/annotations$/.test(response.url()),
      { timeout: 10_000 },
    );
    await page.mouse.move(box.x + box.width * 0.38, box.y + box.height * 0.38);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.62, box.y + box.height * 0.62, { steps: 8 });
    await page.mouse.up();
    const fitted = await fittedResponse;
    const fittedBody = fitted.request().postDataJSON() as {
      geometry?: { type?: string; size?: number[] };
    };
    expect(fittedBody.geometry?.type).toBe("box_3d");
    expect(fittedBody.geometry?.size).toHaveLength(3);
    await expect(creationStatus).toHaveAttribute("data-phase", "armed");

    for (let index = 2; index < 20; index += 1) {
      const responsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" && /\/annotations$/.test(response.url()),
        { timeout: 10_000 },
      );
      await page.mouse.click(point.x + (index % 3) * 8, point.y + (index % 2) * 8);
      await responsePromise;
      await expect(creationStatus).toHaveAttribute("data-phase", "armed");
    }
    expect(boxPostCount).toBe(20);
    await expect(page.getByText(/·\s*21\s*框/)).toBeVisible({ timeout: 10_000 });

    await expect(page.getByLabel("展开详情")).toBeVisible();
    await page.keyboard.press("v");
    await expect(creationStatus).toHaveCount(0);
    await expect(page.getByTestId("three-d-tool-btn-select")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await page.mouse.click(point.x, point.y);
    await page.waitForTimeout(250);
    expect(boxPostCount).toBe(20);

    const firstUndoResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "DELETE" &&
        /\/annotations\/[0-9a-f-]+/.test(response.url()),
    );
    await page.keyboard.press("Control+z");
    await firstUndoResponse;
    await expect(page.getByText(/·\s*20\s*框/)).toBeVisible();
    const secondUndoResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "DELETE" &&
        /\/annotations\/[0-9a-f-]+/.test(response.url()),
    );
    await page.keyboard.press("Control+z");
    await secondUndoResponse;
    await expect(page.getByText(/·\s*19\s*框/)).toBeVisible();
    const redoResponse = page.waitForResponse(
      (response) => response.request().method() === "POST" && /\/annotations$/.test(response.url()),
    );
    await page.keyboard.press("Control+y");
    await redoResponse;
    await expect(page.getByText(/·\s*20\s*框/)).toBeVisible();
  });

  test("连续建框保存失败 → 回滚半成品并保持 armed 可重试", async ({ page, owned: data, seed }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${lidar.lidar_task_ids[0]}`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    let failNext = true;
    await page.route("**/api/v1/tasks/*/annotations", async (route) => {
      if (route.request().method() === "POST" && failNext) {
        failNext = false;
        await route.fulfill({ status: 500, json: { detail: "e2e create failure" } });
        return;
      }
      await route.continue();
    });

    await page.locator("body").click();
    await page.keyboard.press("b");
    const canvas = page.getByTestId("pc-viewport");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("canvas boundingBox 不可用");
    const point = { x: box.x + box.width * 0.5, y: box.y + box.height * 0.55 };

    const annotationsPath = `/tasks/${lidar.lidar_task_ids[0]}/annotations` as const;
    const failedResponse = waitForApiWrite(page, "POST", annotationsPath);
    await page.mouse.click(point.x, point.y);
    expect((await failedResponse).status()).toBe(500);
    const creationStatus = page.getByTestId("three-d-creation-status");
    await expect(creationStatus).toHaveAttribute("data-phase", "error");
    await expect(creationStatus).toContainText("保存失败，请重试");
    await expect(page.locator('[data-testid^="box-list-item-"]')).toHaveCount(1);
    await expect(page.getByTestId("three-d-tool-btn-box")).toHaveAttribute("aria-pressed", "true");

    const retryResponse = waitForApiWrite(page, "POST", annotationsPath);
    await page.mouse.click(point.x + 12, point.y);
    expect((await retryResponse).status()).toBe(201);
    await expect(creationStatus).toHaveAttribute("data-phase", "armed");
    await expect(page.locator('[data-testid^="box-list-item-"]')).toHaveCount(2);
  });

  test("保存期切任务 → 迟到响应不选中旧框且不污染新任务 history", async ({
    page,
    owned: data,
    seed,
  }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);
    const [firstTaskId, secondTaskId] = lidar.lidar_task_ids;

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${firstTaskId}`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    let releaseRequest: (() => void) | null = null;
    const requestGate = new Promise<void>((resolve) => {
      releaseRequest = resolve;
    });
    await page.route(`**/api/v1/tasks/${firstTaskId}/annotations`, async (route) => {
      if (route.request().method() === "POST") await requestGate;
      await route.continue();
    });
    let deleteCount = 0;
    page.on("request", (request) => {
      if (request.method() === "DELETE" && /\/annotations\//.test(request.url())) deleteCount += 1;
    });

    await page.locator("body").click();
    await page.keyboard.press("b");
    const canvas = page.getByTestId("pc-viewport");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("主点云 canvas boundingBox 不可用");
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.55);
    await expect(page.getByTestId("three-d-creation-status")).toHaveAttribute(
      "data-phase",
      "saving",
    );

    await page.getByText(/e2e-lidar-.*-1\.pcd/, { exact: true }).click();
    await expect.poll(() => page.url()).toContain(secondTaskId);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("three-d-creation-status")).toHaveCount(0);
    await expect(page.getByTestId("three-d-tool-btn-select")).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const oldResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes(`/tasks/${firstTaskId}/annotations`),
    );
    releaseRequest?.();
    const response = await oldResponse;
    const oldCreated = (await response.json()) as { id: string };

    await expect(page.locator('[data-testid^="box-list-item-"]')).toHaveCount(1);
    await expect(page.getByTestId(`box-list-item-${oldCreated.id}`)).toHaveCount(0);
    await page.keyboard.press("Control+z");
    await page.waitForTimeout(250);
    expect(deleteCount, "旧任务迟到成功不得写入新任务 history").toBe(0);
  });

  test("armed 中切换类别 → 下一框使用新类别且不退出", async ({ page, owned: data, seed }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate?task=${lidar.lidar_task_ids[0]}`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    const payloadClasses: string[] = [];
    page.on("request", (request) => {
      if (request.method() !== "POST" || !/\/annotations$/.test(request.url())) return;
      payloadClasses.push((request.postDataJSON() as { class_name: string }).class_name);
    });

    await page.locator("body").click();
    await page.keyboard.press("b");
    const status = page.getByTestId("three-d-creation-status");
    await expect(status).toContainText("car");
    const canvas = page.getByTestId("pc-viewport");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("主点云 canvas boundingBox 不可用");
    const point = { x: box.x + box.width * 0.5, y: box.y + box.height * 0.55 };

    const annotationsPath = `/tasks/${lidar.lidar_task_ids[0]}/annotations` as const;
    const firstResponse = waitForApiWrite(page, "POST", annotationsPath);
    await page.mouse.click(point.x, point.y);
    expect((await firstResponse).status()).toBe(201);
    await page
      .locator('[data-workbench-panel="class-palette"]')
      .getByText("pedestrian", { exact: true })
      .click();
    await expect(status).toContainText("pedestrian");

    const secondResponse = waitForApiWrite(page, "POST", annotationsPath);
    await page.mouse.click(point.x + 12, point.y);
    expect((await secondResponse).status()).toBe(201);
    expect(payloadClasses).toEqual(["car", "pedestrian"]);
    await expect(status).toHaveAttribute("data-phase", "armed");
    await expect(page.getByTestId("three-d-tool-btn-box")).toHaveAttribute("aria-pressed", "true");
  });

  test("点选框 → Delete 键 → DELETE /annotations/:id", async ({ page, owned: data, seed }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    const card = page.locator('[data-testid^="box-list-item-"]').first();
    await expect(card).toBeVisible({ timeout: 10_000 });
    await card.click({ position: { x: 12, y: 16 } });

    const delPromise = page.waitForRequest(
      (req) => req.method() === "DELETE" && /\/annotations\/[0-9a-f-]+/.test(req.url()),
      { timeout: 10_000 },
    );
    await page.keyboard.press("Delete");
    await delPromise;

    await expect(page.locator('[data-testid^="box-list-item-"]')).toHaveCount(0, {
      timeout: 10_000,
    });
  });

  test("P → 多边形模式 → 画 polygon → Enter → POST point_mask_3d", async ({
    page,
    owned: data,
    seed,
  }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);
    await seed.setPetEnabled(data.admin_email, true);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    await page.locator("body").click();
    await page.keyboard.press("p");

    const modeSelect = page.getByTestId("pointmask-mode-select");
    await expect(modeSelect).toBeVisible({ timeout: 10_000 });
    await modeSelect.selectOption("polygon");

    const canvas = page.getByTestId("pc-viewport");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("canvas boundingBox 不可用");
    const at = (fx: number, fy: number): [number, number] => [
      box.x + box.width * fx,
      box.y + box.height * fy,
    ];
    await page.mouse.click(...at(0.25, 0.3));
    await page.mouse.click(...at(0.75, 0.3));
    await page.mouse.click(...at(0.5, 0.75));

    const postPromise = page.waitForRequest(
      (req) =>
        req.method() === "POST" &&
        /\/annotations(\?|$)/.test(req.url().split("/api")[1] ?? req.url()),
      { timeout: 10_000 },
    );
    await page.keyboard.press("Enter");
    const post = await postPromise;
    const body = post.postDataJSON() as {
      annotation_type?: string;
      geometry?: { type?: string };
    };
    expect(body.annotation_type).toBe("point_mask_3d");
    expect(body.geometry?.type).toBe("point_mask_3d");
    await expect(page.getByTestId("three-d-selection-panel")).toHaveAttribute(
      "data-pet-linked",
      "true",
    );
    await expect(page.getByTestId("three-d-selection-panel-handle")).toBeVisible();
  });

  test("point-mask 多边形双击优先完成绘制，不触发框聚焦", async ({ page, owned: data, seed }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
    await page.locator("body").click();
    await page.keyboard.press("p");
    await page.getByTestId("pointmask-mode-select").selectOption("polygon");

    const canvas = page.getByTestId("pc-viewport");
    const box = await canvas.boundingBox();
    if (!box) throw new Error("canvas boundingBox 不可用");
    const at = (fx: number, fy: number): [number, number] => [
      box.x + box.width * fx,
      box.y + box.height * fy,
    ];
    await page.mouse.click(...at(0.25, 0.3));
    await page.mouse.click(...at(0.75, 0.3));
    const postResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        /\/annotations(\?|$)/.test(response.url().split("/api")[1] ?? response.url()),
      { timeout: 10_000 },
    );
    await page.mouse.dblclick(...at(0.5, 0.75));
    const response = await postResponsePromise;
    expect(response.ok()).toBe(true);
    const requestBody = (await response.request().postDataJSON()) as {
      geometry?: { type?: string };
    };
    expect(requestBody).toMatchObject({ geometry: { type: "point_mask_3d" } });
    const persisted = (await response.json()) as {
      id?: string;
      annotation_type?: string;
      geometry?: { type?: string };
    };
    expect(persisted.id).toBeTruthy();
    expect(persisted.annotation_type).toBe("point_mask_3d");
    expect(persisted.geometry?.type).toBe("point_mask_3d");
  });

  test("M 点云测量只写会话态 overlay，支持管理并在切任务时清理", async ({
    page,
    owned: data,
    seed,
  }) => {
    const lidar = await seed.seedLidar();
    await seed.injectToken(page, data.admin_email);
    const [, secondTaskId] = lidar.lidar_task_ids;

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects/${lidar.lidar_project_id}/annotate`);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });

    let annotationMutationCount = 0;
    page.on("request", (request) => {
      if (!/\/annotations(?:\/|\?|$)/.test(request.url())) return;
      if (["POST", "PATCH", "DELETE"].includes(request.method())) annotationMutationCount += 1;
    });

    await page.locator("body").click();
    await page.keyboard.press("m");
    await expect(page.getByTestId("three-d-tool-btn-measure")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByTestId("measurement-status")).toContainText("单击点云添加锚点");
    await expect(page.getByTestId("measurement-panel")).toBeVisible();

    const hits = await page.getByTestId("pc-viewport").evaluate((element) => {
      const scene = (
        element as HTMLElement & {
          __pointCloudScene?: {
            pickPoint: (
              clientX: number,
              clientY: number,
            ) => { pointIndex: number; position: [number, number, number] } | null;
          };
        }
      ).__pointCloudScene;
      if (!scene) return [];
      const bounds = element.getBoundingClientRect();
      const found: Array<{
        x: number;
        y: number;
        pointIndex: number;
        position: [number, number, number];
      }> = [];
      for (
        let y = Math.ceil(bounds.top + bounds.height * 0.15);
        y <= bounds.bottom - bounds.height * 0.15;
        y += 8
      ) {
        for (
          let x = Math.ceil(bounds.left + bounds.width * 0.15);
          x <= bounds.right - bounds.width * 0.15;
          x += 8
        ) {
          const hit = scene.pickPoint(x, y);
          if (!hit || found.some((candidate) => candidate.pointIndex === hit.pointIndex)) continue;
          found.push({ x, y, ...hit });
          if (
            found.length >= 2 &&
            Math.hypot(
              found[1].position[0] - found[0].position[0],
              found[1].position[1] - found[0].position[1],
              found[1].position[2] - found[0].position[2],
            ) > 0.1
          ) {
            return found.slice(0, 2);
          }
          if (found.length >= 2) found.splice(1);
        }
      }
      return found;
    });
    expect(hits, "开发态场景探针应找到两个不同的真实点").toHaveLength(2);

    await page.mouse.click(hits[0].x, hits[0].y);
    await page.mouse.click(hits[1].x, hits[1].y);
    const draft = page.getByTestId("measurement-draft");
    await expect(draft).toContainText("2 个锚点");
    await expect(draft).toContainText("三维总长");
    await expect(draft).toContainText(/\d+\.\d{2} m/);

    await page.getByRole("button", { name: "完成当前" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-testid^="measurement-item-"]')).toHaveCount(1);
    const measurement = page.locator('[data-testid^="measurement-item-"]').first();
    await expect(measurement).toContainText("测量 1 · 1 段");
    await expect(draft).toContainText("0 个锚点");
    expect(annotationMutationCount, "测量完成不得写 Annotation API").toBe(0);

    await page.getByRole("button", { name: "隐藏测量 1" }).click();
    await expect(page.getByRole("button", { name: "显示测量 1" })).toBeVisible();
    await page.getByRole("button", { name: "显示测量 1" }).click();
    await expect(page.getByRole("button", { name: "隐藏测量 1" })).toBeVisible();
    await page.getByRole("button", { name: "删除测量 1" }).click();
    await expect(page.locator('[data-testid^="measurement-item-"]')).toHaveCount(0);
    expect(annotationMutationCount, "测量显隐与删除不得写 Annotation API").toBe(0);

    await page.mouse.click(hits[0].x, hits[0].y);
    await page.mouse.click(hits[1].x, hits[1].y);
    await page.getByRole("button", { name: "完成当前" }).click();
    await expect(page.locator('[data-testid^="measurement-item-"]')).toHaveCount(1);

    await page.getByText(/e2e-lidar-.*-1\.pcd/, { exact: true }).click();
    await expect.poll(() => page.url()).toContain(secondTaskId);
    await expect(page.getByTestId("pointcloud-stats")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("three-d-tool-btn-select")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await page.keyboard.press("m");
    await expect(page.locator('[data-testid^="measurement-item-"]')).toHaveCount(0);
    await expect(page.getByTestId("measurement-draft")).toContainText("0 个锚点");
    expect(annotationMutationCount, "切任务清理测量不得写 Annotation API").toBe(0);

    await page.keyboard.press("v");
    await expect(page.getByTestId("three-d-tool-btn-select")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByTestId("measurement-panel")).toHaveCount(0);
  });
});
