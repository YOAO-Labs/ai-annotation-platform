import { expect, type Page } from "@playwright/test";

interface NormalizedRegion {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface FrameExpectation {
  frame_index: number;
  background_luma: number;
  corner_bits: number[];
  center_bits?: number[];
  pts_ms?: number;
  duration_ms?: number;
  is_keyframe?: boolean;
  decode_index?: number;
}

export interface FrameExpectations {
  width: number;
  height: number;
  frame_count: number;
  sample_regions: {
    background: NormalizedRegion;
    corners: Array<NormalizedRegion & { bit: number }>;
    center_bits?: Array<NormalizedRegion & { bit: number }>;
  };
  frames: FrameExpectation[];
}

/**
 * 刷新/任务切换等真实导航可能在属性断言与像素采样之间替换文档，裸
 * `page.evaluate` 会以 "Execution context was destroyed" 失败。限定该错误
 * 进行有界重试：重试仍在**当前**文档上采样，帧身份断言（背景亮度 + 角位/
 * 中心位）逐帧特异，采错帧会照常失败，不会把导航竞态放大成假通过。
 */
async function evaluateSurvivingNavigation<T>(page: Page, read: () => Promise<T>): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await read();
    } catch (error) {
      if (attempt >= 2 || !String(error).includes("Execution context was destroyed")) throw error;
      await page.waitForLoadState("load").catch(() => {});
      await page.waitForTimeout(250);
    }
  }
}

export async function sampleFrameMarkers(
  page: Page,
  regions: FrameExpectations["sample_regions"],
): Promise<{
  background: { luma: number; alpha: number };
  corners: Array<{ bit: number; luma: number; alpha: number }>;
}> {
  await evaluateSurvivingNavigation(page, () =>
    page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    ),
  );
  return evaluateSurvivingNavigation(page, () =>
    page.evaluate((sampleRegions) => {
      const stage = document.querySelector<HTMLElement>('[data-testid="video-konva-stage"]');
      const canvas = stage?.querySelector<HTMLCanvasElement>(".konvajs-content > canvas");
      if (!canvas) throw new Error("Konva media canvas not found");
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Konva media canvas 2D context unavailable");
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      let minX = canvas.width;
      let minY = canvas.height;
      let maxX = -1;
      let maxY = -1;
      for (let y = 0; y < canvas.height; y += 1) {
        for (let x = 0; x < canvas.width; x += 1) {
          if (pixels.data[(y * canvas.width + x) * 4 + 3] < 200) continue;
          minX = Math.min(minX, x);
          minY = Math.min(minY, y);
          maxX = Math.max(maxX, x);
          maxY = Math.max(maxY, y);
        }
      }
      if (maxX < minX || maxY < minY) throw new Error("Konva media canvas has no opaque pixels");
      const mediaWidth = maxX - minX + 1;
      const mediaHeight = maxY - minY + 1;
      const average = (region: NormalizedRegion) => {
        const insetX = region.w * 0.2;
        const insetY = region.h * 0.2;
        const left = Math.max(minX, Math.floor(minX + (region.x + insetX) * mediaWidth));
        const top = Math.max(minY, Math.floor(minY + (region.y + insetY) * mediaHeight));
        const right = Math.min(maxX, Math.ceil(minX + (region.x + region.w - insetX) * mediaWidth));
        const bottom = Math.min(
          maxY,
          Math.ceil(minY + (region.y + region.h - insetY) * mediaHeight),
        );
        let lumaTotal = 0;
        let alphaTotal = 0;
        let count = 0;
        for (let y = top; y <= bottom; y += 1) {
          for (let x = left; x <= right; x += 1) {
            const offset = (y * canvas.width + x) * 4;
            const r = pixels.data[offset];
            const g = pixels.data[offset + 1];
            const b = pixels.data[offset + 2];
            lumaTotal += 0.2126 * r + 0.7152 * g + 0.0722 * b;
            alphaTotal += pixels.data[offset + 3];
            count += 1;
          }
        }
        if (count === 0) throw new Error("pixel sample region is empty");
        return { luma: lumaTotal / count, alpha: alphaTotal / count };
      };
      return {
        background: average(sampleRegions.background),
        corners: sampleRegions.corners.map((region) => ({
          bit: region.bit,
          ...average(region),
        })),
      };
    }, regions),
  );
}

export async function expectVideoFramePixels(
  page: Page,
  expectations: FrameExpectations,
  targetFrame: number,
): Promise<void> {
  const expected = expectations.frames.find((frame) => frame.frame_index === targetFrame);
  expect(expected, `missing frame expectation ${targetFrame}`).toBeDefined();
  const sampled = await sampleFrameMarkers(page, expectations.sample_regions);
  expect(sampled.background.alpha).toBeGreaterThan(240);
  expect(Math.abs(sampled.background.luma - expected!.background_luma)).toBeLessThanOrEqual(40);
  for (const corner of sampled.corners) {
    expect(corner.alpha).toBeGreaterThan(240);
    const expectedBit = expected!.corner_bits[corner.bit];
    if (expectedBit === 1) expect(corner.luma).toBeGreaterThan(160);
    else expect(corner.luma).toBeLessThan(95);
  }
}

/** Sample the long fixture through the actual media transform, including crop and zoom. */
export async function sampleVideoContextFrameMarkers(page: Page, expectations: FrameExpectations) {
  const regions = expectations.sample_regions.center_bits;
  expect(
    regions,
    "The Issue context fixture needs all eight central frame identity bits",
  ).toHaveLength(8);
  await evaluateSurvivingNavigation(page, () =>
    page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    ),
  );
  return evaluateSurvivingNavigation(page, () =>
    page.evaluate((sampleRegions) => {
      const stage = document.querySelector<HTMLElement>('[data-testid="video-konva-stage"]');
      const content = stage?.querySelector<HTMLElement>(".konvajs-content");
      const canvas = content?.querySelector<HTMLCanvasElement>(":scope > canvas");
      if (!stage || !content || !canvas) throw new Error("Konva media canvas unavailable");
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Konva media canvas 2D context unavailable");
      const contentRect = content.getBoundingClientRect();
      const canvasRect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / canvasRect.width;
      const scaleY = canvas.height / canvasRect.height;
      const media = {
        x: contentRect.left - canvasRect.left + Number(stage.getAttribute("data-media-x")),
        y: contentRect.top - canvasRect.top + Number(stage.getAttribute("data-media-y")),
        width: Number(stage.getAttribute("data-media-width")),
        height: Number(stage.getAttribute("data-media-height")),
      };
      if (!Object.values(media).every(Number.isFinite) || media.width <= 0 || media.height <= 0)
        throw new Error("Video media transform unavailable");
      return sampleRegions!.map((region) => {
        const left = Math.ceil((media.x + (region.x + region.w * 0.25) * media.width) * scaleX);
        const top = Math.ceil((media.y + (region.y + region.h * 0.25) * media.height) * scaleY);
        const right = Math.floor((media.x + (region.x + region.w * 0.75) * media.width) * scaleX);
        const bottom = Math.floor((media.y + (region.y + region.h * 0.75) * media.height) * scaleY);
        if (
          left < 0 ||
          top < 0 ||
          right >= canvas.width ||
          bottom >= canvas.height ||
          right <= left ||
          bottom <= top
        )
          throw new Error(
            `Central frame bit ${region.bit} is clipped; no frame identity can be asserted`,
          );
        const pixels = context.getImageData(left, top, right - left, bottom - top).data;
        let luma = 0;
        let alpha = 0;
        for (let offset = 0; offset < pixels.length; offset += 4) {
          luma +=
            0.2126 * pixels[offset] + 0.7152 * pixels[offset + 1] + 0.0722 * pixels[offset + 2];
          alpha += pixels[offset + 3];
        }
        return {
          bit: region.bit,
          luma: luma / (pixels.length / 4),
          alpha: alpha / (pixels.length / 4),
        };
      });
    }, regions),
  );
}

export async function expectVideoContextFramePixels(
  page: Page,
  expectations: FrameExpectations,
  frame: number,
) {
  const expected = expectations.frames.find((candidate) => candidate.frame_index === frame);
  expect(expected?.center_bits, `Missing central frame identity for F${frame}`).toHaveLength(8);
  const samples = await sampleVideoContextFrameMarkers(page, expectations);
  for (const sample of samples) {
    expect(sample.alpha).toBeGreaterThan(240);
    if (expected!.center_bits![sample.bit] === 1) expect(sample.luma).toBeGreaterThan(160);
    else expect(sample.luma).toBeLessThan(95);
  }
}
