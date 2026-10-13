import { expect, test } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

test("secondary inference is available to annotators and absent from review workflows", async ({
  page,
  request,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const taskId = data.task_ids[0];
  const leaveWorkbench = async (email: string) => {
    await page.goto("about:blank");
    const released = await request.delete(`${API_BASE}/api/v1/tasks/${taskId}/lock`, {
      headers: { Authorization: `Bearer ${await seed.accessToken(email)}` },
    });
    expect(released.ok(), await released.text()).toBe(true);
  };
  const errors: string[] = [];
  const apiErrors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/api/v1/") && response.status() >= 400) {
      apiErrors.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  // Only capability discovery is stubbed; users, tasks, annotations and writes use the API.
  await page.route(
    /\/api\/v1\/projects\/[^/]+\/ml-backends\/[^/]+\/(setup|capabilities)$/,
    (route) =>
      route.fulfill({
        json: {
          models: [
            {
              id: "secondary-permission-test",
              display_name: "测试检测",
              task: "detection",
              is_interactive: false,
              supported_inputs: ["crop"],
              supported_geometric_outputs: ["bbox"],
            },
          ],
        },
      }),
  );
  // The base fixture already assigns this task to the annotator through its
  // batch, so create the parent annotation as the real author.
  const parent = await seed.createTaskAnnotation(taskId, data.annotator_email, {
    annotation_type: "bbox",
    tool_unit_id: "bbox",
    class_name: "car",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.7, h: 0.7 },
  });
  await seed.injectToken(page, data.annotator_email);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await page.getByTestId(`box-list-item-${parent.id}`).click();
  await expect(page.getByTestId("secondary-tool-capsule")).toBeVisible();
  await expect(page.getByTestId("selection-toggle-secondary-bar")).toBeVisible();
  await leaveWorkbench(data.annotator_email);

  // Submit through the real workflow as the effective annotator so the review
  // round freezes complete contributor evidence.  advance_task cannot be used
  // here: it leaves review evidence unknown and blocks reviewer adjustments.
  const annotatorToken = await seed.accessToken(data.annotator_email);
  const submitted = await request.post(`${API_BASE}/api/v1/tasks/${taskId}/submit`, {
    headers: { Authorization: `Bearer ${annotatorToken}` },
  });
  expect(submitted.ok(), await submitted.text()).toBe(true);

  // Administrators also lose the inference entry when they enter review mode.
  for (const email of [data.reviewer_email, data.admin_email]) {
    await seed.injectToken(page, email);
    await page.goto(`/projects/${data.project_id}/review?task=${taskId}`);
    await expect(page.getByTestId("review-approve")).toBeVisible();
    await page.getByTestId(`box-list-item-${parent.id}`).click();
    await expect(page.getByTestId("secondary-tool-capsule")).toHaveCount(0);
    await expect(page.getByTestId("secondary-toolbar")).toHaveCount(0);
    await expect(page.getByTestId("selection-toggle-secondary-bar")).toHaveCount(0);

    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toHaveAttribute("data-image-ready", "true");
    const point = await stage.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const data = (element as HTMLElement).dataset;
      return {
        x: rect.x + Number(data.mediaX) + Number(data.mediaWidth) * 0.45,
        y: rect.y + Number(data.mediaY) + Number(data.mediaHeight) * 0.45,
      };
    });
    await page.mouse.click(point.x, point.y, { button: "right" });
    await expect(page.getByRole("menuitem", { name: /^删除/ })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: /二次推理面板/ })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "工作台设置", exact: true }).click();
    const dialog = page.getByTestId("workbench-settings-dialog");
    await dialog.getByRole("textbox", { name: "搜索设置" }).fill("二次推理");
    await expect(dialog.getByTestId("setting-field-ui.secondary_bar_hidden")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await leaveWorkbench(email);
  }

  const token = await seed.accessToken(data.reviewer_email);
  const headers = { Authorization: `Bearer ${token}` };
  const annotationPath = `${API_BASE}/api/v1/tasks/${taskId}/annotations/${parent.id}`;
  for (const writeTarget of ["attributes", "geometry"]) {
    const response = await request.post(`${annotationPath}/secondary-inference`, {
      headers,
      data: { ml_backend_id: data.ml_backend_id, write_target: writeTarget },
    });
    expect(response.status(), await response.text()).toBe(403);
  }
  // Reviewers retain the existing ability to correct confirmed annotations manually.
  const updatedGeometry = { type: "bbox", x: 0.12, y: 0.1, w: 0.7, h: 0.7 };
  const edited = await request.patch(annotationPath, {
    headers,
    data: { geometry: updatedGeometry },
  });
  expect(edited.ok(), await edited.text()).toBe(true);
  expect((await edited.json()).geometry).toEqual(updatedGeometry);
  expect(errors).toEqual([]);
  expect(apiErrors).toEqual([]);
});
