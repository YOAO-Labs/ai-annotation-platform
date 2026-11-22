import { describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useClipboard } from "./useClipboard";
import type { AnnotationPayload } from "@/api/tasks";
import type { Annotation } from "@/types";

function bbox(id: string, over: Partial<Annotation> = {}): Annotation {
  return {
    id,
    cls: "car",
    x: 0.4,
    y: 0.4,
    w: 0.2,
    h: 0.2,
    color: "#fff",
    ...over,
  } as Annotation;
}

function polygonAnn(id: string): Annotation {
  return {
    ...bbox(id),
    geometry: {
      type: "polygon",
      points: [
        [0.1, 0.1],
        [0.2, 0.1],
        [0.2, 0.2],
      ],
    },
    polygon: [
      [0.1, 0.1],
      [0.2, 0.1],
      [0.2, 0.2],
    ],
  } as Annotation;
}

function rotatedAnn(id: string): Annotation {
  return {
    ...bbox(id),
    geometry: {
      type: "rotated_bbox",
      cx: 0.5,
      cy: 0.5,
      w: 0.2,
      h: 0.1,
      angle: 30,
    },
  } as Annotation;
}

function polylineAnn(id: string): Annotation {
  return {
    ...bbox(id),
    geometry: {
      type: "polyline",
      points: [
        [0.1, 0.1],
        [0.4, 0.2],
      ],
    },
    polyline: [
      [0.1, 0.1],
      [0.4, 0.2],
    ],
  } as Annotation;
}

function keypointAnn(id: string): Annotation {
  return {
    ...bbox(id),
    geometry: {
      type: "keypoint",
      points: [
        { x: 0.1, y: 0.2, v: 2 },
        { x: 0.3, y: 0.4, v: 1 },
      ],
    },
    keypoints: [
      { x: 0.1, y: 0.2, v: 2 },
      { x: 0.3, y: 0.4, v: 1 },
    ],
  } as Annotation;
}

function rasterMaskAnn(id: string): Annotation {
  return {
    ...bbox(id, { x: 0, y: 0, w: 0, h: 0 }),
    geometry: {
      type: "raster_mask",
      mask: {
        encoding: "coco_rle_ref",
        size: [10, 20],
        object_key: "raster-masks/sha256/aa/bb/digest.json",
        sha256: "a".repeat(64),
        runs: 4,
        bytes: 32,
      },
    },
  } as Annotation;
}

describe("useClipboard", () => {
  it("copy 选中后返回数量并写入 clipboard setter", () => {
    const setClipboard = vi.fn();
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [bbox("a"), bbox("b")],
        selectedIds: ["a"],
        clipboard: [],
        setClipboard,
        createAnnotation: vi.fn(),
        pushBatch: vi.fn(),
        imgW: 1000,
        imgH: 1000,
      }),
    );
    let n = 0;
    act(() => {
      n = result.current.copySelection();
    });
    expect(n).toBe(1);
    expect(setClipboard).toHaveBeenCalledWith(
      expect.arrayContaining([expect.objectContaining({ id: "a" })]),
    );
  });

  it("copyAnnotations 允许直接复制传入的标注集合", () => {
    const setClipboard = vi.fn();
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [bbox("a"), bbox("b")],
        selectedIds: [],
        clipboard: [],
        setClipboard,
        createAnnotation: vi.fn(),
        pushBatch: vi.fn(),
        imgW: 1000,
        imgH: 1000,
      }),
    );
    let count = 0;
    act(() => {
      count = result.current.copyAnnotations([bbox("b")]);
    });
    expect(count).toBe(1);
    expect(setClipboard).toHaveBeenCalledWith([expect.objectContaining({ id: "b" })]);
  });

  it("raster_mask 不进入剪贴板，copy 返回 0", () => {
    const setClipboard = vi.fn();
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [rasterMaskAnn("mask")],
        selectedIds: ["mask"],
        clipboard: [],
        setClipboard,
        createAnnotation: vi.fn(),
        pushBatch: vi.fn(),
        imgW: 100,
        imgH: 100,
      }),
    );

    expect(result.current.copySelection()).toBe(0);
    expect(setClipboard).not.toHaveBeenCalled();
  });

  it("paste bbox 应用 +10px 偏移并 clamp 到 [0, 1-w]", async () => {
    const createAnnotation = vi.fn(async () => ({ id: "new-1" }) as never);
    const pushBatch = vi.fn();
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [],
        selectedIds: [],
        clipboard: [bbox("a", { x: 0.95, y: 0.95, w: 0.04, h: 0.04 })],
        setClipboard: vi.fn(),
        createAnnotation,
        pushBatch,
        imgW: 100,
        imgH: 100,
      }),
    );
    await act(async () => {
      await result.current.paste();
    });
    expect(createAnnotation).toHaveBeenCalledTimes(1);
    const payload = (createAnnotation.mock.calls[0] as unknown[])[0] as {
      geometry: { type: string; x?: number; points?: number[][] };
    };
    expect(payload.geometry.type).toBe("bbox");
    expect(payload.geometry.x).toBeCloseTo(0.96, 2);
    expect(pushBatch).toHaveBeenCalledTimes(1);
  });

  it("paste polygon 整体平移", async () => {
    const createAnnotation = vi.fn(async () => ({ id: "new-poly" }) as never);
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [],
        selectedIds: [],
        clipboard: [polygonAnn("p")],
        setClipboard: vi.fn(),
        createAnnotation,
        pushBatch: vi.fn(),
        imgW: 100,
        imgH: 100,
      }),
    );
    await act(async () => {
      await result.current.paste();
    });
    const payload = (createAnnotation.mock.calls[0] as unknown[])[0] as {
      geometry: { type: string; x?: number; points?: number[][] };
    };
    expect(payload.geometry.type).toBe("polygon");
    const pts = (payload.geometry as { points: number[][] }).points;
    expect(pts).toHaveLength(3);
    expect(pts[0][0]).toBeCloseTo(0.2, 2);
  });

  it("空 clipboard 时 paste 不触发 createAnnotation", async () => {
    const createAnnotation = vi.fn();
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [],
        selectedIds: [],
        clipboard: [],
        setClipboard: vi.fn(),
        createAnnotation,
        pushBatch: vi.fn(),
        imgW: 100,
        imgH: 100,
      }),
    );
    await act(async () => {
      await result.current.paste();
    });
    expect(createAnnotation).not.toHaveBeenCalled();
  });

  it("防御性跳过旧 clipboard 中的 raster_mask", async () => {
    const createAnnotation = vi.fn();
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [rasterMaskAnn("mask")],
        selectedIds: ["mask"],
        clipboard: [rasterMaskAnn("mask")],
        setClipboard: vi.fn(),
        createAnnotation,
        pushBatch: vi.fn(),
        imgW: 100,
        imgH: 100,
      }),
    );

    expect(result.current.hasClipboard).toBe(false);
    await act(async () => {
      expect(await result.current.duplicateSelection()).toEqual([]);
      expect(await result.current.paste()).toEqual([]);
    });
    expect(createAnnotation).not.toHaveBeenCalled();
  });

  it("paste rotated_bbox / polyline / keypoint 保留几何类型并整体平移", async () => {
    const createAnnotation = vi.fn(
      async (payload: AnnotationPayload) =>
        ({ id: `new-${payload.annotation_type ?? "unknown"}` }) as never,
    );
    const { result } = renderHook(() =>
      useClipboard({
        userBoxes: [],
        selectedIds: [],
        clipboard: [rotatedAnn("r"), polylineAnn("l"), keypointAnn("k")],
        setClipboard: vi.fn(),
        createAnnotation,
        pushBatch: vi.fn(),
        imgW: 100,
        imgH: 100,
      }),
    );
    await act(async () => {
      await result.current.paste();
    });

    expect(createAnnotation).toHaveBeenCalledTimes(3);
    const [rotatedPayload, polylinePayload, keypointPayload] = createAnnotation.mock.calls.map(
      (call) => call[0],
    ) as Array<{
      annotation_type: string;
      geometry: Record<string, unknown>;
    }>;

    expect(rotatedPayload.annotation_type).toBe("rotated_bbox");
    expect(rotatedPayload.geometry).toMatchObject({
      type: "rotated_bbox",
      cx: 0.6,
      cy: 0.6,
      angle: 30,
    });

    expect(polylinePayload.annotation_type).toBe("polyline");
    expect(polylinePayload.geometry.type).toBe("polyline");
    const polylinePoints = (polylinePayload.geometry as { points: number[][] }).points;
    expect(polylinePoints[0][0]).toBeCloseTo(0.2, 6);
    expect(polylinePoints[0][1]).toBeCloseTo(0.2, 6);
    expect(polylinePoints[1][0]).toBeCloseTo(0.5, 6);
    expect(polylinePoints[1][1]).toBeCloseTo(0.3, 6);

    expect(keypointPayload.annotation_type).toBe("keypoint");
    expect(keypointPayload.geometry.type).toBe("keypoint");
    const keypointPoints = (
      keypointPayload.geometry as { points: Array<{ x: number; y: number; v: number }> }
    ).points;
    expect(keypointPoints[0]).toMatchObject({ v: 2 });
    expect(keypointPoints[0].x).toBeCloseTo(0.2, 6);
    expect(keypointPoints[0].y).toBeCloseTo(0.3, 6);
    expect(keypointPoints[1]).toMatchObject({ v: 1 });
    expect(keypointPoints[1].x).toBeCloseTo(0.4, 6);
    expect(keypointPoints[1].y).toBeCloseTo(0.5, 6);
  });
});
