/**
 * v0.16.0 · 图片画框冒烟 + Konva 渲染基线(画布栈统一地基)。
 *
 * 为什么独立于 annotation.spec.ts:后者验证「画框 → POST /annotations 落库链路」(交互正确性),
 * 本 spec 验证「画框后 Konva 画布的真实渲染」(像素回归)——这是 v0.16.0 为后续视频迁到
 * Konva 立的**图片渲染基线**。Konva 渲染到 canvas 无 DOM 可查,konva mock(vitest)只能验
 * 交互/props,真渲染只能靠 Playwright 截图比对。视频基线 v0.16.1 起补。
 *
 * 基线易因字体/抗锯齿环境差异 flaky,故:
 *   - 固定 viewport(setViewportSize)+ 截 workbench-stage 容器(非整页);
 *   - 关动画;给 maxDiffPixelRatio 容差(与 regression.spec.ts 一致 0.01)。
 *
 * 基线文件:tests/__screenshots__/(首次自动生成,随 UI 变更 --update-snapshots)。
 */
import { test, expect } from "../fixtures/seed";

test.describe("workbench image konva smoke", { tag: "@visual" }, () => {
  test("画框后对 workbench-stage 容器截图(Konva 渲染基线)", async ({ page, seed }) => {
    const data = await seed.owned();
    // The rendering baseline must not depend on an external ML service's error banner.
    const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
    const configured = await page.request.patch(`/api/v1/projects/${data.project_id}`, {
      headers,
      data: {
        ai_enabled: false,
        ai_interactive_enabled: false,
        ml_backend_id: null,
      },
    });
    expect(configured.ok(), await configured.text()).toBe(true);
    const removed = await page.request.delete(
      `/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
      { headers },
    );
    expect(removed.status()).toBe(204);
    const enabledBackends = await page.request.get(
      `/api/v1/projects/${data.project_id}/ml-backends`,
      { headers },
    );
    expect(await enabledBackends.json()).toEqual([]);
    // seed 默认 task 无 assignee,先把 task[0] 分给 annotator(同 annotation.spec.ts)
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    await seed.injectToken(page, data.annotator_email);

    // 固定容器尺寸,降低截图 flaky
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
    await page.waitForLoadState("networkidle");

    // 选 bbox 工具
    const bboxBtn = page.getByTestId("tool-btn-box");
    await expect(bboxBtn).toBeVisible({ timeout: 10_000 });
    await bboxBtn.click();
    await expect(bboxBtn).toHaveAttribute("aria-pressed", "true");

    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toBeVisible();
    // 等媒体真正就绪再取 boundingBox：早于 ready 时 workspace 仍在初始布局过渡态,
    // stage 宽度与稳态不同, 截图基线尺寸会漂移。
    await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");

    // 画一个框(固定相对位置,保证基线稳定)
    const startX = box.x + box.width * 0.3;
    const startY = box.y + box.height * 0.3;
    const endX = box.x + box.width * 0.6;
    const endY = box.y + box.height * 0.6;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    await page.mouse.move(endX, endY, { steps: 8 });
    await page.mouse.up();

    // afterBoxCreate 默认 pick_class → 松手后弹选类别窗;选 car 完成落库,
    // 等 user-box-count=1 确保截到「画框已渲染」的稳态,而非过渡帧。
    await expect(page.getByTestId("class-picker-popover")).toBeVisible({ timeout: 10_000 });
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeHidden();
    await expect(stage).toHaveAttribute("data-user-box-count", "1", { timeout: 15_000 });
    await expect(page.getByText("AI 能力", { exact: true })).toHaveCount(0);

    // 等画布稳定,关动画
    await page.waitForTimeout(500);
    await page.addStyleTag({
      content:
        "*,*::before,*::after{animation-duration:0!important;transition-duration:0!important;}",
    });

    // 对 stage 容器截图基线(非整页),容差抗字体/抗锯齿
    await expect(stage).toHaveScreenshot("workbench-image-stage-with-box.png", {
      maxDiffPixelRatio: 0.01,
      animations: "disabled",
    });
  });
});
