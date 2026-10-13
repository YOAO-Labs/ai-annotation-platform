import { expect, test } from "../fixtures/seed";

test("图片旋转框选中后拖动、保存、撤销重做及刷新恢复", async ({ page, seed }, testInfo) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const taskId = data.task_ids[0];
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const projectPath = `/api/v1/projects/${data.project_id}`;
  const projectResponse = await page.request.get(projectPath, { headers });
  expect(projectResponse.ok(), await projectResponse.text()).toBe(true);
  const project = await projectResponse.json();
  const configured = await page.request.patch(projectPath, {
    headers,
    data: {
      ai_enabled: false,
      ai_interactive_enabled: false,
      ml_backend_id: null,
      tool_bindings: {
        ...project.tool_bindings,
        rotated_bbox: { enabled: true, classes: [{ name: "car" }], attribute_schema: {} },
      },
    },
  });
  expect(configured.ok(), await configured.text()).toBe(true);
  const detached = await page.request.delete(`${projectPath}/ml-backends/${data.ml_backend_id}`, {
    headers,
  });
  expect(detached.status()).toBe(204);
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  const geometry = { type: "rotated_bbox", cx: 0.45, cy: 0.5, w: 0.22, h: 0.2, angle: 35 };
  const annotation = await seed.createTaskAnnotation(taskId, data.annotator_email, {
    annotation_type: "rotated_bbox",
    tool_unit_id: "rotated_bbox",
    class_name: "car",
    geometry,
  });
  await seed.injectToken(page, data.annotator_email);
  await page.setViewportSize({ width: 1440, height: 900 });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/") && response.status() >= 400)
      errors.push(`${response.request().method()} ${path}: ${response.status()}`);
  });
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await page.getByTestId("tool-btn-select").click();

  const point = async (cx: number, cy: number) =>
    stage.evaluate(
      (element, center) => {
        const rect = element.getBoundingClientRect();
        return {
          x:
            rect.left +
            Number(element.getAttribute("data-media-x")) +
            center.cx * Number(element.getAttribute("data-media-width")),
          y:
            rect.top +
            Number(element.getAttribute("data-media-y")) +
            center.cy * Number(element.getAttribute("data-media-height")),
        };
      },
      { cx, cy },
    );
  const start = await point(geometry.cx, geometry.cy);
  const end = await point(geometry.cx + 0.12, geometry.cy + 0.1);
  for (const at of [start, end]) {
    expect(
      await stage.evaluate((element, position) => {
        const hit = document.elementFromPoint(position.x, position.y);
        return hit instanceof HTMLCanvasElement && element.contains(hit);
      }, at),
    ).toBe(true);
  }
  await page.mouse.click(start.x, start.y);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await expect(stage).toHaveAttribute("data-drag-kind", "moveRotatedBox");
  await page.mouse.move(end.x, end.y, { steps: 10 });
  const annotationPath = `/api/v1/tasks/${taskId}/annotations/${annotation.id}`;
  const nextSave = () =>
    page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === annotationPath &&
        response.request().method() === "PATCH",
      { timeout: 15_000 },
    );
  const savedResponse = nextSave();
  await page.mouse.up();
  const saved = await savedResponse;
  expect(saved.ok(), await saved.text()).toBe(true);
  const moved = (await saved.json()).geometry;
  expect(moved).toMatchObject({ type: "rotated_bbox", w: geometry.w, h: geometry.h, angle: 35 });
  expect(moved.cx).toBeCloseTo(geometry.cx + 0.12, 2);
  expect(moved.cy).toBeCloseTo(geometry.cy + 0.1, 2);
  await expect(stage).toHaveAttribute("data-drag-kind", "none");
  const readGeometry = async () => {
    const response = await page.request.get(`/api/v1/tasks/${taskId}/annotations`, { headers });
    expect(response.ok(), await response.text()).toBe(true);
    const annotations = await response.json();
    return annotations.find((item: { id: string }) => item.id === annotation.id)?.geometry;
  };
  await expect.poll(readGeometry).toEqual(moved);
  await expect(page.getByTitle("撤销 (Ctrl+Z)", { exact: true })).toBeEnabled();
  const undoResponse = nextSave();
  await page.keyboard.press("ControlOrMeta+z");
  const undone = await undoResponse;
  expect(undone.ok(), await undone.text()).toBe(true);
  await expect.poll(readGeometry).toEqual(geometry);
  await expect(page.getByTitle("重做 (Ctrl+Shift+Z)", { exact: true })).toBeEnabled();
  const redoResponse = nextSave();
  await page.keyboard.press("ControlOrMeta+Shift+z");
  const redone = await redoResponse;
  expect(redone.ok(), await redone.text()).toBe(true);
  await expect.poll(readGeometry).toEqual(moved);
  await page.reload();
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-user-box-count", "1");
  await expect.poll(readGeometry).toEqual(moved);
  const restored = await point(moved.cx, moved.cy);
  await page.mouse.click(restored.x, restored.y);
  await page.mouse.down();
  await expect(stage).toHaveAttribute("data-drag-kind", "moveRotatedBox");
  await page.mouse.up();
  await stage.screenshot({ path: testInfo.outputPath("rotated-bbox-moved.png") });
  expect(errors).toEqual([]);
});
