import { describe, expect, it } from "vitest";
import { buildIoUIndex, buildVertexIndex } from "./iou-index";
import type { Pt } from "./shared/geometry/polygon";

interface Box {
  id: string;
  cls: string;
  x: number;
  y: number;
  w: number;
  h: number;
  polygon?: [number, number][];
}

const b = (id: string, cls: string, x: number, y: number, w: number, h: number): Box => ({
  id,
  cls,
  x,
  y,
  w,
  h,
});

describe("buildIoUIndex", () => {
  it("空输入 → 空候选", () => {
    const idx = buildIoUIndex([]);
    expect(idx.candidatesForBox(b("a", "car", 0, 0, 0.1, 0.1))).toEqual([]);
  });

  it("不同类不互相干扰", () => {
    const cars = [b("c1", "car", 0, 0, 0.1, 0.1)];
    const persons = [b("p1", "person", 0, 0, 0.1, 0.1)];
    const idx = buildIoUIndex([...cars, ...persons]);

    expect(idx.candidatesForBox(b("q", "car", 0, 0, 0.1, 0.1))).toHaveLength(1);
    expect(idx.candidatesForBox(b("q", "person", 0, 0, 0.1, 0.1))).toHaveLength(1);
    expect(idx.candidatesForBox(b("q", "ufo", 0, 0, 0.1, 0.1))).toEqual([]);
  });

  it("同类内仅返回包围盒相交的候选", () => {
    const boxes = [b("near", "car", 0, 0, 0.2, 0.2), b("far", "car", 0.8, 0.8, 0.1, 0.1)];
    const idx = buildIoUIndex(boxes);

    const cands = idx.candidatesForBox(b("q", "car", 0.05, 0.05, 0.1, 0.1));
    expect(cands.map((c) => (c as Box).id)).toEqual(["near"]);
  });

  it("polygon 形状用顶点 bbox 入索引", () => {
    const triangle: Box = {
      id: "tri",
      cls: "x",
      x: 0,
      y: 0,
      w: 0.3,
      h: 0.3,
      polygon: [
        [0.1, 0.1],
        [0.3, 0.1],
        [0.2, 0.3],
      ],
    };
    const idx = buildIoUIndex([triangle]);
    expect(idx.candidatesForBox(b("q", "x", 0.15, 0.15, 0.05, 0.05))).toHaveLength(1);
    expect(idx.candidatesForBox(b("q", "x", 0.5, 0.5, 0.1, 0.1))).toEqual([]);
  });
});

describe("buildVertexIndex", () => {
  const pts: Pt[] = [
    [0.05, 0.05],
    [0.25, 0.1],
    [0.5, 0.05],
    [0.95, 0.5],
    [0.5, 0.95],
    [0.05, 0.5],
  ];

  it("size 反映顶点数", () => {
    expect(buildVertexIndex(pts).size).toBe(6);
  });

  it("命中左半区时只返回左侧顶点", () => {
    const idx = buildVertexIndex(pts);
    const hits = idx.verticesInBBox({ minX: 0, minY: 0, maxX: 0.3, maxY: 1 });
    expect(hits).toEqual([0, 1, 5]);
  });

  it("空 bbox 返回空", () => {
    const idx = buildVertexIndex(pts);
    expect(idx.verticesInBBox({ minX: 0.8, minY: 0.8, maxX: 0.9, maxY: 0.9 })).toEqual([]);
  });

  it("覆盖全图返回全部顶点（已排序）", () => {
    const idx = buildVertexIndex(pts);
    expect(idx.verticesInBBox({ minX: 0, minY: 0, maxX: 1, maxY: 1 })).toEqual([0, 1, 2, 3, 4, 5]);
  });
});
