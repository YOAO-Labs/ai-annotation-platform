import { test, expect } from "../fixtures/seed";

test("workbench perf · __workbenchPerf 在 image 工作台 mount 后存在", async ({ page, seed }) => {
  const data = await seed.owned();
  await seed.advanceTask({
    taskId: data.task_ids[0],
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate`);
  await page.waitForSelector("[data-testid='workbench-stage']", { timeout: 10_000 });
  const stats = await page.evaluate(() => {
    return (
      (
        window as unknown as {
          __workbenchPerf?: {
            longTaskCount: number;
            longTaskMaxMs: number;
            lastLongTaskAt: number | null;
          };
        }
      ).__workbenchPerf ?? null
    );
  });
  expect(stats).not.toBeNull();
  expect(typeof stats!.longTaskCount).toBe("number");
  expect(typeof stats!.longTaskMaxMs).toBe("number");
});
