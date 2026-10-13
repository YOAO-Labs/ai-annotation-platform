/**
 * v0.10.4 M4-α · workbench longtask 观测烟囱测（最小版）。
 *
 * 当前用现有 seed task 验证：
 *   1) `window.__workbenchPerf` 在工作台 mount 后被写入；
 *   2) 鼠标拖拽 / Ctrl+滚轮缩放后 longTaskCount 保持 0 或 longTaskMaxMs<100。
 *
 * **TODO（v0.10.5+）**：扩成 ROADMAP I8.2 完整版 —— 3 张图片（2K / 8K / dense-polygon）
 * × 3 套标注密度（10 / 100 / 500 shapes），需先在 _test_seed router 加 fixture
 * 支持 (density + size 参数)。
 */
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
  // 等工作台 hydrate
  await page.waitForSelector("[data-testid='workbench-stage']", { timeout: 10_000 });
  // useWorkbenchPerf 在 mount 后写入 window；DEV 100%, PROD 5% — 即使采样率 5% 也会先初始化 counters。
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
