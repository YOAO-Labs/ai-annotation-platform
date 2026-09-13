/**
 * 流程录制：项目邀请的创建、被邀请人接受与项目成员结果。
 *
 * 输出：team/project-invitation-join（母版 + 文档 MP4）。
 *
 * 前置：隔离截图库的 video_demo 项目与现有隔离账号 qa（审核员）。录制窗口前
 * 使用真实用户管理 UI 创建邀请（该面板一次性展示注册链接，属于窗口外准备），
 * 窗口内不再出现任何 token 字段。
 *
 * 录制窗口内的真实链路：
 *   1. 在「邀请记录」确认刚创建、等待被邀请人接受的邀请；
 *   2. 真实退出登录，再用被邀请账号真实 UI 登录；
 *   3. 打开邀请链接，由被邀请人本人点击「确认并加入」；
 *   4. 展示真实项目成员结果与「等待分派」的诚实空态（不自动产生激活批次）。
 *
 * 返回 { drawStartMs, drawEndMs, evidence }；finally 撤销本流程拥有的邀请，
 * 已接受的邀请则移除本次新增的项目成员关系。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";

// DEV.md 测试账号：隔离演示账号密码统一为 123456。
const INVITED_PASSWORD = "123456";

interface InvitationCreated {
  id?: string;
  invite_url: string;
  token: string;
  expires_at: string;
  project_id?: string | null;
  project_name?: string | null;
}

interface InvitationRecord {
  id: string;
  email: string;
  status: string;
}

interface ProjectMemberRecord {
  id: string;
  user_id: string;
  user_email: string;
  role: string;
}

interface InvitationAcceptance {
  project_id: string | null;
  project_name: string | null;
  project_member_role: string | null;
  next_action: "start_work" | "wait_for_allocation";
  next_action_label: string;
  responsible_person_name: string | null;
  active_batch_count: number;
}

async function token(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[project-invitation-join] 缺少隔离账号令牌");
  return value;
}

function auth(accessToken: string) {
  return { Authorization: `Bearer ${accessToken}` };
}

/** 只撤销本流程邮箱的待接受邀请，保留其它历史邀请不变。 */
async function revokeOwnedPendingInvitations(
  page: Page,
  accessToken: string,
  email: string,
): Promise<string[]> {
  const response = await page.request.get("/api/v1/invitations", {
    headers: auth(accessToken),
    params: { status: "pending", scope: "all" },
  });
  if (!response.ok()) {
    throw new Error(
      `[project-invitation-join] 读取邀请记录失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const rows = (await response.json()) as InvitationRecord[];
  const revoked: string[] = [];
  for (const row of rows.filter((item) => item.email === email)) {
    const deleted = await page.request.delete(`/api/v1/invitations/${row.id}`, {
      headers: auth(accessToken),
    });
    if (!deleted.ok() && deleted.status() !== 404) {
      throw new Error(
        `[project-invitation-join] 撤销残留邀请失败: HTTP ${deleted.status()} ${await deleted.text()}`,
      );
    }
    revoked.push(row.id);
  }
  return revoked;
}

async function readMembers(
  page: Page,
  accessToken: string,
  projectId: string,
): Promise<ProjectMemberRecord[]> {
  const response = await page.request.get(`/api/v1/projects/${projectId}/members`, {
    headers: auth(accessToken),
  });
  if (!response.ok()) {
    throw new Error(
      `[project-invitation-join] 读取成员失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as ProjectMemberRecord[];
}

/** 移除本次新增的项目成员关系，保证同一隔离库可重复录制。 */
async function removeMembership(
  page: Page,
  accessToken: string,
  projectId: string,
  userId: string,
): Promise<boolean> {
  const members = await readMembers(page, accessToken, projectId);
  const owned = members.filter((member) => member.user_id === userId);
  for (const member of owned) {
    const deleted = await page.request.delete(
      `/api/v1/projects/${projectId}/members/${member.id}`,
      { headers: auth(accessToken) },
    );
    if (!deleted.ok() && deleted.status() !== 404) {
      throw new Error(
        `[project-invitation-join] 移除成员失败: HTTP ${deleted.status()} ${await deleted.text()}`,
      );
    }
  }
  return owned.length > 0;
}

async function logoutFromTopBar(page: Page): Promise<void> {
  await page.getByTitle("退出登录").click();
  const offlinePrompt = page.getByTestId("logout-offline-prompt");
  await Promise.race([
    page.waitForURL(/\/login/, { timeout: 15_000 }),
    offlinePrompt.waitFor({ state: "visible", timeout: 15_000 }),
  ]).catch(() => undefined);
  if (await offlinePrompt.count()) {
    // 保留本机待同步记录后退出：不丢弃隔离账号的本地队列。
    await page.getByTestId("logout-keep-queue").click();
    await page.waitForURL(/\/login/, { timeout: 15_000 });
  }
}

export async function runProjectInvitationJoin(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onInvitationCreated: (invitation: { id: string; email: string }) => void,
): Promise<{ drawStartMs: number; drawEndMs: number; evidence: Record<string, unknown> }> {
  const project = catalog.projects.video_demo;
  const invited = catalog.users.reviewer;
  const accessToken = await token(page);
  let invitation: InvitationCreated | null = null;
  let accepted = false;
  const cleanupErrors: string[] = [];
  let result:
    | { drawStartMs: number; drawEndMs: number; evidence: Record<string, unknown> }
    | undefined;
  let actionError: unknown;

  try {
    // 窗口外准备：先清掉上次中断留下的待接受邀请与成员关系。
    const revokedResiduals = await revokeOwnedPendingInvitations(page, accessToken, invited.email);
    const removedResidualMembership = await removeMembership(
      page,
      accessToken,
      project.id,
      invited.id,
    );

    await page.goto("/users");
    await page.getByRole("button", { name: "邀请成员", exact: true }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: "邀请新成员" });
    await expect(dialog).toBeVisible({ timeout: 20_000 });
    await dialog.getByPlaceholder("newuser@your-org.com").fill(invited.email);
    await dialog.locator('input[list="invite-project-options"]').fill(project.name);
    await dialog.locator("select").selectOption("reviewer");
    await page.waitForTimeout(900);
    const created = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/v1/users/invite",
      { timeout: 20_000 },
    );
    await dialog.getByRole("button", { name: "生成邀请链接", exact: true }).click();
    const createdResponse = await created;
    expect(createdResponse.ok(), await createdResponse.text()).toBe(true);
    invitation = (await createdResponse.json()) as InvitationCreated;
    if (!invitation.id) throw new Error("[project-invitation-join] 邀请编号缺失");
    onInvitationCreated({ id: invitation.id, email: invited.email });
    // 成功面板仅此一次明文展示注册链接：在录制窗口外立即关闭，链接不进入成片。
    await dialog.getByRole("button", { name: "完成", exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 10_000 });
    const inviteToken =
      new URL(invitation.invite_url).searchParams.get("token") ?? invitation.token;

    const drawStartMs = Date.now();

    // 1. 邀请记录：只展示邮箱、角色、项目与待接受状态，不含任何 token 字段。
    await page.getByRole("button", { name: "邀请记录", exact: true }).click();
    const invitationRow = page.getByRole("row").filter({ hasText: invited.email }).first();
    await expect(invitationRow).toBeVisible({ timeout: 20_000 });
    await expect(invitationRow).toContainText("待接受");
    await expect(invitationRow).toContainText("审核员");
    await expect(invitationRow).toContainText(project.name);
    await page.waitForTimeout(2_400);

    // 2. 真实退出登录，再以被邀请账号完成真实 UI 登录。
    await logoutFromTopBar(page);
    const emailInput = page.getByPlaceholder("输入账号或邮箱");
    await expect(emailInput).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(800);
    await emailInput.fill(invited.email);
    await page.waitForTimeout(500);
    await page.getByPlaceholder("••••••••").fill(INVITED_PASSWORD);
    await page.waitForTimeout(600);
    const loginResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/v1/auth/login" &&
        response.ok(),
      { timeout: 20_000 },
    );
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await loginResponse;
    await page.waitForURL(/\/dashboard/, { timeout: 20_000 });
    await page.waitForTimeout(1_600);

    // 3. 打开邀请链接，由被邀请人本人确认加入；页面不得出现 token 字段。
    await page.goto(`/register?token=${inviteToken}`);
    await expect(page.getByRole("heading", { name: "确认加入项目" })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByText(`项目：${project.name}`)).toBeVisible();
    await expect(page.getByText("审核员", { exact: true })).toBeVisible();
    expect(
      await page.locator("body").innerText(),
      "邀请页面不得渲染一次性注册链接或 token",
    ).not.toContain(inviteToken);
    await page.waitForTimeout(2_000);
    const acceptResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/v1/auth/invitations/accept",
      { timeout: 20_000 },
    );
    await page.getByRole("button", { name: "确认并加入", exact: true }).click();
    const acceptedResponse = await acceptResponse;
    expect(acceptedResponse.ok(), await acceptedResponse.text()).toBe(true);
    accepted = true;
    const acceptance = (await acceptedResponse.json()) as { acceptance?: InvitationAcceptance };

    // 4. 真实成员结果与「等待分派」空态。
    await expect(page.getByRole("heading", { name: "邀请已完成" })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText(`你已加入项目「${project.name}」。`)).toBeVisible();
    await expect(page.getByText("等待分派")).toBeVisible();
    await expect(page.getByRole("button", { name: "打开项目", exact: true })).toBeVisible();
    await page.waitForTimeout(2_800);
    const drawEndMs = Date.now();

    // 窗口后（成片裁掉）：真实导航 + API 读回验证新增的项目成员关系。
    await page.getByRole("button", { name: "打开项目", exact: true }).click();
    await page.waitForURL(new RegExp(`/projects/${project.id}/`), { timeout: 20_000 });
    await expect(page.locator("[data-workbench-workspace]")).toBeVisible({ timeout: 20_000 });
    const membership = (await readMembers(page, accessToken, project.id)).find(
      (member) => member.user_id === invited.id,
    );
    expect(membership?.role, "接受邀请后必须有真实项目成员关系").toBe("reviewer");
    expect(acceptance.acceptance?.next_action).toBe("wait_for_allocation");
    expect(acceptance.acceptance?.active_batch_count).toBe(0);

    result = {
      drawStartMs,
      drawEndMs,
      evidence: {
        invitation: {
          id: invitation.id,
          email: invited.email,
          role: "reviewer",
          projectId: project.id,
          projectName: project.name,
        },
        residualCleanup: { revokedResiduals, removedResidualMembership },
        acceptance: acceptance.acceptance ?? null,
        membership: { memberId: membership.id, role: membership.role },
        tokenVisibleInPage: false,
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    if (invitation?.id) {
      if (!accepted) {
        try {
          const response = await page.request.delete(`/api/v1/invitations/${invitation.id}`, {
            headers: auth(accessToken),
          });
          if (!response.ok() && response.status() !== 404) {
            cleanupErrors.push(`撤销邀请失败: HTTP ${response.status()}`);
          }
        } catch (error) {
          cleanupErrors.push(String(error));
        }
      }
      if (accepted) {
        try {
          await removeMembership(page, accessToken, project.id, invited.id);
        } catch (error) {
          cleanupErrors.push(String(error));
        }
      }
    }
    try {
      const leftovers = (await readMembers(page, accessToken, project.id)).filter(
        (member) => member.user_id === invited.id,
      );
      if (leftovers.length > 0) cleanupErrors.push("残留项目成员关系");
    } catch (error) {
      cleanupErrors.push(String(error));
    }
  }
  if (actionError) throw actionError;
  if (cleanupErrors.length > 0) {
    throw new Error(`[project-invitation-join] 清理失败: ${cleanupErrors.join("; ")}`);
  }
  if (!result) throw new Error("[project-invitation-join] 未生成验收结果");
  return result;
}
