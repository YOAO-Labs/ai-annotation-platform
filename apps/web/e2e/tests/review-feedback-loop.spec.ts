import { test, expect } from "../fixtures/seed";
import { json, waitForApiWrite } from "../helpers/api-response";

const API_BASE =
  (typeof globalThis !== "undefined" &&
    (globalThis as { process?: { env?: Record<string, string> } }).process?.env
      ?.PLAYWRIGHT_API_BASE) ||
  "http://127.0.0.1:8010";

test.describe("review feedback loop", () => {
  test("annotator 提交真实标注 → reviewer 通过 UI 退回并持久化原因与说明", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const note = "请将框内对象改为正确类别";

    await seed.createTaskAnnotation(taskId, data.annotator_email, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.7, h: 0.7 },
    });
    const annotatorToken = await seed.accessToken(data.annotator_email);
    const submitted = await request.post(`${API_BASE}/api/v1/tasks/${taskId}/submit`, {
      headers: { Authorization: `Bearer ${annotatorToken}` },
    });
    expect(submitted.ok(), await submitted.text()).toBe(true);

    await seed.injectToken(page, data.reviewer_email);
    await page.goto(`/projects/${data.project_id}/review?task=${taskId}`);

    const rejectBtn = page.getByTestId("review-reject");
    await expect(rejectBtn).toBeVisible({ timeout: 10_000 });
    await rejectBtn.click();

    const choiceDialog = page.getByRole("alertdialog", { name: "退回原因（1 个任务）" });
    await expect(choiceDialog).toBeVisible();
    await choiceDialog.getByRole("button", { name: "类别错误" }).click();
    await expect(choiceDialog).toBeHidden();

    const noteDialog = page.getByRole("alertdialog", { name: "补充说明" });
    await expect(noteDialog).toBeVisible();
    await noteDialog.getByRole("textbox").fill(note);
    const rejected = waitForApiWrite(page, "POST", `/tasks/${taskId}/review/reject`);
    await noteDialog.getByRole("button", { name: "确认退回" }).click();
    expect(await json(await rejected, 200)).toMatchObject({
      task_id: taskId,
      status: "rejected",
      reason_type: "wrong_label",
      reason: note,
    });
    await expect(noteDialog).toBeHidden();

    const reviewerToken = await seed.accessToken(data.reviewer_email);
    const taskRes = await request.get(`${API_BASE}/api/v1/tasks/${taskId}`, {
      headers: { Authorization: `Bearer ${reviewerToken}` },
    });
    expect(await json(taskRes, 200)).toMatchObject({
      status: "rejected",
      reject_reason_type: "wrong_label",
      reject_reason: note,
    });
  });
});
