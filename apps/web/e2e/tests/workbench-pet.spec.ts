import { expect, test } from "../fixtures/seed";

test("桌宠切换信息时保持位置，展开详情后隐藏简化气泡", async ({ page, seed }, testInfo) => {
  test.setTimeout(90_000);
  page.setDefaultTimeout(15_000);
  const data = await seed.owned();
  const taskId = data.task_ids[0];
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const projectPath = `/api/v1/projects/${data.project_id}`;
  const configured = await page.request.patch(projectPath, {
    headers,
    data: { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null },
  });
  expect(configured.ok(), await configured.text()).toBe(true);
  const detached = await page.request.delete(`${projectPath}/ml-backends/${data.ml_backend_id}`, {
    headers,
  });
  expect(detached.status()).toBe(204);
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  const annotations = [];
  for (const [index, className] of ["car", "person"].entries()) {
    annotations.push(
      await seed.createTaskAnnotation(taskId, data.annotator_email, {
        annotation_type: "bbox",
        tool_unit_id: "bbox",
        class_name: className,
        geometry: { type: "bbox", x: 0.15 + index * 0.3, y: 0.2, w: 0.2, h: 0.2 },
      }),
    );
  }
  await seed.injectToken(page, data.annotator_email);
  await seed.setPetEnabled(data.annotator_email, true);
  await page.setViewportSize({ width: 1440, height: 900 });
  // Exclude the intentional idle bob so geometry changes expose layout movement.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(() => {
    localStorage.setItem("workbench.pet.pos", JSON.stringify({ x: 950, y: 780 }));
  });
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
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
    timeout: 15_000,
  });
  const pet = page.locator("[data-pet-mood]");
  const sprite = pet.locator("[data-pet-skin]");
  const selectionCard = page.locator("[data-floating-panel]").filter({
    has: page.getByText("选中对象", { exact: true }),
  });
  await expect(sprite).toBeVisible();
  const baseline = await sprite.boundingBox();
  expect(baseline).not.toBeNull();
  const measurements = [{ state: "idle", bounds: baseline }];
  const measure = async (state: string) => {
    const bounds = await sprite.boundingBox();
    measurements.push({ state, bounds });
    expect.soft(bounds, `${state}: sprite must stay anchored`).toEqual(baseline);
  };

  for (const [index, annotation] of annotations.entries()) {
    await page.getByTestId(`box-list-item-${annotation.id}`).click({ position: { x: 12, y: 16 } });
    if (await selectionCard.isVisible())
      await selectionCard.getByRole("button", { name: "收起浮窗", exact: true }).click();
    await expect(selectionCard).toHaveCount(0);
    await expect(pet.getByText(index === 0 ? "car" : "person", { exact: true })).toBeVisible();
    await measure(`collapsed-${index}`);
    // Clicking the bubble must still expand the card after it leaves normal layout.
    await pet.getByText("▸ 点我展开", { exact: true }).click();
    await expect(selectionCard).toBeVisible();
    expect.soft(await pet.innerText(), "expanded details replace every compact message").toBe("");
    await measure(`expanded-${index}`);
  }

  await selectionCard.getByRole("button", { name: "收起浮窗", exact: true }).click();
  await page.getByTestId(`box-list-item-${annotations[0].id}`).click({
    position: { x: 12, y: 16 },
    modifiers: ["Shift"],
  });
  await expect(pet.getByText("已选 2 个", { exact: true })).toBeVisible();
  await measure("multi-selected");
  await pet.getByText("▸ 点我展开", { exact: true }).click();
  await expect(selectionCard).toBeVisible();
  expect.soft(await pet.innerText()).toBe("");
  await measure("multi-expanded");
  const panelBeforeDrag = await selectionCard.boundingBox();
  if (!baseline || !panelBeforeDrag) throw new Error("Missing pet or selection panel bounds");
  await page.mouse.move(baseline.x + 28, baseline.y + 48);
  await page.mouse.down();
  await page.mouse.move(baseline.x - 32, baseline.y + 8, { steps: 5 });
  await page.mouse.up();
  await expect
    .poll(() => sprite.boundingBox())
    .toEqual({
      ...baseline,
      x: baseline.x - 60,
      y: baseline.y - 40,
    });
  await expect
    .poll(() => selectionCard.boundingBox())
    .toEqual({
      ...panelBeforeDrag,
      x: panelBeforeDrag.x - 60,
      y: panelBeforeDrag.y - 40,
    });
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("workbench.pet.pos")!))).toEqual(
    { x: baseline.x - 60, y: baseline.y - 40 },
  );
  await selectionCard.getByRole("button", { name: "收起浮窗", exact: true }).click();
  await expect(pet.getByText("已选 2 个", { exact: true })).toBeVisible();
  expect(await sprite.boundingBox()).toEqual({
    ...baseline,
    x: baseline.x - 60,
    y: baseline.y - 40,
  });
  await page.screenshot({ path: testInfo.outputPath("pet-details.png") });
  await testInfo.attach("pet-positions", {
    body: JSON.stringify(measurements),
    contentType: "application/json",
  });
  expect(errors).toEqual([]);
});
