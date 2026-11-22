import { describe, expect, it } from "vitest";
import type { Viewport } from "../state/useViewportTransform";
import {
  clientToVideoNorm,
  normToPixel,
  pixelToNorm,
  videoIntrinsicSize,
  videoNormToClient,
} from "./videoKonvaCoordinates";

describe("videoIntrinsicSize", () => {
  it("有宽高时直接采用", () => {
    expect(videoIntrinsicSize(1920, 1080)).toEqual({ w: 1920, h: 1080 });
  });

  it("缺省回退 1280×720(16:9)", () => {
    expect(videoIntrinsicSize(undefined, undefined)).toEqual({ w: 1280, h: 720 });
    expect(videoIntrinsicSize(null, null)).toEqual({ w: 1280, h: 720 });
  });

  it("只有宽 → 按 16:9 反推高", () => {
    expect(videoIntrinsicSize(1600, undefined)).toEqual({ w: 1600, h: 900 });
  });
});

describe("normToPixel / pixelToNorm", () => {
  const size = { w: 1920, h: 1080 };

  it("归一化 ↔ 像素往返一致", () => {
    const norm = { x: 0.25, y: 0.5 };
    const px = normToPixel(norm, size);
    expect(px).toEqual({ x: 480, y: 540 });
    expect(pixelToNorm(px, size)).toEqual(norm);
  });

  it("尺寸为 0 的轴归一化返回 0(不除零)", () => {
    expect(pixelToNorm({ x: 10, y: 10 }, { w: 0, h: 0 })).toEqual({ x: 0, y: 0 });
  });
});

describe("clientToVideoNorm / videoNormToClient", () => {
  const rect = { left: 100, top: 50 };
  const size = { w: 1920, h: 1080 };

  it("含视口平移/缩放时 client ↔ norm 往返一致", () => {
    const vp: Viewport = { scale: 2, tx: 30, ty: -20 };
    const norm = { x: 0.4, y: 0.6 };
    const client = videoNormToClient(norm, rect, vp, size);
    const back = clientToVideoNorm(client.x, client.y, rect, vp, size);
    expect(back).not.toBeNull();
    expect(back!.x).toBeCloseTo(norm.x, 10);
    expect(back!.y).toBeCloseTo(norm.y, 10);
  });

  it("与图片 toImg 公式同构:(client-left-tx)/scale/size", () => {
    const vp: Viewport = { scale: 1, tx: 0, ty: 0 };
    const norm = clientToVideoNorm(rect.left + 960, rect.top + 540, rect, vp, size);
    expect(norm).toEqual({ x: 0.5, y: 0.5 });
  });

  it("尺寸未就绪返回 null", () => {
    const vp: Viewport = { scale: 1, tx: 0, ty: 0 };
    expect(clientToVideoNorm(0, 0, rect, vp, { w: 0, h: 0 })).toBeNull();
  });
});
