/**
 * v0.16.1 · 视频底图渲染冒烟 + 新旧栈像素对照(画布栈统一 epic)。
 *
 * 验证目标(ADR-0041 决策 A/B):实验 flag(experiment.videoKonva / localStorage
 * `video.experimental.konva`)开启后,视频底图经 Konva.Image 上屏,与旧 SVG 栈在
 * 「暂停指定帧」下逐帧像素一致(容差内)。基线固定到暂停帧而非播放中抓帧,避免解码时序 flaky。
 *
 * ⚠️ 暂 skip:默认测试种子(POST /__test/seed/reset,见 apps/api/app/api/v1/_test_seed.py)
 *    只建图片项目,没有视频任务,本 spec 无可导航的视频任务。待测试种子补上视频任务后,
 *    去掉 skip 并 `--update-snapshots` 生成基线即可激活。在此先固化对照流程作为可执行规格。
 *
 * 手动对照流程(开发态,无需种子):
 *   1. 打开任一视频任务工作台;
 *   2. 设置面板「实验特性」开「视频 Konva 渲染栈」(或 URL 加 ?videoKonva=1),刷新;
 *   3. 暂停到固定帧,目测底图/缩放/平移与旧栈一致;关 flag 刷新回旧栈复核。
 */
import { test, expect } from "../fixtures/seed";

const VIDEO_KONVA_STORAGE_KEY = "video.experimental.konva";

test.describe("workbench video media konva smoke", { tag: "@visual" }, () => {
  // TODO(v0.16.x): 测试种子补视频任务后去掉 skip 并生成基线。
  test.skip("Konva 视频底图在暂停帧与旧栈像素一致", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.injectToken(page, data.annotator_email);

    // 开实验 flag(localStorage 粘性,刷新后生效)。
    await page.addInitScript((key) => {
      window.localStorage.setItem(key, "1");
    }, VIDEO_KONVA_STORAGE_KEY);

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    const stage = page.getByTestId("video-konva-stage");
    await expect(stage).toBeVisible({ timeout: 10_000 });

    // 等首帧解码上屏 + 关动画,降低截图 flaky。
    await page.waitForTimeout(800);
    await page.addStyleTag({
      content:
        "*,*::before,*::after{animation-duration:0!important;transition-duration:0!important;}",
    });

    await expect(stage).toHaveScreenshot("workbench-video-konva-frame0.png", {
      maxDiffPixelRatio: 0.01,
      animations: "disabled",
    });
  });
});
