/**
 * 流程录制：通过真实 @ 选择器提及隔离的项目成员，发送任务留言，并作为该成员从通知返回原评论。
 *
 * 该流程证明两件事：
 * 1. 提及来自真实 UserPicker —— 编辑器里留下 data-mention-uid 提及 chip，服务端存回
 *    mentions[]（而不是手写 `@名字` 文本）；
 * 2. 收件人是一个隔离账号：捕获窗口切换身份后，通知中心“任务留言提到了你”把工作台
 *    带回原留言并突出显示；drawEnd 之后再用一个真正隔离的浏览器上下文复用同一条通知
 *    做跨会话核对。
 *
 * 录制器按窗口采集画面，因此成员的界面必须在同一个窗口内取得；切换身份复用截图夹具的
 * 测试登录注入（清空存储后写入成员 token），不共享管理员的 localStorage。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { discussionTargetKey } from "../../../src/pages/Workbench/state/discussionTypes";
import { applyScreenshotTheme } from "../environment";
import type { DrawWindow } from "./rotated-bbox";

const BODY_SUFFIX = "请复核第 3 题的车辆轮廓边界是否完整。";
const MENTION_NOTIFICATION = /^打开通知：在任务留言中提到了你$/;
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3001";

interface ProjectMemberRow {
  user_id: string;
  user_name: string;
  user_email: string;
}

interface Mention {
  userId: string;
  displayName: string;
  offset: number;
  length: number;
}

interface PostedComment {
  id: string;
  body: string;
  mentions: Mention[];
  task_id: string | null;
  thread_parent_id: string | null;
}

interface NotificationItem {
  id: string;
  type: string;
  target_id: string | null;
  read_at: string | null;
}

interface SeedLogin {
  token: string;
  user: unknown;
}

export interface DiscussionMentionReturnResult extends DrawWindow {
  evidence: {
    commentId: string;
    body: string;
    mention: Mention;
    pickerChipName: string;
    memberContextIsolated: boolean;
    highlightedCommentKey: string;
    notificationReadAt: string | null;
  };
}

const discussionPanel = (page: Page) => page.locator('[data-workbench-panel="discussion"]');
const editor = (page: Page) => discussionPanel(page).getByRole("textbox", { name: "留言" });
const notificationBell = (page: Page) =>
  page.getByRole("button", { name: /^通知，(?:\d+ 条未读|未读数暂不可用)$/ });
const commentRow = (page: Page, commentId: string) =>
  discussionPanel(page).locator(`[data-comment-key="feedback:${commentId}"]`);

function auth(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, Connection: "close" };
}

/** 同时接受页面请求（APIResponse）与录制响应（Response）的最小只读契约。 */
interface JsonReadable {
  ok(): boolean;
  status(): number;
  text(): Promise<string>;
  json(): Promise<unknown>;
}

async function json<T>(response: JsonReadable, action: string): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `[discussion-mention-return] ${action}失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as T;
}

async function localToken(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[discussion-mention-return] 缺少登录令牌");
  return value;
}

/** 与截图夹具一致的测试登录：换取目标账号 token 与用户档案。 */
async function seedLogin(page: Page, email: string): Promise<SeedLogin> {
  const response = await page.request.post("/api/v1/__test/seed/login", {
    headers: { Connection: "close" },
    data: { email },
  });
  const body = await json<{ access_token: string; user: unknown }>(response, "测试登录");
  return { token: body.access_token, user: body.user };
}

/** 只用目标账号自己的存储建立会话，不复用上一个账号的任何状态。 */
async function signInWithStorage(page: Page, login: SeedLogin): Promise<void> {
  await page.evaluate(({ token, user }) => {
    localStorage.setItem("token", token);
    localStorage.setItem("auth-storage", JSON.stringify({ state: { token, user }, version: 0 }));
  }, login);
}

async function notifications(page: Page, token: string): Promise<NotificationItem[]> {
  const body = await json<{ items: NotificationItem[] }>(
    await page.request.get("/api/v1/notifications", { headers: auth(token) }),
    "读取通知",
  );
  return body.items;
}

export async function runDiscussionMentionReturn(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<DiscussionMentionReturnResult> {
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  const member = catalog.users.annotator;
  const adminToken = await localToken(page);
  const memberLogin = await seedLogin(page, member.email);
  if (memberLogin.token === adminToken) {
    throw new Error("[discussion-mention-return] 被提及成员与录制账号不能是同一个会话");
  }

  const members = await json<ProjectMemberRow[]>(
    await page.request.get(`/api/v1/projects/${project.id}/members`, {
      headers: auth(adminToken),
    }),
    "读取项目成员",
  );
  const memberRow = members.find((row) => row.user_email === member.email);
  if (!memberRow) {
    throw new Error("[discussion-mention-return] 项目成员列表中缺少隔离标注员");
  }

  let commentId: string | null = null;
  let result: DiscussionMentionReturnResult | undefined;
  let actionError: unknown;
  let cleanupError: unknown;
  try {
    await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true", {
      timeout: 20_000,
    });
    await discussionPanel(page).getByRole("tab", { name: /^评论/ }).click();
    await expect(editor(page)).toBeVisible({ timeout: 15_000 });
    // 没有选中标注时发送目标就是任务留言；提及走这个目标。
    await expect(discussionPanel(page).getByRole("combobox", { name: "发送目标" })).toHaveValue(
      discussionTargetKey({ projectId: project.id, taskId: task.id, kind: "task" }),
      { timeout: 10_000 },
    );

    const drawStartMs = Date.now();
    await page.waitForTimeout(2616);

    // 1) 真实 @ 选择器：输入 @ 触发成员浮层，选中隔离成员后编辑器插入提及 chip。
    await editor(page).click();
    await page.keyboard.type("@");
    const picker = page.getByRole("listbox", { name: "选择用户" });
    await expect(picker).toBeVisible({ timeout: 10_000 });
    await page.keyboard.insertText(memberRow.user_name);
    const option = picker.getByRole("option").filter({ hasText: memberRow.user_name });
    await expect(option).toHaveCount(1, { timeout: 10_000 });
    await page.waitForTimeout(2316);
    await option.click();
    const chip = editor(page).locator(`[data-mention-uid="${memberRow.user_id}"]`);
    await expect(chip).toHaveText(`@${memberRow.user_name}`, { timeout: 10_000 });
    const pickerChipName = (await chip.textContent()) ?? ``;
    // 提及 chip 序列化时自带一个尾随空格，这里不再补前导空格。
    await page.keyboard.insertText(BODY_SUFFIX);

    // 2) 发送真实任务留言，并读回服务端存储的提及对象与混合讨论来源。
    const postedResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/v1/feedbacks" &&
        response.ok(),
      { timeout: 20_000 },
    );
    await discussionPanel(page).getByRole("button", { name: "发送", exact: true }).click();
    const posted = await json<PostedComment>(await postedResponse, "发送任务留言");
    commentId = posted.id;
    const expectedMention: Mention = {
      userId: memberRow.user_id,
      displayName: memberRow.user_name,
      offset: 0,
      length: memberRow.user_name.length + 1,
    };
    // chip 与正文之间由富文本序列化插入空白（普通空格或 NBSP）：归一后再比对。
    const normalizedBody = posted.body.replace(/\u00a0/g, " ");
    expect(normalizedBody).toBe(`@${memberRow.user_name} ${BODY_SUFFIX}`);
    expect(posted.mentions).toEqual([expectedMention]);
    expect(posted.thread_parent_id).toBeNull();
    const feed = await json<{
      items: Array<{ source: string; data: { id: string; body: string } }>;
    }>(
      await page.request.get(`/api/v1/tasks/${task.id}/discussion/page?scope=task`, {
        headers: auth(adminToken),
      }),
      "读回任务讨论",
    );
    expect(
      feed.items.some((item) => item.source === "feedback" && item.data.id === commentId),
    ).toBe(true);
    await expect(
      discussionPanel(page).getByTestId("discussion-comment-row").filter({ hasText: BODY_SUFFIX }),
    ).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(3316);

    // 3) 切换到该成员自己的会话，从通知中心打开“任务留言提到了你”。
    await page.evaluate(() => {
      localStorage.clear();
      sessionStorage.clear();
    });
    await signInWithStorage(page, memberLogin);
    await applyScreenshotTheme(page, "dark");
    await page.goto("/projects", { waitUntil: "domcontentloaded" });
    await notificationBell(page).waitFor({ state: "visible", timeout: 20_000 });
    await page.waitForTimeout(2416);
    await notificationBell(page).click();
    const mentionNotification = page.getByRole("button", { name: MENTION_NOTIFICATION });
    await expect(mentionNotification).toHaveCount(1, { timeout: 15_000 });
    await page.waitForTimeout(2616);
    await mentionNotification.click();
    await expect(page).toHaveURL(new RegExp(`task_comment=${commentId}`), { timeout: 20_000 });
    const highlighted = commentRow(page, commentId);
    await expect(highlighted).toHaveAttribute("aria-current", "true", { timeout: 20_000 });
    await expect(highlighted).toContainText(BODY_SUFFIX);
    await page.waitForTimeout(3816);
    const drawEndMs = Date.now();

    // 4) drawEnd 之外：真正隔离的浏览器上下文复用同一条通知做跨会话核对。
    const browser = page.context().browser();
    if (!browser) throw new Error("[discussion-mention-return] 缺少浏览器实例");
    const memberContext = await browser.newContext({
      baseURL: BASE_URL,
      viewport: page.viewportSize() ?? undefined,
    });
    let memberContextIsolated = false;
    let highlightedCommentKey = "";
    try {
      const memberPage = await memberContext.newPage();
      // 先到应用 origin（about:blank 上 localStorage 访问被拒），再确认零会话。
      await memberPage.goto(BASE_URL, { waitUntil: "domcontentloaded" });
      memberContextIsolated =
        (await memberPage.evaluate(() => localStorage.getItem("token"))) === null;
      if (!memberContextIsolated) {
        throw new Error("[discussion-mention-return] 隔离上下文不应带有上一个账号的会话");
      }
      await signInWithStorage(memberPage, memberLogin);
      await memberPage.goto("/projects", { waitUntil: "domcontentloaded" });
      await notificationBell(memberPage).click();
      const isolatedNotification = memberPage.getByRole("button", { name: MENTION_NOTIFICATION });
      await expect(isolatedNotification).toHaveCount(1, { timeout: 20_000 });
      await isolatedNotification.click();
      await expect(memberPage).toHaveURL(new RegExp(`task_comment=${commentId}`), {
        timeout: 20_000,
      });
      const isolatedRow = commentRow(memberPage, commentId);
      await expect(isolatedRow).toHaveAttribute("aria-current", "true", { timeout: 20_000 });
      highlightedCommentKey = (await isolatedRow.getAttribute("data-comment-key")) ?? "";
      await expect(memberPage).toHaveURL(new RegExp(`task=${task.id}`));
    } finally {
      await memberContext.close();
    }

    const mentions = await notifications(page, memberLogin.token);
    const mentionNotifications = mentions.filter(
      (item) => item.type === "feedback.comment_mentioned" && item.target_id === commentId,
    );
    expect(mentionNotifications).toHaveLength(1);
    expect(mentionNotifications[0].read_at).not.toBeNull();

    result = {
      drawStartMs,
      drawEndMs,
      evidence: {
        commentId,
        body: posted.body,
        mention: expectedMention,
        pickerChipName,
        memberContextIsolated,
        highlightedCommentKey,
        notificationReadAt: mentionNotifications[0].read_at,
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    // 清理失败不吞掉主错误：记录后继续，最后与 actionError 一并判断。
    if (commentId) {
      try {
        const response = await page.request.delete(`/api/v1/feedbacks/${commentId}`, {
          headers: auth(adminToken),
        });
        if (!response.ok() && response.status() !== 404) {
          console.warn(`[discussion-mention-return] 删除任务留言失败: HTTP ${response.status()}`);
        }
      } catch (error) {
        cleanupError = cleanupError ?? error;
      }
    }
    let mentionDeletions: NotificationItem[] = [];
    try {
      mentionDeletions = await notifications(page, memberLogin.token);
    } catch {
      // 预算尾部清理尽力而为：通知残留由下一次运行开头的撤销步骤兜底。
      cleanupError = cleanupError ?? new Error("读取提及通知超时，跳过通知清理");
    }
    for (const item of mentionDeletions) {
      if (item.type !== "feedback.comment_mentioned") continue;
      if (commentId && item.target_id !== commentId) continue;
      const response = await page.request.delete(`/api/v1/notifications/${item.id}`, {
        headers: auth(memberLogin.token),
      });
      // 清理失败不吞掉主错误：记录后继续，最后统一抛出。
      if (!response.ok() && response.status() !== 404) {
        console.warn(`[discussion-mention-return] 删除提及通知失败: HTTP ${response.status()}`);
      }
    }
  }
  if (actionError) throw actionError;
  if (cleanupError) throw cleanupError;
  if (!result) throw new Error("[discussion-mention-return] 未生成验收结果");
  return result;
}
