import type { APIRequestContext, APIResponse, Page } from "@playwright/test";
import { randomUUID } from "node:crypto";

import { expect, test as base, type SeedData } from "../fixtures/seed";
import { rangeValueForFrame, timelineClickX } from "../helpers/video-timeline-seek";

// 真实指针时间轴定位回归（default-two 偶发失败修复的阶段 C）。
//
// `video-issue-context.spec.ts` 的持久化前置定位已改为确定性的 range 输入通路，
// 它不能证明真实鼠标点击路径没有产品缺陷。本 spec 用 `video-timeline-shell`
// 定位器 + `locator.click({ position })` 走应用真实指针路径（shell capture 阶段
// 的 frameFromPointer → onSeek），Playwright 的 hit-target 检查会让被浮层吞掉的
// 点击显式失败，而不是像裸 `mouse.click` 一样静默丢失。约定：
// - 不使用 `force: true`、不全局关动画、不用键盘/程序化输入补救失败点击；
// - `setFrameByRange` 仅用于建立起始帧前置条件，不是被测行为；
// - 浮层可交互是真实点击的合法前置：真实用户移动指针到画布即唤出浮层，
//   这里用同一真实指针移动建立（video-konva-stage onPointerMove →
//   showPlaybackOverlay），并断言 shell 的 pointerEvents 恢复 auto。
// 慢媒体/连续导航的旧请求覆盖顺序由组件级单测
// （useVideoPlaybackController.stale-seek.test.ts，Issue #114）与 G2 套件的
// 延迟媒体用例覆盖，此处不重复构造全局媒体阻断。

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
const MAIN_FIXTURE = "h264-issue-context";
const MAX_FRAME = 179;

const stage = (page: Page) => page.getByTestId("video-konva-stage");
const timelineShell = (page: Page) => page.getByTestId("video-timeline-shell");
const navigation = (page: Page) => page.getByTestId("issue-frame-navigation");
const modal = (page: Page) => page.getByRole("dialog").filter({ hasText: "标记问题 (Issue)" });
const auth = (token: string) => ({ Authorization: `Bearer ${token}` });

interface SeekCase {
  data: SeedData;
  taskId: string;
  token: string;
}

const test = base.extend<{ seekCase: SeekCase }>({
  seekCase: async ({ page, request, seed }, provide) => {
    const data = await seed.owned();
    const video = await seed.videoWebCodecs(data.project_id, { fixture: MAIN_FIXTURE });
    const token = await seed.accessToken(data.admin_email);
    await expectSeededChunks(request, token, video.task_id);
    await json(
      await request.patch(`${API_BASE}/api/v1/projects/${data.project_id}`, {
        headers: auth(token),
        data: { ai_enabled: false, ai_interactive_enabled: false, ml_backend_id: null },
      }),
    );
    expect(
      (
        await request.delete(
          `${API_BASE}/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`,
          { headers: auth(token) },
        )
      ).status(),
    ).toBe(204);
    await seed.injectToken(page, data.admin_email);
    await page.evaluate(() => localStorage.setItem("video.experimental.webcodecs", "1"));
    await page.setViewportSize({ width: 1440, height: 1000 });
    await provide({ data, taskId: video.task_id, token });
    try {
      if (!page.isClosed()) await page.goto("about:blank");
    } finally {
      await seed.owned();
    }
  },
});

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function expectSeededChunks(request: APIRequestContext, token: string, taskId: string) {
  const base = `${API_BASE}/api/v1/tasks/${taskId}/video`;
  const manifest = await json<{ frame_count: number; chunk_size_frames: number }>(
    await request.get(`${base}/manifest-v2`, { headers: auth(token) }),
  );
  expect(manifest.frame_count).toBe(MAX_FRAME + 1);
  const { chunks } = await json<{
    chunks: Array<{ chunk_id: number; start_frame: number; end_frame: number; status: string }>;
  }>(
    await request.get(`${base}/chunks`, {
      headers: auth(token),
      params: { from_frame: 0, to_frame: manifest.frame_count - 1 },
    }),
  );
  expect(chunks).toHaveLength(Math.ceil(manifest.frame_count / manifest.chunk_size_frames));
  for (const chunk of chunks) expect(chunk.status).toBe("ready");
}

async function openWorkbench(page: Page, seekCase: SeekCase) {
  // 冷媒体响应可能被延迟；DOMContentLoaded 不等待它。
  await page.goto(`/projects/${seekCase.data.project_id}/annotate?task=${seekCase.taskId}`, {
    waitUntil: "domcontentloaded",
  });
  await expect(stage(page)).toBeVisible({ timeout: 25_000 });
  await page.getByRole("button", { name: "布局", exact: true }).click();
  await page.getByRole("button", { name: "标准标注布局", exact: true }).click();
}

async function readTimeline(page: Page): Promise<{ from: number; to: number }> {
  const overlay = page.getByTestId("video-playback-overlay");
  await expect(overlay).toHaveAttribute("data-timeline-from", /\d/);
  return overlay.evaluate((node) => ({
    from: Number(node.getAttribute("data-timeline-from")),
    to: Number(node.getAttribute("data-timeline-to")),
  }));
}

async function expectPaintSettled(page: Page) {
  // 精确取帧异步完成，负载高时会迟到；等绘制帧号追上选中帧号再继续。
  // 原生 <video> 回退不经精确管线，无需等待。
  await expect
    .poll(
      () =>
        stage(page).evaluate((node) => {
          if (node.getAttribute("data-video-frame-source") !== "webcodecs") return true;
          return (
            node.getAttribute("data-video-painted-frame-index") ===
            node.getAttribute("data-video-frame-index")
          );
        }),
      { timeout: 30_000 },
    )
    .toBe(true);
}

/** 起始帧前置条件：走确定性 range 输入通路（被测行为是真实指针点击，见文件头）。 */
async function setFrameByRange(page: Page, frame: number) {
  const window = await readTimeline(page);
  const input = page.getByLabel("视频帧时间轴", { exact: true });
  await input.scrollIntoViewIfNeeded();
  const mapping = rangeValueForFrame(frame, window);
  expect(mapping.expressible, `起始帧 F${frame} 在窗口 [${window.from}, ${window.to}] 可表达`).toBe(
    true,
  );
  await input.fill(String(mapping.value));
  await expect(stage(page)).toHaveAttribute("data-video-frame-index", String(frame));
  await expectPaintSettled(page);
}

/** 真实用户路径的前置：指针移动到画布唤出浮层，并确认 shell 可接收指针事件。 */
async function revealTimeline(page: Page) {
  const box = (await stage(page).boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 4 });
  await expect
    .poll(() => timelineShell(page).evaluate((node) => getComputedStyle(node).pointerEvents))
    .toBe("auto");
}

/** 真实单击：shell 定位器 + 几何换算与组件 frameFromPointer 一致（见 helper）。 */
async function clickTimelineFrame(page: Page, frame: number) {
  const window = await readTimeline(page);
  expect(
    frame >= window.from && frame <= window.to && Number.isInteger(frame),
    `点击目标 F${frame} 在窗口 [${window.from}, ${window.to}] 内`,
  ).toBe(true);
  const shell = timelineShell(page);
  await shell.scrollIntoViewIfNeeded();
  const rect = (await shell.boundingBox())!;
  await shell.click({
    position: { x: timelineClickX(frame, window, rect.width), y: rect.height / 2 },
  });
}

async function expectNavigated(page: Page, frame: number, tolerance = 0) {
  // tolerance>0 仅用于端点落点：输入量化到整数像素给最边缘点击带来 ±1 帧
  // 的固有不确定性（真实用户相同），且 pctToFrame 保证不越出窗口端点。
  await expect(async () => {
    const value = Number(await stage(page).getAttribute("data-video-frame-index"));
    expect(Number.isInteger(value) && Math.abs(value - frame)).toBeLessThanOrEqual(tolerance);
  }).toPass({ timeout: 5_000 });
  await expectPaintSettled(page);
  await expect(async () => {
    const value = Number(await stage(page).getAttribute("data-video-frame-index"));
    expect(Number.isInteger(value) && Math.abs(value - frame)).toBeLessThanOrEqual(tolerance);
  }).toPass({ timeout: 5_000 });
}

test.describe("视频时间轴真实指针定位", () => {
  test.setTimeout(120_000);
  test.use({ actionTimeout: 10_000 });

  test("折叠时间轴：不同起始帧的单次点击直接导航到点击帧", async ({ page, seekCase }) => {
    await openWorkbench(page, seekCase);
    for (const [start, target] of [
      [0, 120],
      [95, 45],
      [121, 173],
    ] as const) {
      await test.step(`从 F${start} 单次点击到 F${target}`, async () => {
        await setFrameByRange(page, start);
        await revealTimeline(page);
        await clickTimelineFrame(page, target);
        await expectNavigated(page, target);
      });
    }
  });

  test("展开时间轴：展开态几何下单次点击不被过渡层吞掉", async ({ page, seekCase }) => {
    await openWorkbench(page, seekCase);
    await setFrameByRange(page, 10);
    await page.getByTestId("video-timeline-toggle").click();
    await expect(page.getByTestId("video-timeline-details")).toBeVisible();
    await revealTimeline(page);
    await clickTimelineFrame(page, 140);
    await expectNavigated(page, 140);
    await clickTimelineFrame(page, 25);
    await expectNavigated(page, 25);
  });

  test("关闭 Issue 表单后：单次点击 F120 无需键盘补偿即建立目标导航", async ({
    page,
    seekCase,
  }) => {
    await openWorkbench(page, seekCase);
    await setFrameByRange(page, 93);
    // 真实表单创建 F93 Issue；提交成功后表单关闭（原始故障即发生在下一步）。
    const size = page.viewportSize()!;
    await page.mouse.move(size.width - 12, size.height - 12);
    await page.getByTestId("issue-pin-fab").click();
    await expect(page.getByTestId("issue-pin-fab")).toHaveAttribute("data-armed", "true");
    const bounds = await stage(page).evaluate((element) => {
      const content = element.querySelector<HTMLElement>(".konvajs-content");
      if (!content) throw new Error("Konva content bounds unavailable");
      const media = {
        x: Number(element.getAttribute("data-media-x")),
        y: Number(element.getAttribute("data-media-y")),
        width: Number(element.getAttribute("data-media-width")),
        height: Number(element.getAttribute("data-media-height")),
      };
      if (!Object.values(media).every(Number.isFinite) || media.width <= 0 || media.height <= 0)
        throw new Error("Video media transform unavailable");
      const contentBounds = content.getBoundingClientRect();
      const scaleX = contentBounds.width / content.clientWidth;
      const scaleY = contentBounds.height / content.clientHeight;
      return {
        x: contentBounds.left + media.x * scaleX,
        y: contentBounds.top + media.y * scaleY,
        width: media.width * scaleX,
        height: media.height * scaleY,
      };
    });
    await page.mouse.click(bounds.x + bounds.width * 0.47, bounds.y + bounds.height * 0.53);
    await expect(navigation(page)).toHaveAttribute("data-status", "ready", { timeout: 12_000 });
    await expect(navigation(page)).toHaveAttribute("data-frame-index", "93");
    await expect(modal(page)).toBeVisible();
    await expect(page.getByTestId("issue-create-frame")).toHaveText("源帧 F 93");
    const saved = page.waitForResponse(
      (candidate) =>
        new URL(candidate.url()).pathname === "/api/v1/feedbacks" &&
        candidate.request().method() === "POST",
    );
    await modal(page)
      .getByPlaceholder("描述问题位置 / 现象 / 期望行为")
      .fill(`timeline click ${randomUUID()}`);
    await modal(page).getByRole("button", { name: "提交", exact: true }).click();
    expect((await saved).ok()).toBe(true);
    await expect(modal(page)).toBeHidden();
    // 原失败链路：提交后指针已离开画布，浮层自动隐藏后裸坐标点击被静默吞掉。
    // 真实用户会把指针移回时间轴（浮层随之恢复可交互）；此处复现同一前置后
    // 单次点击，不允许任何键盘补偿。
    await revealTimeline(page);
    await clickTimelineFrame(page, 120);
    await expectNavigated(page, 120);
  });

  test("缩放窗口与端点：点击映射与窗口语义一致且不越界", async ({ page, seekCase }) => {
    await openWorkbench(page, seekCase);
    await page.getByTestId("video-timeline-toggle").click();
    await expect(page.getByTestId("video-timeline-details")).toBeVisible();
    await page.getByTestId("video-timeline-zoom-in").click();
    // 等窗口状态实际更新后再换算点击几何，不复用缩放前的窗口。
    await expect
      .poll(async () => {
        const window = await readTimeline(page);
        return window.to - window.from;
      })
      .toBeLessThan(MAX_FRAME);
    const zoomed = await readTimeline(page);
    expect(zoomed.to - zoomed.from).toBeLessThan(MAX_FRAME);
    const mid = Math.round((zoomed.from + zoomed.to) / 2);
    await revealTimeline(page);
    await clickTimelineFrame(page, mid);
    await expectNavigated(page, mid);
    // 回全窗口后点击首末帧（端点在 helper 中内缩 2px；落点量化带来 ±1 帧
    // 固有不确定性，断言端点 ±1 帧且不越界，内部帧场景仍精确断言）。
    await page.getByTestId("video-timeline-zoom-reset").click();
    await expect.poll(async () => (await readTimeline(page)).to).toBe(MAX_FRAME);
    await revealTimeline(page);
    await clickTimelineFrame(page, 0);
    await expectNavigated(page, 0, 1);
    await clickTimelineFrame(page, MAX_FRAME);
    await expectNavigated(page, MAX_FRAME, 1);
  });
});
