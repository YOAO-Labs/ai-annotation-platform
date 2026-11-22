import RBush, { type BBox } from "rbush";

import type { ShapeForIoU } from "./iou";
import type { Pt } from "./shared/geometry/polygon";

interface IndexedShape extends BBox {
  shape: ShapeForIoU;
}

function shapeBBox(s: ShapeForIoU): BBox {
  if (s.polygon && s.polygon.length >= 3) {
    let minX = Infinity,
      minY = Infinity,
      maxX = -Infinity,
      maxY = -Infinity;
    for (const [x, y] of s.polygon) {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
    return { minX, minY, maxX, maxY };
  }
  return { minX: s.x, minY: s.y, maxX: s.x + s.w, maxY: s.y + s.h };
}

export interface IoUClassIndex {
  candidatesForBox: (query: ShapeForIoU & { cls: string }) => ShapeForIoU[];
}

export function buildIoUIndex(boxes: Array<ShapeForIoU & { cls: string }>): IoUClassIndex {
  const trees = new Map<string, RBush<IndexedShape>>();
  for (const b of boxes) {
    let tree = trees.get(b.cls);
    if (!tree) {
      tree = new RBush<IndexedShape>();
      trees.set(b.cls, tree);
    }
    tree.insert({ ...shapeBBox(b), shape: b });
  }
  return {
    candidatesForBox(query) {
      const tree = trees.get(query.cls);
      if (!tree) return [];
      const hits = tree.search(shapeBBox(query));
      return hits.map((h) => h.shape);
    },
  };
}

interface IndexedVertex extends BBox {
  idx: number;
}

export interface VertexIndex {
  verticesInBBox: (bbox: BBox) => number[];
  readonly size: number;
}

export function buildVertexIndex(points: Pt[]): VertexIndex {
  const tree = new RBush<IndexedVertex>();
  const items: IndexedVertex[] = points.map(([x, y], idx) => ({
    minX: x,
    minY: y,
    maxX: x,
    maxY: y,
    idx,
  }));
  tree.load(items);
  return {
    verticesInBBox(bbox) {
      const hits = tree.search(bbox);
      const out = hits.map((h) => h.idx);
      out.sort((a, b) => a - b);
      return out;
    },
    size: points.length,
  };
}
