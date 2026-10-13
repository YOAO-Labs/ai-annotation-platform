import type { APIResponse, Page } from "@playwright/test";
import { expect, test, type SeedAPI } from "../fixtures/seed";
import { layoutCommand } from "../helpers/workbench-layout";
import { discussionTargetKey } from "../../src/pages/Workbench/state/discussionTypes";
import type { NotificationItem } from "../../src/api/notifications";
import type {
  AnnotationFeedback,
  AnnotationFeedbackThreadPage,
  CreateFeedbackPayload,
} from "../../src/api/feedbacks";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const discussion = (page: Page) => page.locator('[data-workbench-panel="discussion"]');
const editor = (page: Page) => discussion(page).getByRole("textbox", { name: "留言" });
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });

async function createIssue(
  page: Page,
  data: { project_id: string; token: string },
  taskId: string,
  title: string,
  extra: Partial<CreateFeedbackPayload> = {},
) {
  return json<AnnotationFeedback>(
    await page.request.post(`${API_BASE}/api/v1/feedbacks`, {
      headers: auth(data.token),
      data: {
        project_id: data.project_id,
        task_id: taskId,
        kind: "issue",
        anchor_type: "task",
        title,
        body: `${title}：请确认标注结果`,
        ...extra,
      },
    }),
  );
}

async function replyToIssue(page: Page, token: string, rootId: string, body: string) {
  return json<AnnotationFeedback>(
    await page.request.post(`${API_BASE}/api/v1/feedbacks/${rootId}/replies`, {
      headers: auth(token),
      data: { body },
    }),
  );
}

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function setup(page: Page, seed: SeedAPI) {
  const data = await seed.owned();
  const token = await seed.accessToken(data.admin_email);
  await json(
    await page.request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
      headers: auth(token),
      data: { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null },
    }),
  );
  expect(
    (
      await page.request.delete(
        `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
        { headers: auth(token) },
      )
    ).status(),
  ).toBe(204);
  await seed.injectToken(page, data.admin_email);
  return { ...data, token };
}

async function openTask(page: Page, projectId: string, taskId: string) {
  await page.goto(`/projects/${projectId}/annotate?task=${taskId}`);
  await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
    timeout: 20_000,
  });
  await layoutCommand(page, "标准标注布局");
  await discussion(page).getByRole("tab", { name: /^评论/ }).click();
  await expect(editor(page)).toBeVisible();
}

test.use({ viewport: { width: 1440, height: 1000 } });

async function notifications(page: Page, token: string) {
  return json<{ items: NotificationItem[]; unread: number }>(
    await page.request.get(`${API_BASE}/api/v1/notifications`, { headers: auth(token) }),
  );
}

test("讨论通知通过实时推送打开五十条之后的旧回复，并刷新已缓存对话中的新回复", async ({
  page,
  seed,
}) => {
  test.setTimeout(120_000);
  const data = await setup(page, seed);
  const reviewerToken = await seed.accessToken(data.reviewer_email);
  const root = await createIssue(page, data, data.task_ids[0], "通知分页问题");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    const received: Array<{ type?: string; target_id?: string }> = [];
    const socketReady = page.waitForEvent("websocket", {
      predicate: (socket) => new URL(socket.url()).pathname === "/ws/notifications",
    });
    page.on("websocket", (socket) => {
      if (new URL(socket.url()).pathname !== "/ws/notifications") return;
      socket.on("framereceived", ({ payload }) => {
        try {
          received.push(JSON.parse(String(payload)));
        } catch {
          /* Non-JSON frames are not events. */
        }
      });
    });
    await page.goto("/projects");
    await socketReady;
    await expect(page.getByTestId("notifications-trigger")).toBeVisible();
    const oldReply = await replyToIssue(page, reviewerToken, root.id, "通知中的最早回复");
    await expect
      .poll(() =>
        received.some(
          (event) => event.type === "feedback.reply_created" && event.target_id === root.id,
        ),
      )
      .toBe(true);
    for (let index = 0; index < 51; index++)
      await replyToIssue(page, data.token, root.id, `后续回复 ${index}`);
    const before = await notifications(page, data.token);
    expect(before.items.filter((item) => item.type === "feedback.reply_created")).toHaveLength(1);
    expect(before.items[0]).toMatchObject({
      target_id: root.id,
      read_at: null,
      payload: { reply_id: oldReply.id, source: "feedback" },
    });
    await page.getByTestId("notifications-trigger").click();
    await page.getByRole("button", { name: /^打开通知：回复了问题/ }).click();
    await expect(page).toHaveURL(new RegExp(`reply=${oldReply.id}`));
    await expect(page.getByTestId("discussion-issue-detail")).toHaveAttribute(
      "data-issue-id",
      root.id,
    );
    const oldRow = page.getByTestId(`discussion-issue-reply-${oldReply.id}`);
    await expect(oldRow).toHaveAttribute("aria-current", "true");
    await expect(oldRow).toBeVisible();
    await expect(
      page.getByTestId("discussion-issue-thread-scroll").getByTestId(/^discussion-issue-reply-/),
    ).toHaveCount(52);
    await expect
      .poll(async () => (await notifications(page, data.token)).items[0].read_at)
      .not.toBeNull();
    // Browser back retains the authenticated query cache; the new reply must
    // still be fetched when a later notification opens this same conversation.
    await page.goBack();
    await expect(page.getByTestId("notifications-trigger")).toBeVisible();
    const freshReply = await replyToIssue(page, reviewerToken, root.id, "缓存建立后才发布的新回复");
    await page.getByTestId("notifications-trigger").click();
    await expect(page.getByRole("button", { name: /^打开通知：回复了问题/ })).toHaveCount(2);
    await page
      .getByRole("button", { name: /^打开通知：回复了问题/ })
      .first()
      .click();
    await expect(page.getByTestId(`discussion-issue-reply-${freshReply.id}`)).toHaveAttribute(
      "aria-current",
      "true",
    );
    await expect(page).toHaveURL(new RegExp(`reply=${freshReply.id}`));
    expect(errors).toEqual([]);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("标注提及通知定位原评论，发送目标跟随标注，支持静音和删除后的不可用提示", async ({
  page,
  seed,
}) => {
  test.setTimeout(120_000);
  const data = await setup(page, seed);
  const reviewerToken = await seed.accessToken(data.reviewer_email);
  const admin = await json<{ id: string; name: string }>(
    await page.request.get(`${API_BASE}/api/v1/auth/me`, { headers: auth(data.token) }),
  );
  const annotation = await seed.createTaskAnnotation(data.task_ids[0], data.admin_email, {
    annotation_type: "bbox",
    tool_unit_id: "bbox",
    class_name: "car",
    geometry: { type: "bbox", x: 0.2, y: 0.2, w: 0.3, h: 0.3 },
  });
  const mention = async () =>
    json<{ id: string }>(
      await page.request.post(`${API_BASE}/api/v1/annotations/${annotation.id}/comments`, {
        headers: auth(reviewerToken),
        data: {
          body: `@${admin.name} 请检查原标注`,
          mentions: [
            { userId: admin.id, displayName: admin.name, offset: 0, length: admin.name.length + 1 },
          ],
          attachments: [],
        },
      }),
    );
  try {
    const original = await mention();
    for (let index = 0; index < 51; index++)
      await json(
        await page.request.post(`${API_BASE}/api/v1/annotations/${annotation.id}/comments`, {
          headers: auth(data.token),
          data: { body: `后续标注评论 ${index}`, mentions: [], attachments: [] },
        }),
      );
    const before = await notifications(page, data.token);
    expect(before.items).toHaveLength(1);
    expect(before.items[0]).toMatchObject({
      type: "annotation.comment_mentioned",
      target_type: "annotation_comment",
      target_id: original.id,
      payload: { source: "annotation_comment", annotation_id: annotation.id },
    });
    await page.goto("/projects");
    await page.getByTestId("notifications-trigger").click();
    await page.getByRole("button", { name: /^打开通知：在标注评论中提到了你/ }).click();
    await expect(page).toHaveURL(new RegExp(`comment=${original.id}`));
    const row = discussion(page).locator(`[data-comment-key="annotation_comment:${original.id}"]`);
    await expect(row).toHaveAttribute("aria-current", "true");
    await expect(row).toBeVisible();
    await expect(discussion(page).getByRole("combobox", { name: "评论阅读范围" })).toHaveValue(
      "annotation",
    );
    await expect(discussion(page).getByRole("combobox", { name: "发送目标" })).toHaveValue(
      discussionTargetKey({
        projectId: data.project_id,
        taskId: data.task_ids[0],
        kind: "annotation",
        annotationId: annotation.id,
      }),
    );
    await json(
      await page.request.put(`${API_BASE}/api/v1/notification-preferences`, {
        headers: auth(data.token),
        data: { type: "annotation.comment_mentioned", in_app: false },
      }),
    );
    await mention();
    expect((await notifications(page, data.token)).items).toHaveLength(1);
    await page.goBack();
    await expect(page.getByTestId("notifications-trigger")).toBeVisible();
    const sourceUrl = page.url();
    await seed.deleteTaskAnnotation(data.task_ids[0], annotation.id, data.admin_email);
    await page.getByTestId("notifications-trigger").click();
    await page.getByRole("button", { name: /^打开通知：在标注评论中提到了你/ }).click();
    await expect(page.getByRole("dialog", { name: "打开通知目标" })).toContainText(
      "标注已删除或当前账号不可见",
    );
    await expect(page).toHaveURL(sourceUrl);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("问题状态通知为审核员打开对应审核工作台并保留字段权限", async ({ page, seed }) => {
  const data = await setup(page, seed);
  const reviewerToken = await seed.accessToken(data.reviewer_email);
  try {
    const root = await createIssue(page, data, data.task_ids[0], "状态通知问题");
    await replyToIssue(page, reviewerToken, root.id, "审核参与者回复");
    await json(
      await page.request.patch(`${API_BASE}/api/v1/feedbacks/${root.id}`, {
        headers: auth(data.token),
        data: { status: "resolved" },
      }),
    );
    const rows = await notifications(page, reviewerToken);
    expect(rows.items).toHaveLength(1);
    expect(rows.items[0]).toMatchObject({
      type: "feedback.status_changed",
      target_id: root.id,
      payload: { from_status: "open", to_status: "resolved" },
    });
    await seed.injectToken(page, data.reviewer_email);
    await page.goto("/review");
    await page.getByTestId("notifications-trigger").click();
    await page.getByRole("button", { name: /^打开通知：状态 / }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${data.project_id}/review\\?`));
    const detail = page.getByTestId("discussion-issue-detail");
    await expect(detail).toHaveAttribute("data-issue-id", root.id, { timeout: 20_000 });
    await expect(detail.getByRole("button", { name: "重新打开", exact: true })).toBeVisible();
    await expect(detail.getByRole("button", { name: "删除", exact: true })).toHaveCount(0);
    await detail.getByRole("button", { name: "重新打开", exact: true }).click();
    await expect
      .poll(
        async () =>
          (
            await json<AnnotationFeedbackThreadPage>(
              await page.request.get(`${API_BASE}/api/v1/feedbacks/${root.id}/thread`, {
                headers: auth(reviewerToken),
              }),
            )
          ).root.status,
      )
      .toBe("open");
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("失效讨论地址和跨项目任务不会永久加载或先显示错误画布", async ({ page, seed }) => {
  test.setTimeout(120_000);
  const data = await setup(page, seed);
  // The foreign project below is deliberately named like the shared fixture but
  // is owned by this namespace's admin, so it must be removed before the owned
  // pre-clean: `projects.owner_id` would otherwise block the user delete with a
  // misleading residual.
  let foreignProjectId: string | null = null;
  try {
    const root = await createIssue(page, data, data.task_ids[0], "校验地址问题");
    for (const query of [
      "discussion=bogus",
      `discussion=issues&issue=${root.id}&issue=${root.id}`,
    ]) {
      await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}&${query}`);
      await expect(page.getByText("讨论链接不完整或格式无效", { exact: true })).toBeVisible({
        timeout: 20_000,
      });
      await expect(page.getByTestId("workbench-stage")).toHaveCount(0);
    }
    // The standard seed cleanup owns projects with this exact fixture name.
    const foreign = await json<{ id: string }>(
      await page.request.post(`${API_BASE}/api/v1/projects`, {
        headers: auth(data.token),
        data: {
          name: "E2E Demo Project",
          type_label: "video",
          type_key: "video-track",
          classes: ["car"],
        },
      }),
    );
    foreignProjectId = foreign.id;
    const foreignCanvasReads: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === `/api/v1/tasks/${data.task_ids[0]}/annotations`)
        foreignCanvasReads.push(request.method());
    });
    await page.goto(
      `/projects/${foreign.id}/annotate?task=${data.task_ids[0]}&discussion=issues&issue=${root.id}`,
    );
    await expect(page.getByText("讨论目标不属于当前项目", { exact: true })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByTestId("video-konva-stage")).toHaveCount(0);
    expect(foreignCanvasReads).toEqual([]);

    // Invalid history input must also preserve an already admitted canvas and
    // its send destination, even when the URL attempts a new batch and task.
    await openTask(page, data.project_id, data.task_ids[0]);
    await editor(page).fill("无效链接不能更换当前草稿目标");
    await page.getByTestId("workbench-stage").evaluate((element) => {
      element.setAttribute("data-discussion-owner-probe", "retained");
    });
    await page.evaluate(
      ({ projectId, taskId, invalidBatchId }) => {
        window.history.pushState(
          window.history.state,
          "",
          `/projects/${projectId}/annotate?task=${taskId}&batch=${invalidBatchId}&discussion=bogus`,
        );
        window.dispatchEvent(new PopStateEvent("popstate"));
      },
      { projectId: data.project_id, taskId: data.task_ids[1], invalidBatchId: foreign.id },
    );
    await expect(
      discussion(page).getByText("讨论链接不完整或格式无效", { exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
      "data-discussion-owner-probe",
      "retained",
    );
    await expect(editor(page)).toHaveText("无效链接不能更换当前草稿目标");
  } finally {
    // Remove the foreign project and prove it completed before the owned
    // pre-clean runs; page cleanup must still happen if that assertion fails.
    try {
      if (foreignProjectId) {
        const deleted = await page.request.delete(
          `${API_BASE}/api/v1/projects/${foreignProjectId}`,
          { headers: auth(data.token) },
        );
        expect(deleted.status()).toBe(204);
      }
    } finally {
      await page.close();
      await seed.owned();
    }
  }
});

test("未选标注可发送任务留言，失败保留正文，读回原生任务来源", async ({ page, seed }) => {
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await openTask(page, data.project_id, taskId);
    await expect(discussion(page).getByRole("combobox", { name: "评论阅读范围" })).toHaveValue(
      "all",
    );
    await expect(discussion(page).getByRole("button", { name: "在题图上绘制" })).toBeEnabled();
    let attempts = 0;
    await page.route("**/api/v1/feedbacks", async (route) => {
      if (route.request().method() === "POST" && attempts++ === 0) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: '{"detail":"test retry"}',
        });
      } else await route.continue();
    });
    const body = "无需选框的任务留言";
    await editor(page).fill(body);
    await discussion(page).getByRole("button", { name: "发送", exact: true }).click();
    await expect(discussion(page).getByRole("alert")).toBeVisible();
    await expect(editor(page)).toHaveText(body);
    await discussion(page).getByRole("button", { name: "重试", exact: true }).click();
    const row = discussion(page).getByTestId("discussion-comment-row").filter({ hasText: body });
    await expect(row).toBeVisible();
    await expect(row.getByTestId("discussion-source-chip")).toHaveText("任务留言");
    await expect(editor(page)).toBeEmpty();
    const stored = await json<{
      items: Array<{
        source: string;
        data: { body: string; task_id: string; thread_parent_id: string | null };
      }>;
    }>(
      await page.request.get(`${API_BASE}/api/v1/tasks/${taskId}/discussion/page?scope=task`, {
        headers: auth(data.token),
      }),
    );
    expect(stored.items).toHaveLength(1);
    expect(stored.items[0]).toMatchObject({
      source: "feedback",
      data: { body, task_id: taskId, thread_parent_id: null },
    });
    expect(errors).toEqual([]);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("混合讨论可加载旧标注评论，阅读范围与各发送目标的草稿独立", async ({ page, seed }) => {
  test.setTimeout(120_000);
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  try {
    const annotation = await seed.createTaskAnnotation(taskId, data.admin_email, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.2, y: 0.2, w: 0.3, h: 0.3 },
    });
    const oldBody = "来自真实标注的旧批注";
    const attachmentBody = "原标注附件，不依赖当前选中对象";
    const upload = await json<{ storage_key: string; upload_url: string }>(
      await page.request.post(
        `${API_BASE}/api/v1/annotations/${annotation.id}/comment-attachments/upload-init`,
        {
          headers: auth(data.token),
          data: { file_name: "discussion-proof.txt", content_type: "text/plain" },
        },
      ),
    );
    const uploaded = await page.request.put(upload.upload_url, {
      data: Buffer.from(attachmentBody),
      headers: { "Content-Type": "text/plain" },
    });
    expect(uploaded.ok()).toBe(true);
    await json(
      await page.request.post(`${API_BASE}/api/v1/annotations/${annotation.id}/comments`, {
        headers: auth(data.token),
        data: {
          body: oldBody,
          mentions: [],
          attachments: [
            {
              storageKey: upload.storage_key,
              fileName: "discussion-proof.txt",
              mimeType: "text/plain",
              size: Buffer.byteLength(attachmentBody),
            },
          ],
          canvas_drawing: { shapes: [{ type: "line", points: [0.1, 0.1, 0.4, 0.4] }] },
        },
      }),
    );
    for (let index = 0; index < 51; index++) {
      await json(
        await page.request.post(`${API_BASE}/api/v1/feedbacks`, {
          headers: auth(data.token),
          data: {
            project_id: data.project_id,
            task_id: taskId,
            kind: "comment",
            anchor_type: "task",
            body: `分页任务留言 ${index}`,
          },
        }),
      );
    }
    await openTask(page, data.project_id, taskId);
    const panel = discussion(page);
    await expect(panel.getByTestId("discussion-comment-row")).toHaveCount(50);
    await expect(panel.getByText(oldBody, { exact: true })).toHaveCount(0);
    await panel.getByTestId("comments-load-more").click();
    const oldRow = panel.getByTestId("discussion-comment-row").filter({ hasText: oldBody });
    await expect(oldRow).toBeVisible();
    await expect(oldRow.getByTestId("discussion-source-chip")).toHaveText("标注评论");
    const downloadEvent = page.waitForEvent("download");
    await oldRow.getByRole("button", { name: "下载附件 discussion-proof.txt" }).click();
    const download = await downloadEvent;
    expect(await download.failure()).toBeNull();
    expect(download.suggestedFilename()).toBe("discussion-proof.txt");
    const stream = await download.createReadStream();
    expect(stream).not.toBeNull();
    const chunks: Buffer[] = [];
    for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
    expect(Buffer.concat(chunks).toString()).toBe(attachmentBody);
    await oldRow.getByTestId("comment-annotation-chip").click();
    const scope = panel.getByRole("combobox", { name: "评论阅读范围" });
    await expect(scope).toHaveValue("all");
    const destination = panel.getByRole("combobox", { name: "发送目标" });
    await destination.selectOption(
      discussionTargetKey({ projectId: data.project_id, taskId, kind: "task" }),
    );
    await editor(page).fill("任务草稿保留");
    await destination.selectOption(
      discussionTargetKey({
        projectId: data.project_id,
        taskId,
        kind: "annotation",
        annotationId: annotation.id,
      }),
    );
    await expect(editor(page)).toBeEmpty();
    await editor(page).fill("标注草稿保留");
    await scope.selectOption("annotation");
    await expect(editor(page)).toHaveText("标注草稿保留");
    await scope.selectOption("task");
    await expect(editor(page)).toHaveText("标注草稿保留");
    await destination.selectOption(
      discussionTargetKey({ projectId: data.project_id, taskId, kind: "task" }),
    );
    await expect(editor(page)).toHaveText("任务草稿保留");
    await expect(panel.getByTestId("comment-input-disabled")).toHaveCount(0);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("任务 A 的晚响应不清空任务 B 草稿，离开工作台再返回仍保留当前草稿", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await setup(page, seed);
  const [taskA, taskB] = data.task_ids;
  let releaseResponse = () => {};
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let committed = false;
  try {
    const taskLabels = await Promise.all(
      [taskA, taskB].map(async (id) =>
        json<{ display_id: string }>(
          await page.request.get(`${API_BASE}/api/v1/tasks/${id}`, { headers: auth(data.token) }),
        ),
      ),
    );
    await page.route("**/api/v1/feedbacks", async (route) => {
      if (route.request().method() !== "POST" || route.request().postDataJSON().task_id !== taskA) {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      committed = true;
      await responseGate;
      await route.fulfill({ response });
    });
    await openTask(page, data.project_id, taskA);
    await editor(page).fill("任务 A 已发送内容");
    await discussion(page).getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => committed).toBe(true);
    const queue = page.getByRole("tabpanel", { name: "任务队列", exact: true });
    await queue.getByText(taskLabels[1].display_id, { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`task=${taskB}`));
    await expect(editor(page)).toBeEditable();
    await editor(page).fill("任务 B 未发送草稿");
    releaseResponse();
    await expect(discussion(page).getByRole("button", { name: "发送", exact: true })).toBeEnabled();
    await expect(editor(page)).toHaveText("任务 B 未发送草稿");
    await queue.getByText(taskLabels[0].display_id, { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`task=${taskA}`));
    await expect(editor(page)).toBeEmpty();
    await editor(page).fill("离开工作台仍保留的草稿");
    await page
      .getByTestId("workbench-topbar")
      .getByRole("button", { name: "返回", exact: true })
      .click();
    await expect(page.getByTestId("workbench-topbar")).toHaveCount(0);
    await page.goBack();
    await expect(editor(page)).toHaveText("离开工作台仍保留的草稿");
  } finally {
    releaseResponse();
    await page.close();
    await seed.owned();
  }
});

test("弹窗未保存笔触跨页签和工作台路由恢复，并提交到原标注", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    const annotation = await seed.createTaskAnnotation(taskId, data.admin_email, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.2, y: 0.2, w: 0.3, h: 0.3 },
    });
    await openTask(page, data.project_id, taskId);
    await page.getByTestId(`box-list-item-${annotation.id}`).click();
    const destination = discussion(page).getByRole("combobox", { name: "发送目标" });
    const annotationTarget = discussionTargetKey({
      projectId: data.project_id,
      taskId,
      kind: "annotation",
      annotationId: annotation.id,
    });
    await destination.selectOption(annotationTarget);
    const popupButton = discussion(page).getByRole("button", {
      name: /^(弹窗批注|批注 · \d+ 条)$/,
    });
    await popupButton.click();
    const popup = page.getByRole("dialog", { name: "画布批注", exact: true });
    await expect(popup).toBeVisible();
    const drawing = popup.locator('svg[viewBox="0 0 1 1"]');
    const bounds = (await drawing.boundingBox())!;
    await page.mouse.move(bounds.x + bounds.width * 0.2, bounds.y + bounds.height * 0.25);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width * 0.7, bounds.y + bounds.height * 0.65, {
      steps: 6,
    });
    await page.mouse.up();
    await expect(drawing.locator("polyline")).toHaveCount(1);
    const points = await drawing.locator("polyline").getAttribute("points");
    // Closing the modal is not the explicit Save action: its unfinished
    // composer draft must already own this stroke before presentation unmounts.
    await page.keyboard.press("Escape");
    await expect(popup).toHaveCount(0);
    await discussion(page).getByRole("tab", { name: "历史", exact: true }).click();
    await discussion(page).getByRole("tab", { name: /^评论/ }).click();
    await expect(popupButton).toContainText("1 条");
    await page
      .getByTestId("workbench-topbar")
      .getByRole("button", { name: "返回", exact: true })
      .click();
    await expect(page.getByTestId("workbench-topbar")).toHaveCount(0);
    await page.goBack();
    // Returning without a canvas selection shows the task draft; reopen the original target.
    await destination.selectOption(annotationTarget);
    await expect(popupButton).toContainText("1 条");
    await popupButton.click();
    await expect(drawing.locator("polyline")).toHaveCount(1);
    await expect(drawing.locator("polyline")).toHaveAttribute("points", points!);
    await popup.getByRole("button", { name: "保存批注", exact: true }).click();
    await editor(page).fill("关闭弹窗和离开工作台都未丢失的绘图");
    await discussion(page).getByRole("button", { name: "发送", exact: true }).click();
    const row = discussion(page).getByTestId("discussion-comment-row").filter({
      hasText: "关闭弹窗和离开工作台都未丢失的绘图",
    });
    await expect(row).toBeVisible();
    const stored = await json<{
      items: Array<{
        data: { annotation_id: string; canvas_drawing: { shapes: Array<{ points: number[] }> } };
      }>;
    }>(
      await page.request.get(`${API_BASE}/api/v1/tasks/${taskId}/discussion/page`, {
        headers: auth(data.token),
      }),
    );
    expect(stored.items).toHaveLength(1);
    expect(stored.items[0].data.annotation_id).toBe(annotation.id);
    expect(stored.items[0].data.canvas_drawing.shapes).toHaveLength(1);
    expect(stored.items[0].data.canvas_drawing.shapes[0].points).toEqual(
      points!.split(/[ ,]/).map(Number),
    );
    expect(errors).toEqual([]);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("任务问题完整读取旧回复和嵌套回复，失败重试后解决仍保留详情", async ({ page, seed }) => {
  test.setTimeout(150_000);
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    const root = await createIssue(page, data, taskId, "检查遮挡边缘");
    const first = await replyToIssue(page, data.token, root.id, "最早的复核意见");
    const child = await replyToIssue(page, data.token, first.id, "历史嵌套回复仍属于原问题");
    for (let index = 0; index < 51; index++) {
      await replyToIssue(page, data.token, root.id, `后续复核记录 ${index}`);
    }
    await openTask(page, data.project_id, taskId);
    const panel = discussion(page);
    await panel.getByRole("tab", { name: /^问题/ }).click();
    await page.getByTestId(`discussion-issue-open-${root.id}`).click();
    const detail = page.getByTestId("discussion-issue-detail");
    await expect(detail).toHaveAttribute("data-issue-id", root.id);
    await expect(detail).toContainText(root.body);
    await expect(detail.getByTestId(`discussion-issue-locate-${root.id}`)).toHaveCount(0);
    await expect(detail.locator('[data-testid^="discussion-issue-reply-"]')).toHaveCount(50);
    await expect(detail.getByText(first.body, { exact: true })).toHaveCount(0);
    let earlierAttempts = 0;
    await page.route(`**/api/v1/feedbacks/${root.id}/thread?*`, async (route) => {
      if (new URL(route.request().url()).searchParams.has("cursor") && earlierAttempts++ === 0) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: '{"detail":"test earlier replies retry"}',
        });
      } else await route.continue();
    });
    await detail.getByRole("button", { name: "加载更早回复", exact: true }).click();
    const rows = detail.locator('[data-testid^="discussion-issue-reply-"]');
    const threadError = detail.getByTestId("discussion-issue-thread-error");
    await expect(threadError).toBeVisible();
    await expect(rows).toHaveCount(50);
    await expect(detail).toContainText(root.body);
    await threadError.getByRole("button", { name: "重试", exact: true }).click();
    await expect(rows).toHaveCount(53);
    await expect(threadError).toHaveCount(0);
    expect(earlierAttempts).toBe(2);
    await expect(rows.first()).toContainText(first.body);
    await expect(rows.nth(1)).toContainText(child.body);
    await expect(detail.getByRole("button", { name: "加载更早回复", exact: true })).toHaveCount(0);

    let replyAttempts = 0;
    await page.route(`**/api/v1/feedbacks/${root.id}/replies`, async (route) => {
      if (route.request().method() === "POST" && replyAttempts++ === 0) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: '{"detail":"test reply retry"}',
        });
      } else await route.continue();
    });
    const replyBody = "已修正遮挡边缘，请再次确认";
    const replyEditor = detail.getByRole("textbox", { name: "留言" });
    await replyEditor.fill(replyBody);
    await detail.getByRole("button", { name: "发送", exact: true }).click();
    await expect(detail.getByRole("alert")).toBeVisible();
    await expect(replyEditor).toHaveText(replyBody);
    await detail.getByRole("button", { name: "重试", exact: true }).click();
    await expect(detail.getByText(replyBody, { exact: true })).toBeVisible();
    await expect(replyEditor).toBeEmpty();
    expect(replyAttempts).toBe(2);
    const readback = await json<AnnotationFeedbackThreadPage>(
      await page.request.get(`${API_BASE}/api/v1/feedbacks/${root.id}/thread?limit=200`, {
        headers: auth(data.token),
      }),
    );
    expect(readback.total).toBe(54);
    expect(readback.items.filter((item) => item.body === replyBody)).toHaveLength(1);
    expect(readback.items.find((item) => item.body === replyBody)?.thread_parent_id).toBe(root.id);

    await detail.getByRole("button", { name: "解决", exact: true }).click();
    await expect(detail).toHaveAttribute("data-issue-id", root.id);
    await expect(detail.getByRole("button", { name: "重新打开", exact: true })).toBeVisible();
    const resolved = await json<AnnotationFeedbackThreadPage>(
      await page.request.get(`${API_BASE}/api/v1/feedbacks/${root.id}/thread`, {
        headers: auth(data.token),
      }),
    );
    expect(resolved.root.status).toBe("resolved");
    await detail.getByRole("button", { name: "返回问题列表", exact: true }).click();
    await expect(panel.getByTestId(`discussion-issue-card-${root.id}`)).toHaveCount(0);
    await expect(panel.getByTestId("issue-open-count")).toHaveText("待处理 0");
    await panel.getByTestId("issue-status-resolved").click();
    await panel.getByTestId(`discussion-issue-open-${root.id}`).click();
    await detail.getByRole("button", { name: "重新打开", exact: true }).click();
    await expect(detail.getByRole("button", { name: "解决", exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("未加载且被筛掉的图钉直接打开详情，不改列表筛选", async ({ page, seed }) => {
  test.setTimeout(150_000);
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  try {
    const oldRoot = await createIssue(page, data, taskId, "较早的边缘问题", {
      anchor_type: "pixel",
      anchor_position: { x: 0.3, y: 0.4 },
    });
    await json(
      await page.request.patch(`${API_BASE}/api/v1/feedbacks/${oldRoot.id}`, {
        headers: auth(data.token),
        data: { status: "resolved" },
      }),
    );
    for (let index = 0; index < 51; index++) {
      await createIssue(page, data, taskId, `其他待处理问题 ${index}`);
    }
    await openTask(page, data.project_id, taskId);
    const panel = discussion(page);
    await panel.getByRole("tab", { name: /^问题/ }).click();
    await expect(panel.getByTestId("issue-open-count")).toHaveText("待处理 51");
    await expect(panel.getByTestId(`discussion-issue-card-${oldRoot.id}`)).toHaveCount(0);
    await expect(page.getByTestId("issue-pin-coverage")).toHaveCount(0);
    const stage = page.getByTestId("workbench-stage");
    const bounds = await stage.boundingBox();
    expect(bounds).not.toBeNull();
    const point = await stage.evaluate((element) => ({
      x:
        Number(element.getAttribute("data-media-x")) +
        Number(element.getAttribute("data-media-width")) * 0.3,
      y:
        Number(element.getAttribute("data-media-y")) +
        Number(element.getAttribute("data-media-height")) * 0.4,
    }));
    await page.mouse.click(bounds!.x + point.x, bounds!.y + point.y);
    const detail = page.getByTestId("discussion-issue-detail");
    await expect(detail).toHaveAttribute("data-issue-id", oldRoot.id);
    await expect(detail).toContainText(oldRoot.body);
    await expect(detail.getByRole("button", { name: "重新打开", exact: true })).toBeVisible();
    await detail.getByRole("button", { name: "返回问题列表", exact: true }).click();
    await expect(panel.getByTestId("issue-status-open")).toHaveAttribute("aria-pressed", "true");
    await expect(panel.getByTestId(`discussion-issue-card-${oldRoot.id}`)).toHaveCount(0);
    await expect(panel.getByTestId("issue-open-count")).toHaveText("待处理 51");
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("解决分页边界上的问题后继续下一条，返回列表保留阅读位置", async ({ page, seed }) => {
  test.setTimeout(150_000);
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  try {
    const roots: AnnotationFeedback[] = [];
    for (let index = 0; index < 52; index++) {
      roots.push(await createIssue(page, data, taskId, `顺序复核 ${index}`));
    }
    await openTask(page, data.project_id, taskId);
    const panel = discussion(page);
    await panel.getByRole("tab", { name: /^问题/ }).click();
    await expect(panel.locator('[data-testid^="discussion-issue-card-"]')).toHaveCount(50);
    const current = roots[2]; // The fiftieth item of the descending first page.
    const next = roots[1];
    const last = roots[0];
    const openCurrent = panel.getByTestId(`discussion-issue-open-${current.id}`);
    await openCurrent.scrollIntoViewIfNeeded();
    const list = panel.getByTestId("discussion-issue-list-scroll");
    const scrollTop = await list.evaluate((element) => element.scrollTop);
    expect(scrollTop).toBeGreaterThan(0);
    await openCurrent.click();
    const detail = page.getByTestId("discussion-issue-detail");
    await expect(detail).toHaveAttribute("data-issue-id", current.id);
    await detail.getByRole("button", { name: "解决", exact: true }).click();
    await expect(detail.getByRole("button", { name: "重新打开", exact: true })).toBeVisible();
    await detail.getByRole("button", { name: "下一条", exact: true }).click();
    await expect(detail).toHaveAttribute("data-issue-id", next.id);
    await detail.getByRole("button", { name: "下一条", exact: true }).click();
    await expect(detail).toHaveAttribute("data-issue-id", last.id);
    await expect(detail.getByRole("button", { name: "下一条", exact: true })).toBeDisabled();
    await detail.getByRole("button", { name: "上一条", exact: true }).click();
    await expect(detail).toHaveAttribute("data-issue-id", next.id);
    await detail.getByRole("button", { name: "返回问题列表", exact: true }).click();
    // One resolved card was removed; the list restores the nearest available
    // scroll offset instead of resetting to the top or rewinding pagination.
    await expect
      .poll(() => list.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(scrollTop / 2);
    await expect(panel.getByTestId("issue-status-open")).toHaveAttribute("aria-pressed", "true");
    await expect(panel.getByTestId("issue-open-count")).toHaveText("待处理 51");
  } finally {
    await page.close();
    await seed.owned();
  }
});

test("迟到的问题回复不清空另一问题或原问题的新草稿", async ({ page, seed }) => {
  test.setTimeout(90_000);
  const data = await setup(page, seed);
  const taskId = data.task_ids[0];
  let releaseResponse!: () => void;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let committed = false;
  try {
    const first = await createIssue(page, data, taskId, "问题 A");
    const second = await createIssue(page, data, taskId, "问题 B");
    await page.route(`**/api/v1/feedbacks/${first.id}/replies`, async (route) => {
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      committed = true;
      await responseGate;
      await route.fulfill({ response });
    });
    await openTask(page, data.project_id, taskId);
    const panel = discussion(page);
    await panel.getByRole("tab", { name: /^问题/ }).click();
    const detail = page.getByTestId("discussion-issue-detail");
    const input = detail.getByRole("textbox", { name: "留言" });
    const openIssue = async (id: string) => {
      await panel.getByTestId(`discussion-issue-open-${id}`).click();
      await expect(detail).toHaveAttribute("data-issue-id", id);
      await expect(input).toBeEditable();
    };
    const back = () => detail.getByRole("button", { name: "返回问题列表", exact: true }).click();
    await openIssue(first.id);
    await input.fill("问题 A 已提交的回复");
    await detail.getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => committed).toBe(true);
    await back();
    await openIssue(second.id);
    await input.fill("问题 B 保留的草稿");
    await back();
    await openIssue(first.id);
    await input.fill("问题 A 后续补充的新草稿");
    releaseResponse();
    await expect(detail.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
    await expect(input).toHaveText("问题 A 后续补充的新草稿");
    const readback = await json<AnnotationFeedbackThreadPage>(
      await page.request.get(`${API_BASE}/api/v1/feedbacks/${first.id}/thread`, {
        headers: auth(data.token),
      }),
    );
    expect(readback.items).toHaveLength(1);
    expect(readback.items[0]).toMatchObject({
      kind: "comment",
      thread_parent_id: first.id,
      body: "问题 A 已提交的回复",
    });
    await back();
    await openIssue(second.id);
    await expect(input).toHaveText("问题 B 保留的草稿");
  } finally {
    releaseResponse();
    await page.close();
    await seed.owned();
  }
});
