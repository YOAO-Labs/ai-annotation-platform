import { expect, test } from "../fixtures/seed";
import { panAndZoomImage } from "../helpers/image-bench-gestures";

test("image workbench smoke · standard owned fixture", async ({ page, seed }) => {
  const data = await seed.owned();
  const taskId = data.task_ids[0];
  await seed.advanceTask({ taskId, toStatus: "pending", annotatorEmail: data.annotator_email });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);

  const stage = page.getByTestId("workbench-stage");
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
  await expect(stage).toHaveAttribute("data-image-decoded-width", "64");
  await expect(stage).toHaveAttribute("data-image-decoded-height", "48");
  await expect(stage).toHaveAttribute("data-user-box-count", "0");
  await test.step("Zoom out from the tiny fixture's oversized fit before cycling", async () => {
    const fittedWidth = Number(await stage.getAttribute("data-media-width"));
    await stage.hover();
    await page.keyboard.down("Control");
    try {
      for (let i = 0; i < 4; i += 1) await page.mouse.wheel(0, 120);
      await expect
        .poll(async () => Number(await stage.getAttribute("data-media-width")))
        .toBeLessThan(fittedWidth);
    } finally {
      await page.keyboard.up("Control");
    }
  });
  await panAndZoomImage(page, stage);

  const stats = await page.evaluate(
    () =>
      (window as Window & { __workbenchPerf?: { longTaskMaxMs: number } }).__workbenchPerf ?? null,
  );
  expect(stats).not.toBeNull();
  expect(stats?.longTaskMaxMs).toBeLessThan(500);
});
