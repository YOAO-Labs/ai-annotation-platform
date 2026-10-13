import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import type { APIRequestContext, APIResponse } from "@playwright/test";

import { expect, test, type SeedNativeMaskCandidateData } from "../fixtures/seed";

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

interface MaskReference {
  encoding: "coco_rle_ref";
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
}

interface VideoMaskAnnotation {
  id: string;
  version: number;
  geometry: {
    type: "video_track_mask";
    keyframes: Array<{ frame_index: number; source?: string }>;
    outside?: Array<{ from: number; to: number; source?: string }>;
  };
}

function auth(token: string) {
  return { Authorization: `Bearer ${token}` };
}

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

async function listAnnotations(
  request: APIRequestContext,
  taskId: string,
  token: string,
): Promise<VideoMaskAnnotation[]> {
  return await json<VideoMaskAnnotation[]>(
    await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, { headers: auth(token) }),
  );
}

function isKeyframeResponse(
  response: APIResponse,
  taskId: string,
  annotationId: string,
  frameIndex: number,
  method: "PATCH" | "PUT",
) {
  return (
    response
      .url()
      .endsWith(
        `/api/v1/tasks/${taskId}/video/tracks/${annotationId}/mask-keyframes/${frameIndex}`,
      ) && response.request().method() === method
  );
}

function isMutationResponse(response: APIResponse, taskId: string) {
  return (
    response.url().endsWith(`/api/v1/tasks/${taskId}/annotations/mask-mutations:commit`) &&
    response.request().method() === "POST"
  );
}

async function seedVideoMask(
  request: APIRequestContext,
  seedRle: SeedNativeMaskCandidateData["rle"],
  taskId: string,
  token: string,
): Promise<VideoMaskAnnotation> {
  const mask = await json<MaskReference>(
    await request.post(`${API_BASE}/api/v1/tasks/${taskId}/mask-content`, {
      headers: auth(token),
      data: seedRle,
    }),
  );
  return await json<VideoMaskAnnotation>(
    await request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: auth(token),
      data: {
        annotation_type: "video_track_mask",
        tool_unit_id: "region",
        class_name: "car",
        geometry: {
          type: "video_track_mask",
          track_id: "trk_e2e_mask_keyframe_operations",
          keyframes: [
            { frame_index: 0, mask, source: "manual", occluded: false },
            { frame_index: 10, mask, source: "manual", occluded: false },
          ],
          outside: [],
        },
      },
    }),
  );
}

test("视频 Mask 关键帧复制、outside、删除撤销与组件拆轨保持原子语义", async ({
  page,
  request,
  seed,
}) => {
  test.setTimeout(90_000);
  const data = await seed.owned();
  const { task_id: taskId } = await seed.videoTask(data.project_id);
  const fixture = await seed.nativeMaskCandidate(taskId, {
    variant: "multimask_donut",
  });
  const token = await seed.accessToken(data.admin_email);
  const source = await seedVideoMask(request, fixture.rles.at(-1) ?? fixture.rle, taskId, token);

  await seed.injectToken(page, data.admin_email);
  await seed.setPetEnabled(data.admin_email, true);
  await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
  await expect(page.getByTestId("video-konva-stage")).toBeVisible({ timeout: 20_000 });
  const row = page.getByTestId(`video-mask-track-${source.id}`);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();
  await expect(page.getByRole("button", { name: "复制当前帧" })).toBeEnabled();

  const copiedAtZero = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/annotations/${source.id}/mask-content/0`),
  );
  await page.getByRole("button", { name: "复制当前帧" }).click();
  expect((await copiedAtZero).status()).toBe(200);

  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("ArrowRight");
  await expect(page.getByText(/F 1 \//)).toBeVisible({ timeout: 10_000 });

  let keyframeWrites = 0;
  page.on("request", (outgoing) => {
    if (
      outgoing.url().includes(`/video/tracks/${source.id}/mask-keyframes/1`) &&
      ["PATCH", "PUT"].includes(outgoing.method())
    )
      keyframeWrites += 1;
  });
  await page.getByRole("button", { name: "粘贴当前轨迹" }).click();
  await openMaskSettings(page);
  const toolbar = page.getByTestId("mask-toolbar");
  await expect(toolbar).toContainText("未保存", { timeout: 15_000 });
  expect(keyframeWrites).toBe(0);
  const materialized = page.waitForResponse((response) =>
    isKeyframeResponse(response, taskId, source.id, 1, "PUT"),
  );
  await toolbar.getByTestId("mask-primary-action").click();
  expect((await materialized).status()).toBe(200);
  // 网络响应先于保存成功后的会话清理；等 UI 真正回到选择态再发起下一项操作。
  await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0, { timeout: 10_000 });

  const copiedAtOne = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/annotations/${source.id}/mask-content/1`),
  );
  await page.getByRole("button", { name: "复制当前帧" }).click();
  expect((await copiedAtOne).status()).toBe(200);

  let mutationWrites = 0;
  page.on("request", (outgoing) => {
    if (outgoing.url().endsWith(`/api/v1/tasks/${taskId}/annotations/mask-mutations:commit`))
      mutationWrites += 1;
  });
  await page.getByRole("button", { name: "粘贴新轨迹" }).click();
  await openMaskSettings(page);
  await expect(toolbar).toContainText("粘贴为新轨迹", { timeout: 15_000 });
  await expect(toolbar).toContainText("待原子提交");
  expect(mutationWrites).toBe(0);
  const copiedTrack = page.waitForResponse((response) => isMutationResponse(response, taskId));
  await toolbar.getByTestId("mask-primary-action").click();
  const copyResponse = await copiedTrack;
  expect(copyResponse.status(), await copyResponse.text()).toBe(200);
  expect(copyResponse.request().postDataJSON()).toMatchObject({
    operation: "copy_keyframe",
    source_frame_index: 1,
  });
  expect((await copyResponse.json()).created_annotations).toHaveLength(1);
  await expect.poll(async () => (await listAnnotations(request, taskId, token)).length).toBe(2);

  await page.getByRole("button", { name: "收起浮窗" }).click();
  await row.click();
  await page.getByLabel(/展开选中信息卡:car/).click();
  const markedOutside = page.waitForResponse((response) =>
    isKeyframeResponse(response, taskId, source.id, 1, "PATCH"),
  );
  await page.getByRole("button", { name: "标记消失" }).click();
  expect((await markedOutside).status()).toBe(200);
  await expect(page.getByRole("button", { name: "恢复保持" })).toBeEnabled();

  const restoredHeld = page.waitForResponse((response) =>
    isKeyframeResponse(response, taskId, source.id, 1, "PATCH"),
  );
  await page.getByRole("button", { name: "恢复保持" }).click();
  expect((await restoredHeld).status()).toBe(200);
  await expect(page.getByRole("button", { name: "删除关键帧" })).toBeEnabled();

  const deleted = page.waitForResponse((response) =>
    isKeyframeResponse(response, taskId, source.id, 1, "PATCH"),
  );
  await page.keyboard.press("Delete");
  const deleteResponse = await deleted;
  expect(deleteResponse.status(), await deleteResponse.text()).toBe(200);
  expect((await deleteResponse.allHeaders())["x-resolved-keyframe-frame"]).toBe("0");
  await expect(page.getByTestId("video-track-context-state")).toHaveAttribute("data-state", "held");
  await expect(page.getByTestId("video-track-context-source")).toContainText("保持自 F0");

  // The context bar intentionally owns its own keyboard controls; return focus to the
  // workbench before sending the global undo shortcut.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const undone = page.waitForResponse((response) =>
    isKeyframeResponse(response, taskId, source.id, 1, "PUT"),
  );
  // Deleting the keyframe reloads the held Mask. Its loading guard consumes undo.
  await openMaskSettings(page);
  await expect(toolbar).toContainText("就绪");
  await closeMaskSettings(page);
  await page.keyboard.press("Control+z");
  const undoResponse = await undone;
  expect(undoResponse.status(), await undoResponse.text()).toBe(200);
  expect(undoResponse.request().postDataJSON()).toMatchObject({ source: "manual" });
  await expect(page.getByTestId("video-track-context-state")).toHaveAttribute(
    "data-state",
    "keyframe",
  );

  await page.getByRole("button", { name: "组件拆轨" }).click();
  await openMaskSettings(page);
  await expect(toolbar).toContainText("拆分组件", { timeout: 15_000 });
  await expect(toolbar).toContainText("1 个来源 → 3 个结果");
  const split = page.waitForResponse((response) => isMutationResponse(response, taskId));
  await toolbar.getByTestId("mask-primary-action").click();
  const splitResponse = await split;
  expect(splitResponse.status(), await splitResponse.text()).toBe(200);
  expect(splitResponse.request().postDataJSON()).toMatchObject({
    operation: "split_components",
  });
  expect((await splitResponse.json()).created_annotations).toHaveLength(2);
  await expect.poll(async () => (await listAnnotations(request, taskId, token)).length).toBe(4);
});
