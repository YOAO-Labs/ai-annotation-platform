/**
 * Mask keyboard controls and refinement of the seeded prediction.
 * Native creation, persistence and re-edit are covered by the native write matrix.
 */
import { mergeTests, type APIRequestContext } from "@playwright/test";
import { openMaskSettings, closeMaskSettings } from "../fixtures/mask-toolbar";
import { test as seedTest, expect } from "../fixtures/seed";
import { test as browserTest } from "../fixtures/raster-browser";
import { openPredictionRefinement, confirmMaskRefinement } from "../helpers/mask-refinement";

const test = mergeTests(seedTest, browserTest);

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

interface SavedMask {
  id: string;
  annotation_type: string;
  parent_prediction_id: string | null;
  class_name: string;
  geometry: { type: string };
}

interface MaskContent {
  encoding: string;
  size: number[];
  counts: number[];
}

async function savedMasks(request: APIRequestContext, taskId: string, token: string) {
  const response = await request.get(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok()).toBe(true);
  return (await response.json()) as SavedMask[];
}

async function maskContent(request: APIRequestContext, id: string, token: string) {
  const response = await request.get(`${API_BASE}/api/v1/annotations/${id}/mask-content`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok()) {
    throw new Error(`Mask content failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as MaskContent;
}

// COCO RLE is column-major; inspect fixed source pixels without the editor codec.
function pixel(content: MaskContent, x: number, y: number): number {
  const offset = x * content.size[0] + y;
  let end = 0;
  for (let index = 0; index < content.counts.length; index += 1) {
    end += content.counts[index];
    if (offset < end) return index % 2;
  }
  throw new Error("RLE does not cover requested pixel");
}

test.describe("mask editor (I11)", { tag: "@mask-native" }, () => {
  test("AI prediction polygon 精修 → 确认后新原生 Mask 入库", async ({ page, request, seed }) => {
    const data = await seed.owned();
    const taskId = data.task_ids[0];
    await seed.configureRasterMask(data.project_id, true);
    const token = await seed.accessToken(data.annotator_email);
    await seed.advanceTask({
      taskId,
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    // 注入一条 polygon prediction
    const { prediction_id: predictionId } = await seed.injectPrediction({
      taskId,
      projectId: data.project_id,
      label: "car",
      polygon: [
        [0.3, 0.3],
        [0.6, 0.3],
        [0.6, 0.6],
        [0.3, 0.6],
      ],
      score: 0.92,
    });

    await seed.prepareRasterImages(data);
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${taskId}`);
    await openPredictionRefinement(page, predictionId);

    // mask 工具应激活 + buffer 已 from polygon 初始化
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible();
    await expect(page.getByTestId("mask-settings-trigger")).toHaveAttribute("title", /· 就绪 ·/);
    // This case selects the real eraser control; keyboard behavior has its own case.
    await openMaskSettings(page);
    await page.getByTestId("mask-toolbar").getByLabel("橡皮", { exact: true }).click();

    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 10_000 });
    // fitted 完成前 Konva 宿主为 visibility:hidden (ImageStage konvaHostHidden),
    // 此窗口内的 pointer 事件不会到达画布 → 一笔橡皮会被整体丢弃且不置 dirty。
    await expect(stage.locator("canvas").first()).toBeVisible({ timeout: 10_000 });
    // 真实 E2E 图片为 64x48；默认 16px 橡皮会把约 19x14px 的候选整个擦空。
    // 用 2px 半径从多边形内部深处向外擦出与外部连通的小缺口，既保留前景，也不制造 hole。
    // boundingBox 取在 slider 之后：slider blur 触发工具栏重渲染，先取 box 会拿到
    // 重渲染前的过渡几何，换算出的擦除点会偏离 mask buffer 的真实坐标。
    const slider = page.getByTestId("mask-radius-slider");
    await slider.fill("2");
    await expect(slider).toHaveValue("2");
    await slider.blur();
    await closeMaskSettings(page);
    const box = await stage.boundingBox();
    if (!box) throw new Error("stage boundingBox 不可用");
    const cx = box.x + box.width * 0.35;
    const cy = box.y + box.height * 0.45;
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx - 20, cy, { steps: 4 });
    await page.mouse.up();

    await openMaskSettings(page);
    await page.getByTestId("mask-toolbar").getByTestId("mask-primary-action").click();
    const createdResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/tasks/${taskId}/annotations`) &&
        response.request().method() === "POST",
    );
    await confirmMaskRefinement(page, true);
    const createdResult = await createdResponse;
    expect(createdResult.ok(), `${createdResult.status()} ${await createdResult.text()}`).toBe(
      true,
    );

    await expect.poll(async () => (await savedMasks(request, taskId, token)).length).toBe(1);
    const [saved] = await savedMasks(request, taskId, token);
    expect(saved).toMatchObject({
      annotation_type: "raster_mask",
      parent_prediction_id: predictionId,
      class_name: "car",
      geometry: { type: "raster_mask" },
    });
    const content = await maskContent(request, saved.id, token);
    expect(content.encoding).toBe("coco_rle");
    expect(content.size).toEqual([48, 64]);
    expect(content.counts.reduce((sum, count) => sum + count, 0)).toBe(48 * 64);
    expect(pixel(content, 0, 0)).toBe(0);
    expect(pixel(content, 32, 22)).toBe(1);
    const foreground = content.counts.reduce((sum, count, i) => sum + (i % 2 ? count : 0), 0);
    expect(foreground).toBeLessThan(285);

    await page.reload();
    await expect(page.getByTestId(`box-list-item-${saved.id}`)).toBeVisible();
    expect(await savedMasks(request, taskId, token)).toContainEqual(saved);
    expect(await maskContent(request, saved.id, token)).toEqual(content);
  });

  test("mask 工具 hotkey 全集：B / E / 滚轮 / Esc", async ({ page, seed }) => {
    const data = await seed.owned();
    await seed.advanceTask({
      taskId: data.task_ids[0],
      toStatus: "pending",
      annotatorEmail: data.annotator_email,
    });
    await seed.prepareRasterImages(data);
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${data.task_ids[0]}`);
    await expect(page.getByTestId("workbench-stage")).toHaveAttribute("data-image-ready", "true");

    await page.keyboard.press("m");
    await expect(page.getByTestId("mask-tool-capsule")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("mask-settings-trigger")).toHaveAttribute("title", /· 就绪 ·/);

    // 默认是 brush 模式（aria-pressed 或视觉态由 chipStyle 控制），按 E 切橡皮
    await page.keyboard.press("e");
    const trigger = page.getByTestId("mask-settings-trigger");
    await expect(trigger).toHaveAttribute("aria-label", "Mask 常用工具：橡皮");
    // 按 B 切回笔刷
    await page.keyboard.press("b");
    await expect(trigger).toHaveAttribute("aria-label", "Mask 常用工具：笔刷");

    // 滚轮调半径 —— 取 slider 的 value 前后对比
    const stage = page.getByTestId("workbench-stage");
    const box = await stage.boundingBox();
    if (!box) throw new Error("stage boundingBox 不可用");
    const slider = page.getByTestId("mask-radius-slider");
    await openMaskSettings(page);
    const before = await slider.inputValue();
    await closeMaskSettings(page);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -120);
    await openMaskSettings(page);
    const after = await slider.inputValue();
    await closeMaskSettings(page);
    expect(Number(after)).not.toBe(Number(before));

    // Esc 取消 → mask-toolbar 隐藏（mask 工具退出）
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("mask-tool-capsule")).toHaveCount(0);
  });
});
