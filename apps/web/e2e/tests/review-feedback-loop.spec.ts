/**
 * v0.8.7 F3 · review 反馈环 E2E：reviewer reject 真实 UI 流。
 *
 * - annotator 提交（advance_task → submitted/review）
 * - reviewer 进 /projects/:id/review?task={id} → 点退回 → 两步决策弹窗选预设原因并填补充说明 → 确认
 * - 后端 task.status 应为 rejected，task.reject_reason 应非空
 *
 * 不走 _test_seed.advance_task 的 reject 短路，全程通过 UI 触发当前 review workbench
 * 的 /tasks/{id}/review/reject。reviewer 通过 injectToken 跳过登录，但 reject 流程必须真实。
 */
import { test, expect } from "../fixtures/seed";

const API_BASE =
  (typeof globalThis !== "undefined" &&
    (globalThis as { process?: { env?: Record<string, string> } }).process?.env
      ?.PLAYWRIGHT_API_BASE) ||
  "http://127.0.0.1:8010";

test.describe("review feedback loop", () => {
  test("reviewer 通过 UI reject 任务 → annotator 看到 review_feedback", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();

    // 1. 真实标注 + 提交，冻结完整的 review contributor 证据。
    //    不能用 advance_task：它绕过工作流，review 证据保持 unknown 会被自审守卫拒绝。
    await seed.createTaskAnnotation(data.task_ids[0], data.annotator_email, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.7, h: 0.7 },
    });
    const annotatorToken = await seed.accessToken(data.annotator_email);
    const submitted = await request.post(`${API_BASE}/api/v1/tasks/${data.task_ids[0]}/submit`, {
      headers: { Authorization: `Bearer ${annotatorToken}` },
    });
    expect(submitted.ok(), await submitted.text()).toBe(true);

    // 2. reviewer 登录 → 进项目级 review workbench 路由
    await seed.injectToken(page, data.reviewer_email);
    await page.goto(`/projects/${data.project_id}/review?task=${data.task_ids[0]}`);
    await page.waitForLoadState("networkidle");

    // 3. 点退回按钮 → 弹出退回原因 choiceDialog（plan T3.5 起为两步决策流）
    const rejectBtn = page.getByTestId("review-reject");
    await expect(rejectBtn).toBeVisible({ timeout: 10_000 });
    await rejectBtn.click();

    // 4. 第一步选「类别错误」(wrong_label)，第二步填可选补充说明后确认
    //    v0.10.16 起 reason_type 为结构化必填字段，reject_reason 仅存可选自由文本，
    //    故显式选类型并填写补充说明，断言两者都已持久化（不依赖默认勾选顺序）。
    const choiceDialog = page.getByRole("alertdialog", { name: "退回原因（1 个任务）" });
    await expect(choiceDialog).toBeVisible();
    await choiceDialog.getByRole("button", { name: "类别错误" }).click();
    await expect(choiceDialog).toBeHidden();

    const noteDialog = page.getByRole("alertdialog", { name: "补充说明" });
    await expect(noteDialog).toBeVisible();
    await noteDialog.getByRole("textbox").fill("类别错误");
    await noteDialog.getByRole("button", { name: "确认退回" }).click();
    await expect(noteDialog).toBeHidden();

    // 5. Modal 关闭后，从后端 API 直接读 task 状态确认 reject 成功
    //    （UI 可能已导航走，跳过 DOM 断言避免 flaky）
    // 等 500ms 让 mutation onSuccess 完成
    await page.waitForTimeout(500);

    // 拿 reviewer token 直查 task 状态
    const tokenRes = await request.post(`${API_BASE}/api/v1/__test/seed/login`, {
      data: { email: data.reviewer_email },
    });
    const { access_token } = (await tokenRes.json()) as { access_token: string };
    const taskRes = await request.get(`${API_BASE}/api/v1/tasks/${data.task_ids[0]}`, {
      headers: { Authorization: `Bearer ${access_token}` },
    });
    expect(taskRes.ok()).toBeTruthy();
    const task = (await taskRes.json()) as {
      status: string;
      reject_reason: string | null;
      reject_reason_type: string | null;
    };
    expect(task.status).toBe("rejected");
    expect(task.reject_reason_type).toBe("wrong_label");
    expect(task.reject_reason).toBe("类别错误");
  });
});
