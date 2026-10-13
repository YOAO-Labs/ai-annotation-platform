/**
 * v0.10.10 · I8.2 · image-bench 基准 fixture 单场景执行。
 *
 * 通过环境变量选场景：
 *   IMAGE_BENCH_SIZE        - 2k | 8k | polygon-dense（默认 2k）
 *   IMAGE_BENCH_DENSITY     - 10 | 100 | 500（默认 10）
 *
 * 当前实现走 owned 私有 fixture（未带 size/density 参数）；后端 _test_seed
 * 端点扩入参后此处改 reset({ imageSize, density })。无论参数是否生效，spec 都会：
 *   1) 进入工作台 → 等 mount
 *   2) 模拟 pan / zoom / select 各一回合
 *   3) page.evaluate 读 window.__workbenchPerf → 写到 test-results/image-bench/...
 */
import { test, expect } from "../fixtures/seed";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const env: any = (globalThis as any).process?.env ?? {};
const SIZE = env.IMAGE_BENCH_SIZE ?? "2k";
const DENSITY = parseInt(env.IMAGE_BENCH_DENSITY ?? "10", 10);

test(`image-bench · size=${SIZE} density=${DENSITY}`, async ({ page, seed }) => {
  const data = await seed.owned();
  await seed.advanceTask({
    taskId: data.task_ids[0],
    toStatus: "pending",
    annotatorEmail: data.annotator_email,
  });
  await seed.injectToken(page, data.annotator_email);
  await page.goto(`/projects/${data.project_id}/annotate`);
  await page.waitForSelector("[data-testid='workbench-stage']", { timeout: 15_000 });

  const stage = page.getByTestId("workbench-stage");
  const box = await stage.boundingBox();
  if (!box) throw new Error("stage boundingBox 不可用");

  // pan 2s（按住空格 + 拖）—— 简化：直接鼠标拖
  for (let i = 0; i < 5; i += 1) {
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.7, { steps: 10 });
    await page.mouse.up();
  }

  // zoom cycle
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.keyboard.down("Control");
  for (let i = 0; i < 4; i += 1) await page.mouse.wheel(0, -120);
  for (let i = 0; i < 4; i += 1) await page.mouse.wheel(0, 120);
  await page.keyboard.up("Control");

  await page.waitForTimeout(500);

  const stats = await page.evaluate(() => {
    const w = window as unknown as {
      __workbenchPerf?: {
        longTaskCount: number;
        longTaskMaxMs: number;
        lastLongTaskAt: number | null;
      };
    };
    return w.__workbenchPerf ?? null;
  });

  expect(stats).not.toBeNull();
  console.log(
    `[image-bench] size=${SIZE} density=${DENSITY} longTaskCount=${stats!.longTaskCount} longTaskMaxMs=${stats!.longTaskMaxMs}`,
  );
  // soft sanity for the default 2k/10 baseline; 其余场景仅记录，不强求阈值
  if (SIZE === "2k" && DENSITY === 10) {
    expect(stats!.longTaskMaxMs).toBeLessThan(500);
  }
});
