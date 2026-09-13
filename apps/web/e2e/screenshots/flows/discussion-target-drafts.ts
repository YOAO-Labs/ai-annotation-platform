/**
 * 流程录制：任务留言与标注草稿各自独立。
 *
 * 输出：.artifacts/marketing/<run>/masters/discussion-target-drafts → docs-site/public/media/discussion/target-drafts.mp4
 *
 * 准备（不进母版）：把右侧栏拖宽，让讨论面板文字在母版里可读；草稿内容本身只来自真实输入。
 *
 * 母版窗口：以「当前任务」为收件人输入“整体边界待复核” → 选中一个已保存对象，输入另一段
 * 草稿 → 只改变阅读范围（仅任务留言 ↔ 当前标注）→ 在两个收件人之间往返，各自输入框仍显示
 * 自己的原草稿 → 发送任务草稿，并在「任务留言」来源标签下读回。
 *
 * 草稿语义诚实：草稿按用户 / 项目 / 任务 / 目标分桶，只承诺同一登录会话内恢复；
 * 本流程不做刷新，也不声明刷新后仍存。发送后的评论由流程在母版窗口之外按 id 精确删除。
 */
import { expect, type Locator, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { discussionTargetKey } from "../../../src/pages/Workbench/state/discussionTypes";
import { hidePredictions, movePointerAtRefreshRate, openImageAnnotate } from "./_canvas";
import { waitForRecordingPanels } from "./_workbench-layout";
import type { DrawWindow } from "./rotated-bbox";

const TASK_DRAFT = "整体边界待复核";
const ANNOTATION_DRAFT = "轮廓与影像边界不一致";

interface TaskDiscussionItem {
  data: {
    body: string;
    task_id: string;
    annotation_id?: string | null;
    canvas_drawing?: { shapes?: unknown[] } | null;
  };
  source: string;
}

interface DiscussionPage {
  items: TaskDiscussionItem[];
}

async function apiFetch<T>(
  page: Page,
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  return page.evaluate(
    async ({ path, method, body }) => {
      const response = await fetch(path, {
        method,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("token") ?? ""}`,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      if (!response.ok) {
        throw new Error(`[discussion-target-drafts] ${path}: HTTP ${response.status} ${text}`);
      }
      return text ? JSON.parse(text) : null;
    },
    { path, method: options.method ?? "GET", body: options.body },
  ) as Promise<T>;
}

/**
 * 准备用真实拖拽把右栏拖宽（同一 Dockview 竖向分隔条，偏好由安装布局的沙箱接管）。
 * 讨论面板默认在 15% 右栏里，母版文字过窄；这一步不进母版，也不改产品默认值。
 */
export async function widenDiscussionColumn(page: Page, deltaPx = 220): Promise<Locator> {
  const discussion = page.locator('[data-workbench-panel="discussion"]');
  await discussion.waitFor({ state: "visible", timeout: 10_000 });
  const before = await discussion.boundingBox();
  if (!before) throw new Error("[discussion-target-drafts] 讨论面板没有可见边界");
  const sash = await page
    .locator("[data-workbench-workspace] .dv-sash")
    .evaluateAll((elements, boundary) => {
      const rect = elements
        .map((element) => element.getBoundingClientRect())
        .find(
          (candidate) =>
            candidate.height > candidate.width &&
            candidate.width > 0 &&
            Math.abs(candidate.left + candidate.width / 2 - boundary) < 6,
        );
      return rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null;
    }, before.x);
  if (!sash) throw new Error("[discussion-target-drafts] 右栏分隔条不存在");
  await page.mouse.move(sash.x, sash.y);
  await page.waitForTimeout(300);
  await page.mouse.down();
  await movePointerAtRefreshRate(page, sash, { x: sash.x - deltaPx, y: sash.y }, 800);
  await page.mouse.up();
  await expect
    .poll(async () => (await discussion.boundingBox())?.width ?? 0)
    .toBeGreaterThan(before.width + deltaPx * 0.6);
  return discussion;
}

/** 逐字输入并保留真实输入节奏，母版里能看清正在写的草稿。 */
async function typeIntoEditor(page: Page, editor: Locator, text: string, delayMs = 160) {
  await editor.click();
  for (const char of text) {
    await page.keyboard.insertText(char);
    await page.waitForTimeout(delayMs);
  }
}

export interface DiscussionTargetDraftsOptions {
  /** 测试体通过正式 API 预先落库的已保存对象；流程只选中它，不修改它。 */
  annotationId: string;
}

export async function runDiscussionTargetDrafts(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  options: DiscussionTargetDraftsOptions,
): Promise<DrawWindow & { evidence: Record<string, unknown> }> {
  const project = catalog.projects.image_demo;
  const task = project.tasks.annotating;
  const stage = page.getByTestId("workbench-stage");
  const reader = () => page.locator('[data-workbench-panel="discussion"]');
  const editor = () => reader().getByRole("textbox", { name: "留言" });
  const scope = () => reader().getByRole("combobox", { name: "评论阅读范围" });
  const destination = () => reader().getByRole("combobox", { name: "发送目标" });
  const taskTargetKey = discussionTargetKey({
    projectId: project.id,
    taskId: task.id,
    kind: "task",
  });
  const annotationTargetKey = discussionTargetKey({
    projectId: project.id,
    taskId: task.id,
    kind: "annotation",
    annotationId: options.annotationId,
  });

  await openImageAnnotate(page, catalog);
  await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 20_000 });
  await waitForRecordingPanels(page, [
    "canvas",
    "task-queue",
    "class-palette",
    "inspector",
    "discussion",
  ]);
  await hidePredictions(page);
  await page.getByTestId("tool-btn-select").click();
  await widenDiscussionColumn(page);
  await reader().getByRole("tab", { name: /^评论/ }).click();
  await expect(editor()).toBeVisible();
  await expect(destination()).toHaveValue(taskTargetKey);

  // ── 母版窗口 ──
  const drawStartMs = Date.now();
  await page.waitForTimeout(1200);

  await typeIntoEditor(page, editor(), TASK_DRAFT);
  await expect(editor()).toHaveText(TASK_DRAFT);
  await page.waitForTimeout(1200);

  // 选中一个已保存对象：发送目标自动跟随它，输入框切换到该目标自己的（空）草稿。
  const annotationRow = page.getByTestId(`box-list-item-${options.annotationId}`);
  await annotationRow.click();
  await expect(annotationRow).toHaveClass(/border-brand/);
  await expect(destination()).toHaveValue(annotationTargetKey);
  await expect(editor()).toBeEmpty();
  await page.waitForTimeout(900);

  await typeIntoEditor(page, editor(), ANNOTATION_DRAFT);
  await expect(editor()).toHaveText(ANNOTATION_DRAFT);
  await page.waitForTimeout(1100);

  // 只改变阅读范围：列表筛选变化，当前目标的草稿不被搬动。
  const scopeChecks: Array<{ scope: string; editorText: string }> = [];
  for (const nextScope of ["task", "annotation", "all"]) {
    await scope().selectOption(nextScope);
    await page.waitForTimeout(900);
    scopeChecks.push({ scope: nextScope, editorText: await editor().innerText() });
    expect(await editor().innerText()).toBe(ANNOTATION_DRAFT);
  }

  // 在两个收件人之间往返：每个目标仍显示自己的原草稿。
  await destination().selectOption(taskTargetKey);
  await expect(editor()).toHaveText(TASK_DRAFT);
  await page.waitForTimeout(1300);
  await destination().selectOption(annotationTargetKey);
  await expect(editor()).toHaveText(ANNOTATION_DRAFT);
  await page.waitForTimeout(1300);
  await destination().selectOption(taskTargetKey);
  await expect(editor()).toHaveText(TASK_DRAFT);
  await page.waitForTimeout(900);

  // 发送任务草稿，并在「任务留言」来源标签下读回。
  const pending = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/feedbacks",
  );
  await reader().getByRole("button", { name: "发送", exact: true }).click();
  const response = await pending;
  expect(response.ok(), await response.text()).toBe(true);
  const sent = (await response.json()) as { id: string };
  const row = reader().getByTestId("discussion-comment-row").filter({ hasText: TASK_DRAFT });
  await expect(row).toBeVisible({ timeout: 15_000 });
  await expect(row.getByTestId("discussion-source-chip")).toHaveText("任务留言");
  await page.waitForTimeout(2400);

  const drawEndMs = Date.now();

  // ── 母版窗口之外：真实回读 + 只清理本次发送的评论 ──
  const taskScope = await apiFetch<DiscussionPage>(
    page,
    `/api/v1/tasks/${task.id}/discussion/page?scope=task`,
  );
  const annotationScope = await apiFetch<DiscussionPage>(
    page,
    `/api/v1/tasks/${task.id}/discussion/page?scope=annotation`,
  );
  const readBack = taskScope.items.filter((item) => item.data.body === TASK_DRAFT);
  expect(readBack).toHaveLength(1);
  expect(readBack[0]!.data).toMatchObject({ task_id: task.id, thread_parent_id: null });
  expect(annotationScope.items).toEqual([]);
  expect(taskScope.items.some((item) => item.data.body === ANNOTATION_DRAFT)).toBe(false);
  await apiFetch(page, `/api/v1/feedbacks/${sent.id}`, { method: "DELETE" });

  const evidence = {
    drafts: {
      task: { target: "task", body: TASK_DRAFT },
      annotation: {
        target: "annotation",
        annotation_id: options.annotationId,
        body: ANNOTATION_DRAFT,
      },
    },
    scope_changes: scopeChecks,
    sent_comment: {
      id: sent.id,
      source: "feedback",
      source_chip: "任务留言",
      body: TASK_DRAFT,
      task_id: task.id,
    },
    readback: {
      task_scope_bodies: taskScope.items.map((item) => item.data.body),
      task_scope_source: taskScope.items[0]?.source ?? null,
      annotation_scope_items: annotationScope.items.length,
    },
    draft_semantics: "per-target session drafts; no reload/persistence claim",
    comment_cleanup: `deleted /feedbacks/${sent.id}`,
  };
  return { drawStartMs, drawEndMs, evidence };
}
