/**
 * v0.23.5 · WS-B/C · E2E · mask 编辑会话状态机与 guard (A1/A7/P1)。
 *
 * 验收 §1「离开 dirty session 必须经过 guard」+ §1「无变化不物化 held keyframe」:
 *  - 按 M 进 mask 但尚未涂抹时, Enter 不触发 POST (canCommitMask 要求 dirty);
 *  - 涂抹后 dirty, Enter 触发一次 POST;
 *  - 涂抹后切工具 (离开 dirty session) 会弹 toast「有未保存的 Mask 稿件」。
 *
 * 注: A1 (迟到 GET 覆盖) 与 A7 (单飞去重) 的核心逻辑在 useMaskEditorSession 单测已覆盖,
 * 这里补浏览器层的 guard UI 与 Enter 守卫闭环。
 */
import { test, expect } from "../fixtures/seed";

test.describe("mask session guard (v0.23.5 WS-B/C)", () => {
  test("未涂抹时 Enter 不提交; 涂抹后 Enter 提交一次", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    await page.keyboard.press("m");
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible({ timeout: 10_000 });

    // 未涂抹: Enter 不应产生 POST。
    const noPrematurePost = page
      .waitForResponse(
        (resp) =>
          /\/api\/v1\/(annotations|tasks\/[^/]+\/annotations)/.test(resp.url()) &&
          resp.request().method() === "POST",
        { timeout: 3_000 },
      )
      .catch(() => null);
    await page.keyboard.press("Enter");
    expect(await noPrematurePost).toBeNull();

    // 涂抹一笔 → dirty。
    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");
    await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.4);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.55, box.y + box.height * 0.55, { steps: 10 });
    await page.mouse.up();
    await expect(page.getByTestId("mask-tool-capsule").getByRole("status")).toHaveAttribute(
      "aria-label",
      "未保存",
    );

    // 第一次 Enter 只打开类别选择，第二次 Enter 才确认提交。
    let commitPostCount = 0;
    page.on("request", (request) => {
      if (
        /\/api\/v1\/(annotations|tasks\/[^/]+\/annotations)/.test(request.url()) &&
        request.method() === "POST"
      )
        commitPostCount += 1;
    });
    const commitPost = page.waitForResponse(
      (resp) =>
        /\/api\/v1\/(annotations|tasks\/[^/]+\/annotations)/.test(resp.url()) &&
        resp.request().method() === "POST" &&
        resp.status() < 400,
      { timeout: 10_000 },
    );
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("class-picker-popover")).toBeVisible();
    expect(commitPostCount).toBe(0);
    await page.keyboard.press("Enter");
    const resp = await commitPost;
    expect(resp.status()).toBeLessThan(400);
    await page.waitForTimeout(500);
    expect(commitPostCount).toBe(1);
  });

  test("涂抹后切工具离开 dirty session 弹未保存提示", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate`);
    await page.waitForLoadState("networkidle");

    await page.keyboard.press("m");
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible({ timeout: 10_000 });

    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("workbench-stage boundingBox 不可用");
    await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.4);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5, { steps: 10 });
    await page.mouse.up();
    await expect(page.getByTestId("mask-tool-capsule").getByRole("status")).toHaveAttribute(
      "aria-label",
      "未保存",
    );

    // 应用内决策对话框选择「继续编辑」= continue，必须恢复旧工具与 Buffer。
    const bboxBtn = page.getByTestId("tool-btn-box");
    await expect(bboxBtn).toBeVisible();
    await bboxBtn.click();
    const leaveDialog = page.getByRole("alertdialog");
    await expect(leaveDialog).toBeVisible({ timeout: 5_000 });
    await leaveDialog.getByRole("button", { name: "继续编辑", exact: true }).click();
    await expect(page.getByText("有未保存的 Mask 稿件").first()).toBeVisible({ timeout: 5_000 });
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible();
    await expect(page.getByTestId("mask-tool-capsule").getByRole("status")).toHaveAttribute(
      "aria-label",
      "未保存",
    );
  });
});
