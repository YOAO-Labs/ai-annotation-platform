import type { NotificationsResponse } from "../../src/api/notifications";
import { test, expect } from "../fixtures/seed";

const API_BASE =
  (typeof globalThis !== "undefined" &&
    (globalThis as { process?: { env?: Record<string, string> } }).process?.env
      ?.PLAYWRIGHT_API_BASE) ||
  "http://127.0.0.1:8010";

test.describe("review approve loop", () => {
  test("reviewer 通过 UI approve 任务 → annotator 通知列表出现 task.approved", async ({
    page,
    seed,
    request,
  }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    const taskPath = `/api/v1/tasks/${taskId}`;

    await seed.createTaskAnnotation(taskId, data.annotator_email, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.7, h: 0.7 },
    });
    const submitToken = await seed.accessToken(data.annotator_email);
    const submitted = await request.post(`${API_BASE}${taskPath}/submit`, {
      headers: { Authorization: `Bearer ${submitToken}` },
    });
    expect(submitted.ok(), await submitted.text()).toBe(true);

    const listApprovedNotifications = async () => {
      const response = await request.get(`${API_BASE}/api/v1/notifications?limit=20`, {
        headers: { Authorization: `Bearer ${submitToken}` },
      });
      expect(response.ok(), await response.text()).toBe(true);
      const { items }: NotificationsResponse = await response.json();
      return items.filter(
        (item) =>
          item.type === "task.approved" && item.target_type === "task" && item.target_id === taskId,
      );
    };
    expect(await listApprovedNotifications()).toEqual([]);

    await seed.injectToken(page, data.reviewer_email);
    await page.goto(`/projects/${data.project_id}/review?task=${taskId}`);

    const approveBtn = page.getByTestId("review-approve");
    const retryClaim = page.getByRole("button", { name: "重试认领" });
    await expect(approveBtn.and(page.locator(":enabled")).or(retryClaim)).toBeVisible({
      timeout: 10_000,
    });
    if (await retryClaim.isVisible()) {
      await expect(approveBtn).toBeDisabled();
      await retryClaim.click();
    }
    await expect(approveBtn).toBeEnabled();

    const [approved] = await Promise.all([
      page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === `${taskPath}/review/approve` &&
          response.request().method() === "POST",
      ),
      approveBtn.click(),
    ]);
    expect(approved.status(), await approved.text()).toBe(200);
    expect(await approved.json()).toMatchObject({ status: "approved", task_id: taskId });

    const reviewerToken = await seed.accessToken(data.reviewer_email);
    const taskRes = await request.get(`${API_BASE}${taskPath}`, {
      headers: { Authorization: `Bearer ${reviewerToken}` },
    });
    expect(taskRes.ok(), await taskRes.text()).toBe(true);
    expect(await taskRes.json()).toMatchObject({ id: taskId, status: "completed" });
    await expect.poll(listApprovedNotifications).not.toEqual([]);
  });
});
