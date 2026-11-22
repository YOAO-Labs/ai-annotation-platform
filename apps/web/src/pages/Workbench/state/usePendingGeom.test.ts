import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePendingGeom } from "./usePendingGeom";
import type { AnnotationResponse, BboxGeometry, PolygonGeometry } from "@/types";

function ann(id: string, geom: AnnotationResponse["geometry"]): AnnotationResponse {
  return {
    id,
    task_id: "T",
    project_id: "P",
    user_id: "U",
    class_name: "c",
    geometry: geom,
    confidence: 1,
    source: "manual",
    attributes: {},
    created_at: "2026-07-01T00:00:00Z",
    updated_at: null,
    parent_prediction_id: null,
    lead_time: null,
    version: 1,
  } as AnnotationResponse;
}

const G1: BboxGeometry = { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 };
const G2: BboxGeometry = { type: "bbox", x: 0.3, y: 0.3, w: 0.4, h: 0.4 };

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("usePendingGeom", () => {
  it("mark 后 pendingGeomMap 立即含条目 (桥接 setDrag→onMutate 一帧空窗)", () => {
    const { result } = renderHook(() => usePendingGeom([ann("a", G1)]));
    expect(result.current.pendingGeomMap.size).toBe(0);
    act(() => result.current.markPendingGeom("a", G2));
    expect(result.current.pendingGeomMap.get("a")).toEqual(G2);
  });

  it("annotations 反映到目标几何时自动清 (乐观回填后消费方无缝落地)", () => {
    const { result, rerender } = renderHook(
      ({ list }: { list: AnnotationResponse[] }) => usePendingGeom(list),
      { initialProps: { list: [ann("a", G1)] } },
    );
    act(() => result.current.markPendingGeom("a", G2));
    expect(result.current.pendingGeomMap.get("a")).toEqual(G2);
    rerender({ list: [ann("a", G2)] });
    expect(result.current.pendingGeomMap.has("a")).toBe(false);
  });

  it("annotations 未反映时超时兜底清 (10s 后主收敛路径均失效才清, 防挂死顶住旧几何)", () => {
    const { result } = renderHook(() => usePendingGeom([ann("a", G1)]));
    act(() => result.current.markPendingGeom("a", G2));
    expect(result.current.pendingGeomMap.has("a")).toBe(true);
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current.pendingGeomMap.has("a")).toBe(true);
    act(() => {
      vi.advanceTimersByTime(5500);
    });
    expect(result.current.pendingGeomMap.has("a")).toBe(false);
  });

  it("annotations 被删除时清 (标注消失了 override 不该继续顶)", () => {
    const { result, rerender } = renderHook(
      ({ list }: { list: AnnotationResponse[] }) => usePendingGeom(list),
      { initialProps: { list: [ann("a", G1)] } },
    );
    act(() => result.current.markPendingGeom("a", G2));
    rerender({ list: [] });
    expect(result.current.pendingGeomMap.has("a")).toBe(false);
  });

  it("多条目互不干扰: a 收敛不影响 b", () => {
    const { result, rerender } = renderHook(
      ({ list }: { list: AnnotationResponse[] }) => usePendingGeom(list),
      { initialProps: { list: [ann("a", G1), ann("b", G1)] } },
    );
    act(() => {
      result.current.markPendingGeom("a", G2);
      result.current.markPendingGeom("b", G2);
    });
    rerender({ list: [ann("a", G2), ann("b", G1)] });
    expect(result.current.pendingGeomMap.has("a")).toBe(false);
    expect(result.current.pendingGeomMap.get("b")).toEqual(G2);
  });

  it("按 geometry.type 分辨: polygon 不会误命中 bbox", () => {
    const poly: PolygonGeometry = {
      type: "polygon",
      points: [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
    };
    const { result } = renderHook(() => usePendingGeom([ann("a", G1)]));
    act(() => result.current.markPendingGeom("a", poly));
    expect(result.current.pendingGeomMap.get("a")).toEqual(poly);
  });
});
