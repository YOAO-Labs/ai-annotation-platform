/**
 * 流程录制：带评论的视频轨迹与单帧标注，在已知两帧之间移动后点击可见对象的评论角标。
 *
 * 轨迹（F0 → F8 关键帧）、单帧标注和三位作者的评论全部在录制窗口前通过正式 API 准备；
 * 窗口内只保留真实交互：展开时间轴、用上一帧 / 下一帧在两个已知帧之间移动、点击当前帧
 * 可见对象角标，并在讨论面板核对只出现该对象的评论。角标数字来自
 * `/tasks/{id}/discussion/annotation-counts` 的评论总数（含已读与本人评论），不是未读数。
 */
import { expect, type APIResponse, type Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { discussionTargetKey } from "../../../src/pages/Workbench/state/discussionTypes";
import { recordingAnchor } from "./_canvas";
import type { DrawWindow } from "./rotated-bbox";

const FRAME_START = 0;
const FRAME_END = 8;
const TRACK_ID = "trk_marketing_comment_badges";

interface CreatedAnnotation {
  id: string;
  task_id: string;
  geometry: Record<string, unknown>;
}

interface CreatedComment {
  id: string;
  annotation_id: string;
  body: string;
}

interface OwnedComment {
  id: string;
  token: string;
}

export interface VideoCommentBadgesResult extends DrawWindow {
  evidence: {
    counts: Record<string, number>;
    globalTaskCommentCount: number;
    scopedTrackCommentCount: number;
    trackBadgeLabel: string;
    singleBadgeLabel: string;
    frames: { start: number; end: number };
    discussionScope: string;
    discussionTarget: string;
    trackCommentIds: string[];
    singleCommentId: string;
  };
}

const badge = (page: Page, annotationId: string) =>
  page.locator(`[data-testid="annotation-comment-badge"][data-annotation-id="${annotationId}"]`);
const discussionPanel = (page: Page) => page.locator('[data-workbench-panel="discussion"]');

function auth(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, Connection: "close" };
}

async function json<T>(response: APIResponse, action: string): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `[video-comment-badges] ${action}失败: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  return (await response.json()) as T;
}

async function localToken(page: Page): Promise<string> {
  const value = await page.evaluate(() => localStorage.getItem("token"));
  if (!value) throw new Error("[video-comment-badges] 缺少登录令牌");
  return value;
}

/** 复用截图夹具的测试登录端点，为多作者评论换取目标账号 token。 */
async function seedToken(page: Page, email: string): Promise<string> {
  const response = await page.request.post("/api/v1/__test/seed/login", {
    headers: { Connection: "close" },
    data: { email },
  });
  const body = await json<{ access_token: string }>(response, "测试登录");
  return body.access_token;
}

function normalizedBbox(bbox: [number, number, number, number]) {
  const [x1, y1, x2, y2] = bbox;
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

export async function runVideoCommentBadges(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<VideoCommentBadgesResult> {
  const project = catalog.projects.video_demo;
  const task = project.tasks.tracking;
  const trackStart = recordingAnchor(
    catalog,
    "video_demo",
    "tracking",
    "front_truck_f0",
    FRAME_START,
  );
  const trackEnd = recordingAnchor(catalog, "video_demo", "tracking", "front_truck_f8", FRAME_END);
  const singleFrame = recordingAnchor(
    catalog,
    "video_demo",
    "tracking",
    "left_bus_f0",
    FRAME_START,
  );
  if (trackStart.label !== "truck" || trackEnd.label !== "truck" || singleFrame.label !== "bus") {
    throw new Error("[video-comment-badges] 录制锚点与预期目标不一致");
  }

  const adminToken = await localToken(page);
  const ownerToken = await seedToken(page, catalog.users.project_admin.email);
  const annotationIds: string[] = [];
  const comments: OwnedComment[] = [];

  const createComment = async (annotationId: string, token: string, body: string) => {
    const response = await page.request.post(`/api/v1/annotations/${annotationId}/comments`, {
      headers: auth(token),
      data: { body, mentions: [], attachments: [] },
    });
    const comment = await json<CreatedComment>(response, "创建标注评论");
    comments.push({ id: comment.id, token });
    return comment;
  };

  let result: VideoCommentBadgesResult | undefined;
  let actionError: unknown;
  let cleanupError: unknown;
  try {
    const trackGeometry = {
      type: "video_track_bbox",
      track_id: TRACK_ID,
      keyframes: [
        {
          frame_index: FRAME_START,
          bbox: normalizedBbox(trackStart.bbox),
          source: "manual",
          occluded: false,
        },
        {
          frame_index: FRAME_END,
          bbox: normalizedBbox(trackEnd.bbox),
          source: "manual",
          occluded: false,
        },
      ],
      outside: [],
    };
    const singleGeometry = {
      type: "video_bbox",
      frame_index: FRAME_START,
      ...normalizedBbox(singleFrame.bbox),
    };
    const track = await json<CreatedAnnotation>(
      await page.request.post(`/api/v1/tasks/${task.id}/annotations`, {
        headers: auth(adminToken),
        data: {
          annotation_type: "video_track_bbox",
          tool_unit_id: "bbox",
          class_name: trackStart.label,
          geometry: trackGeometry,
        },
      }),
      "创建轨迹标注",
    );
    annotationIds.push(track.id);
    expect(track.geometry).toMatchObject(trackGeometry);
    const single = await json<CreatedAnnotation>(
      await page.request.post(`/api/v1/tasks/${task.id}/annotations`, {
        headers: auth(adminToken),
        data: {
          annotation_type: "video_bbox",
          tool_unit_id: "bbox",
          class_name: singleFrame.label,
          geometry: singleGeometry,
        },
      }),
      "创建单帧标注",
    );
    annotationIds.push(single.id);
    expect(single.geometry).toEqual(singleGeometry);
    const savedAnnotations = await json<CreatedAnnotation[]>(
      await page.request.get(`/api/v1/tasks/${task.id}/annotations`, {
        headers: auth(adminToken),
      }),
      "读取标注几何",
    );
    expect(savedAnnotations.find(({ id }) => id === track.id)).toMatchObject({
      id: track.id,
      task_id: task.id,
      geometry: trackGeometry,
    });
    expect(savedAnnotations.find(({ id }) => id === single.id)).toMatchObject({
      id: single.id,
      task_id: task.id,
      geometry: singleGeometry,
    });

    // 两位作者的评论：轨迹两条、单帧一条。角标展示评论总数，而不是当前账号的未读数。
    const trackCommentOwner = await createComment(
      track.id,
      ownerToken,
      "F8 的卡车框请保持整车范围，不要只框住货箱。",
    );
    const trackCommentAdmin = await createComment(
      track.id,
      adminToken,
      "已按整车范围核对，F0–F8 的轨迹都对齐。",
    );
    const singleComment = await createComment(
      single.id,
      ownerToken,
      "左侧公交车单帧框确认通过，不需要延续到后续帧。",
    );
    const storedTrackComments = await json<CreatedComment[]>(
      await page.request.get(`/api/v1/annotations/${track.id}/comments`, {
        headers: auth(adminToken),
      }),
      "读取轨迹评论",
    );
    expect(storedTrackComments).toHaveLength(2);
    expect(storedTrackComments.find(({ id }) => id === trackCommentOwner.id)).toMatchObject(
      trackCommentOwner,
    );
    expect(storedTrackComments.find(({ id }) => id === trackCommentAdmin.id)).toMatchObject(
      trackCommentAdmin,
    );
    const storedSingleComments = await json<CreatedComment[]>(
      await page.request.get(`/api/v1/annotations/${single.id}/comments`, {
        headers: auth(adminToken),
      }),
      "读取单帧评论",
    );
    expect(storedSingleComments).toHaveLength(1);
    expect(storedSingleComments[0]).toMatchObject(singleComment);

    const counts = await json<{ counts: Record<string, number> }>(
      await page.request.get(`/api/v1/tasks/${task.id}/discussion/annotation-counts`, {
        headers: auth(adminToken),
      }),
      "读取评论计数",
    );
    expect(counts.counts[track.id]).toBe(2);
    expect(counts.counts[single.id]).toBe(1);
    const globalTaskCommentCount = Object.values(counts.counts).reduce(
      (total, count) => total + count,
      0,
    );
    expect(globalTaskCommentCount).toBe(3);

    await page.goto(`/projects/${project.id}/annotate?task=${task.id}`);
    const stage = page.getByTestId("video-konva-stage");
    await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 20_000 });
    await expect(stage).toHaveAttribute("data-video-frame-index", String(FRAME_START), {
      timeout: 20_000,
    });
    await page.waitForFunction(() => {
      const video = document.querySelector("video");
      return video instanceof HTMLVideoElement && video.readyState >= 2 && video.videoWidth > 0;
    });

    // 角标只统计真实评论；先等两个对象各自的计数落到位再开始录制窗口。
    const trackBadgeLabel = `标注 ${track.id} 有 2 条评论`;
    const singleBadgeLabel = `标注 ${single.id} 有 1 条评论`;
    await expect(badge(page, track.id)).toHaveAttribute("aria-label", trackBadgeLabel, {
      timeout: 20_000,
    });
    await expect(badge(page, single.id)).toHaveAttribute("aria-label", singleBadgeLabel, {
      timeout: 20_000,
    });

    const drawStartMs = Date.now();
    await page.waitForTimeout(1700);
    await page.getByTestId("video-timeline-toggle").click();
    await page
      .getByTestId("video-timeline-bottom-bar")
      .waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForTimeout(1400);

    // F0 → F8：逐帧前进，轨迹角标跟随当前帧对象。
    const nextFrame = page.getByRole("button", { name: "下一帧", exact: true });
    for (let frame = FRAME_START + 1; frame <= FRAME_END; frame += 1) {
      await nextFrame.click();
      await expect(stage).toHaveAttribute("data-video-frame-index", String(frame), {
        timeout: 10_000,
      });
      await page.waitForTimeout(560);
    }
    // 单帧标注只属于 F0：离开该帧后对象和角标一起消失，轨迹角标仍在。
    await expect(badge(page, single.id)).toHaveCount(0);
    await expect(badge(page, track.id)).toHaveAttribute("aria-label", trackBadgeLabel);
    await page.waitForTimeout(2000);

    // F8 → F0：回到单帧标注所在帧，两个角标同时可见。
    const previousFrame = page.getByRole("button", { name: "上一帧", exact: true });
    for (let frame = FRAME_END - 1; frame >= FRAME_START; frame -= 1) {
      await previousFrame.click();
      await expect(stage).toHaveAttribute("data-video-frame-index", String(frame), {
        timeout: 10_000,
      });
      await page.waitForTimeout(560);
    }
    await expect(badge(page, single.id)).toHaveAttribute("aria-label", singleBadgeLabel);
    await page.waitForTimeout(1700);

    // 点击可见轨迹的角标：讨论切换到该对象的评论范围。
    await badge(page, track.id).click();
    const discussion = discussionPanel(page);
    await expect(discussion).toHaveAttribute("aria-hidden", "false", { timeout: 10_000 });
    await expect(discussion.getByRole("combobox", { name: "评论阅读范围" })).toHaveValue(
      "annotation",
      { timeout: 10_000 },
    );
    const discussionTarget = discussionTargetKey({
      projectId: project.id,
      taskId: task.id,
      kind: "annotation",
      annotationId: track.id,
    });
    await expect(discussion.getByRole("combobox", { name: "发送目标" })).toHaveValue(
      discussionTarget,
      { timeout: 10_000 },
    );
    await expect(
      discussion.getByRole("tab", { name: "评论 3 条评论", exact: true }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(discussion.locator('[data-testid="discussion-comment-row"]')).toHaveCount(2);
    for (const comment of [trackCommentOwner, trackCommentAdmin]) {
      const row = discussion.locator(`[data-comment-key="annotation_comment:${comment.id}"]`);
      await expect(row).toContainText(comment.body);
      await row.scrollIntoViewIfNeeded();
      await expect(row).toBeVisible();
      await expect(row).toBeInViewport();
      await page.waitForTimeout(1_600);
    }
    // The selected annotation contains both track comments, not the third task-wide comment.
    await expect(
      discussion.locator(`[data-comment-key="annotation_comment:${singleComment.id}"]`),
    ).toHaveCount(0);
    await page.waitForTimeout(1_600);

    result = {
      drawStartMs,
      drawEndMs: Date.now(),
      evidence: {
        counts: {
          [track.id]: counts.counts[track.id] ?? 0,
          [single.id]: counts.counts[single.id] ?? 0,
        },
        globalTaskCommentCount,
        scopedTrackCommentCount: 2,
        trackBadgeLabel,
        singleBadgeLabel,
        frames: { start: FRAME_START, end: FRAME_END },
        discussionScope: "annotation",
        discussionTarget,
        trackCommentIds: [trackCommentOwner.id, trackCommentAdmin.id],
        singleCommentId: singleComment.id,
      },
    };
  } catch (error) {
    actionError = error;
  } finally {
    // 清理失败不吞掉主错误：记录后继续，最后与 actionError 一并判断。
    for (const comment of comments.reverse()) {
      try {
        const response = await page.request.delete(`/api/v1/comments/${comment.id}`, {
          headers: auth(comment.token),
        });
        if (!response.ok() && response.status() !== 404) {
          console.warn(
            `[video-comment-badges] 删除评论 ${comment.id} 失败: HTTP ${response.status()}`,
          );
        }
      } catch (error) {
        cleanupError = cleanupError ?? error;
      }
    }
    for (const annotationId of annotationIds.reverse()) {
      try {
        const response = await page.request.delete(
          `/api/v1/tasks/${task.id}/annotations/${annotationId}`,
          { headers: auth(adminToken) },
        );
        if (!response.ok() && response.status() !== 404) {
          console.warn(
            `[video-comment-badges] 删除标注 ${annotationId} 失败: HTTP ${response.status()}`,
          );
        }
      } catch (error) {
        cleanupError = cleanupError ?? error;
      }
    }
  }
  if (actionError) throw actionError;
  if (cleanupError) throw cleanupError;
  if (!result) throw new Error("[video-comment-badges] 未生成验收结果");
  return result;
}
