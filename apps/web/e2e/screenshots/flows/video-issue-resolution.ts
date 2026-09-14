/**
 * 流程录制：视频问题（Issue）的阅读、定位、回复、解决与连续处理。
 *
 * 输出：discussion/video-issue-resolution（母版 + 文档 MP4）。
 *
 * 前置：隔离截图库的 video_demo / tracking 真实视频任务。两条问题由正式
 * feedbacks API 在录制窗口前创建：较新的目标问题锚定 F4 左侧公交车，较早的
 * 待处理问题锚定 F8 中间卡车（「下一条」按创建时间取更早的一条）。
 *
 * 录制窗口内的真实链路：
 *   1. 停留在另一源帧打开问题详情 → 阅读不改变当前帧；
 *   2. 点击「定位」→ 等待真实目标帧渲染完成：data-status=ready、已绘制帧与
 *      帧号一致、媒体画布像素真实变化、锚点图钉像素出现；
 *   3. 回复并「解决」，保留真实 PATCH 结果；
 *   4. 继续阅读已解决详情，再用「下一条」打开下一条未解决问题。
 *
 * 返回 { drawStartMs, drawEndMs, evidence }；两条问题夹具在 finally 中按 id
 * 通过正式 DELETE 精确清理。
 */
import { expect, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";

// F4 左侧公交车 / F8 中间卡车：来自 catalog 的已复核录制锚点。
const READING_FRAME = 0;
const TARGET_FRAME = 4;
const NEXT_ISSUE_FRAME = 8;
const TARGET_ANCHOR = { x: 0.197, y: 0.72 };
const NEXT_ISSUE_ANCHOR = { x: 0.596, y: 0.6 };

const TARGET_TITLE = "左公交边界待复核";
const TARGET_BODY = "定位到该帧后核对左侧公交车边界，确认遮挡处是否需要回退重标。";
const NEXT_TITLE = "中间卡车类别待确认";
const NEXT_BODY = "确认中间卡车类别与可见轮廓，必要时统一为 truck。";
const REPLY_BODY = "已按定位帧核对：目标为左侧公交车，边界按可见轮廓修正，可以解决。";

interface FeedbackIssue {
  id: string;
  kind: string;
  status: string;
  title: string | null;
  body: string;
  task_id: string;
  thread_parent_id: string | null;
  anchor_position: { x: number; y: number; frame?: number } | null;
  resolved_at: string | null;
}

type FrameRegion = readonly [number, number];

const MEDIA_REGIONS: FrameRegion[] = [
  [0.2, 0.35],
  [0.5, 0.35],
  [0.8, 0.35],
  [0.2, 0.6],
  [0.5, 0.6],
  [0.8, 0.6],
  [0.35, 0.8],
  [0.65, 0.8],
];

async function token(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[video-issue-resolution] 缺少隔离账号令牌");
  return value;
}

function auth(accessToken: string) {
  return { Authorization: `Bearer ${accessToken}` };
}

async function createIssue(
  page: Page,
  accessToken: string,
  payload: {
    projectId: string;
    taskId: string;
    title: string;
    body: string;
    anchor: { x: number; y: number };
    frame: number;
  },
): Promise<FeedbackIssue> {
  const response = await page.request.post("/api/v1/feedbacks", {
    headers: auth(accessToken),
    data: {
      kind: "issue",
      anchor_type: "pixel",
      anchor_position: { ...payload.anchor, frame: payload.frame },
      severity: "warn",
      project_id: payload.projectId,
      task_id: payload.taskId,
      title: payload.title,
      body: payload.body,
    },
  });
  if (!response.ok()) {
    throw new Error(
      `[video-issue-resolution] 创建问题失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as FeedbackIssue;
}

async function deleteIssue(page: Page, accessToken: string, id: string): Promise<void> {
  const response = await page.request.delete(`/api/v1/feedbacks/${id}`, {
    headers: auth(accessToken),
  });
  if (!response.ok() && response.status() !== 404) {
    throw new Error(
      `[video-issue-resolution] 删除问题失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
}

async function listIssues(
  page: Page,
  accessToken: string,
  projectId: string,
  taskId: string,
): Promise<FeedbackIssue[]> {
  const response = await page.request.get("/api/v1/feedbacks", {
    headers: auth(accessToken),
    params: { project_id: projectId, task_id: taskId, kind: "issue", limit: 50 },
  });
  if (!response.ok()) {
    throw new Error(
      `[video-issue-resolution] 读取问题失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const body = (await response.json()) as { items?: unknown };
  if (!Array.isArray(body.items)) {
    throw new Error("[video-issue-resolution] 问题列表返回结构异常");
  }
  return body.items as FeedbackIssue[];
}

/** 采样真实媒体层（第 1 个 canvas）像素，用于证明目标帧已经被绘制。 */
async function mediaSignature(page: Page): Promise<number[]> {
  return page.getByTestId("video-konva-stage").evaluate((element, regions) => {
    const canvas = element.querySelector<HTMLCanvasElement>(".konvajs-content > canvas");
    if (!canvas) throw new Error("视频媒体画布缺失");
    const context = canvas.getContext("2d");
    if (!context) throw new Error("视频媒体画布上下文缺失");
    return regions.flatMap(([x, y]) => [
      ...context.getImageData(
        Math.min(canvas.width - 1, Math.round(canvas.width * x)),
        Math.min(canvas.height - 1, Math.round(canvas.height * y)),
        1,
        1,
      ).data,
    ]);
  }, MEDIA_REGIONS);
}

/** 采样覆盖层画布在锚点位置的像素；目标帧上的问题图钉为琥珀色。 */
async function issuePinSamples(page: Page, anchor: { x: number; y: number }): Promise<number[][]> {
  return page.getByTestId("video-konva-stage").evaluate((element, point) => {
    const media = {
      x: Number(element.getAttribute("data-media-x")),
      y: Number(element.getAttribute("data-media-y")),
      w: Number(element.getAttribute("data-media-width")),
      h: Number(element.getAttribute("data-media-height")),
    };
    if (!Object.values(media).every(Number.isFinite) || media.w <= 0 || media.h <= 0) {
      throw new Error("视频媒体变换不可用");
    }
    return Array.from(element.querySelectorAll<HTMLCanvasElement>(".konvajs-content > canvas"))
      .slice(1)
      .map((canvas) => {
        const scaleX = canvas.width / canvas.clientWidth;
        const scaleY = canvas.height / canvas.clientHeight;
        // 图钉固定 8px 屏幕半径；偏移 4px 采样，避开字形与描边。
        const px = Math.round((media.x + media.w * point.x + 4) * scaleX);
        const py = Math.round((media.y + media.h * point.y) * scaleY);
        const context = canvas.getContext("2d");
        if (!context) throw new Error("覆盖层画布上下文缺失");
        return [...context.getImageData(px, py, 1, 1).data];
      });
  }, anchor);
}

function isIssuePinPixel([r, g, b, alpha]: number[]): boolean {
  return alpha! > 240 && r! > 140 && g! > 70 && b! < 150;
}

/** 记录 issue-frame-navigation 的真实状态迁移，避免竞态下漏看 preparing。 */
async function observeIssueNavigation(page: Page): Promise<void> {
  await page.evaluate(() => {
    const entries: Array<{ status: string; frame: string | null }> = [];
    const record = () => {
      const node = document.querySelector('[data-testid="issue-frame-navigation"]');
      const entry = {
        status: node?.getAttribute("data-status") ?? "idle",
        frame: node?.getAttribute("data-frame-index") ?? null,
      };
      if (JSON.stringify(entry) !== JSON.stringify(entries.at(-1))) entries.push(entry);
    };
    const observer = new MutationObserver(record);
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["data-status", "data-frame-index"],
    });
    Object.defineProperty(window, "__issueNavigationObservation", {
      value: { entries, observer },
      configurable: true,
    });
    record();
  });
}

async function finishIssueNavigationObservation(
  page: Page,
): Promise<Array<{ status: string; frame: string | null }>> {
  return page.evaluate(() => {
    const observation = (
      window as unknown as {
        __issueNavigationObservation: {
          entries: Array<{ status: string; frame: string | null }>;
          observer: MutationObserver;
        };
      }
    ).__issueNavigationObservation;
    observation.observer.disconnect();
    return observation.entries;
  });
}

async function revealFab(page: Page): Promise<void> {
  // marketing 项目 viewport 为 null：直接读窗口内尺寸。
  const size = (await page.evaluate(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }))) as { width: number; height: number };
  await page.mouse.move(size.width - 12, size.height - 12);
}

export async function runVideoIssueResolution(
  page: Page,
  catalog: ScreenshotSeedCatalog,
  onIssueCreated: (issue: { id: string; title: string }) => void,
): Promise<{ drawStartMs: number; drawEndMs: number; evidence: Record<string, unknown> }> {
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  const stage = page.getByTestId("video-konva-stage");
  const panel = page.locator('[data-workbench-panel="discussion"]');
  const detail = page.getByTestId("discussion-issue-detail");
  const accessToken = await token(page);
  const created: FeedbackIssue[] = [];
  const cleanupErrors: string[] = [];
  let result:
    | { drawStartMs: number; drawEndMs: number; evidence: Record<string, unknown> }
    | undefined;
  let actionError: unknown;

  try {
    // 录制窗口前：用正式 API 创建两条按创建时间排序的问题（较早的一条作为
    // 「下一条」目标），随后在窗口内只通过真实 UI 阅读、定位与决策。
    const nextIssue = await createIssue(page, accessToken, {
      projectId: project.id,
      taskId: task.id,
      title: NEXT_TITLE,
      body: NEXT_BODY,
      anchor: NEXT_ISSUE_ANCHOR,
      frame: NEXT_ISSUE_FRAME,
    });
    created.push(nextIssue);
    onIssueCreated({ id: nextIssue.id, title: nextIssue.title ?? NEXT_TITLE });
    const targetIssue = await createIssue(page, accessToken, {
      projectId: project.id,
      taskId: task.id,
      title: TARGET_TITLE,
      body: TARGET_BODY,
      anchor: TARGET_ANCHOR,
      frame: TARGET_FRAME,
    });
    created.push(targetIssue);
    onIssueCreated({ id: targetIssue.id, title: targetIssue.title ?? TARGET_TITLE });

    await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
    await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 20_000 });
    await expect(stage).toHaveAttribute("data-video-frame-index", String(READING_FRAME), {
      timeout: 20_000,
    });

    const drawStartMs = Date.now();

    // 1. 在另一源帧打开已存在的问题：阅读详情不改变当前帧。
    await revealFab(page);
    await page.getByTestId("issue-fab").click();
    await expect(panel.getByRole("tab", { name: /^问题/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(panel.getByTestId(`discussion-issue-card-${targetIssue.id}`)).toBeVisible({
      timeout: 10_000,
    });
    await page.waitForTimeout(1950);
    await panel.getByTestId(`discussion-issue-open-${targetIssue.id}`).click();
    await expect(detail).toHaveAttribute("data-issue-id", targetIssue.id, { timeout: 10_000 });
    await expect(detail.getByTestId(`discussion-issue-root-${targetIssue.id}`)).toContainText(
      TARGET_BODY,
    );
    await page.waitForTimeout(2350);
    const frameAfterReading = await stage.getAttribute("data-video-frame-index");
    expect(frameAfterReading, "阅读问题不得改变当前源帧").toBe(String(READING_FRAME));
    const readingSignature = await mediaSignature(page);

    // 2. 定位：等待真实目标帧渲染完成，而不是只看帧号变化。
    await observeIssueNavigation(page);
    await detail.getByTestId(`discussion-issue-locate-${targetIssue.id}`).click();
    const navigation = page.getByTestId("issue-frame-navigation");
    await expect(navigation).toHaveAttribute("data-status", "ready", { timeout: 20_000 });
    await expect(navigation).toHaveAttribute("data-frame-index", String(TARGET_FRAME));
    await expect(stage).toHaveAttribute("data-video-frame-index", String(TARGET_FRAME));
    const renderedFrameIndex = await stage.getAttribute("data-video-painted-frame-index");
    const targetSignature = await mediaSignature(page);
    const changedRegions = MEDIA_REGIONS.filter((_, index) =>
      [0, 1, 2].some(
        (channel) =>
          Math.abs(
            (readingSignature[index * 4 + channel] ?? 0) -
              (targetSignature[index * 4 + channel] ?? 0),
          ) > 8,
      ),
    );
    expect(
      changedRegions.length,
      "目标帧必须由真实像素变化证明已绘制，而不是仅帧号变化",
    ).toBeGreaterThan(0);
    await expect
      .poll(async () => (await issuePinSamples(page, TARGET_ANCHOR)).some(isIssuePinPixel), {
        timeout: 10_000,
      })
      .toBe(true);
    const pinSamples = await issuePinSamples(page, TARGET_ANCHOR);
    const navigationTransitions = await finishIssueNavigationObservation(page);
    expect(navigationTransitions.map((entry) => entry.status)).toContain("preparing");
    expect(navigationTransitions.at(-1)).toEqual({ status: "ready", frame: String(TARGET_FRAME) });
    await page.waitForTimeout(2350); // 让目标帧、图钉与状态提示停留可读

    // 3. 回复并解决，保留真实写入结果。
    const editor = detail.getByRole("textbox", { name: "留言" });
    await editor.fill(REPLY_BODY);
    const replyCreated = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === `/api/v1/feedbacks/${targetIssue.id}/replies`,
      { timeout: 15_000 },
    );
    await detail.getByRole("button", { name: "发送", exact: true }).click();
    const replyResponse = await replyCreated;
    expect(replyResponse.ok(), await replyResponse.text()).toBe(true);
    const reply = (await replyResponse.json()) as FeedbackIssue;
    await expect(detail.getByTestId(`discussion-issue-reply-${reply.id}`)).toContainText(
      REPLY_BODY,
    );
    await page.waitForTimeout(1950);

    const resolved = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        new URL(response.url()).pathname === `/api/v1/feedbacks/${targetIssue.id}`,
      { timeout: 15_000 },
    );
    await detail.getByRole("button", { name: "解决", exact: true }).click();
    const resolvedResponse = await resolved;
    expect(resolvedResponse.ok(), await resolvedResponse.text()).toBe(true);
    await expect(
      detail.getByTestId(`discussion-issue-root-${targetIssue.id}`).getByText("已解决", {
        exact: true,
      }),
    ).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(2550); // 继续阅读已解决的完整详情

    // 4. 打开下一条未解决问题，保持连续处理。
    await detail.getByTestId(`discussion-issue-next-${targetIssue.id}`).click();
    await expect(detail).toHaveAttribute("data-issue-id", nextIssue.id, { timeout: 15_000 });
    await expect(
      detail.getByTestId(`discussion-issue-root-${nextIssue.id}`).getByText("未解决", {
        exact: true,
      }),
    ).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(2550);

    const drawEndMs = Date.now();

    // 真实读回：目标问题已解决且带回复，下一条问题仍未解决。
    // 回复以 kind=comment 落库，不出现在 kind=issue 的列表里：父子链以创建响应为准。
    const issues = await listIssues(page, accessToken, project.id, task.id);
    const storedTarget = issues.find((issue) => issue.id === targetIssue.id);
    const storedNext = issues.find((issue) => issue.id === nextIssue.id);
    expect(storedTarget?.status, "目标问题必须真实落库为已解决").toBe("resolved");
    expect(storedTarget?.resolved_at, "已解决时间戳必须落库").toBeTruthy();
    expect(reply.thread_parent_id, "回复必须挂在目标问题线程下").toBe(targetIssue.id);
    expect(storedNext?.status, "下一条问题必须仍未解决").toBe("open");

    result = {
      drawStartMs,
      drawEndMs,
      evidence: {
        readingFrame: Number(frameAfterReading),
        targetFrame: TARGET_FRAME,
        banner: "在另一源帧阅读问题详情，帧保持不变后才执行定位",
        navigation: {
          transitions: navigationTransitions,
          paintedFrameIndex: renderedFrameIndex,
          changedRegions: changedRegions.length,
          pinSamples,
        },
        targetIssue: { id: targetIssue.id, title: TARGET_TITLE, status: storedTarget?.status },
        reply: { id: reply.id, body: REPLY_BODY },
        nextIssue: { id: nextIssue.id, title: NEXT_TITLE, status: storedNext?.status },
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    for (const issue of [...created].reverse()) {
      try {
        await deleteIssue(page, accessToken, issue.id);
      } catch (error) {
        cleanupErrors.push(String(error));
      }
    }
    try {
      const remaining = await listIssues(page, accessToken, project.id, task.id);
      const leftovers = remaining.filter((issue) => created.some((item) => item.id === issue.id));
      if (leftovers.length > 0) {
        cleanupErrors.push(`残留问题: ${leftovers.map((issue) => issue.id).join(", ")}`);
      }
    } catch (error) {
      cleanupErrors.push(String(error));
    }
  }
  if (actionError) throw actionError;
  if (cleanupErrors.length > 0) {
    throw new Error(`[video-issue-resolution] 问题夹具清理失败: ${cleanupErrors.join("; ")}`);
  }
  if (!result) throw new Error("[video-issue-resolution] 未生成验收结果");
  return result;
}
