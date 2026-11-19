/**
 * IoU 候选裁剪索引（rbush 同类分桶）。
 * 追加 `buildVertexIndex`：对密集 polygon 顶点做空间分桶 + viewport 粗筛。
 *
 * 用途：
 * - `buildIoUIndex`: WorkbenchShell 计算 dimmedAiIds 时避免对每个 AI 框扫全部 user 框。
 * - `buildVertexIndex`: 编辑态多 polygon 视口外顶点不必创建 Konva Circle 句柄，
 *   500 顶点 polygon 视口内只露 ~20 个时大幅减少 Konva 节点数。
 *
 * 调用方仍走精确判定，候选裁剪只解决"包围盒不可能交"的快速排除。
 */
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
  /** 返回与 query 包围盒可能相交的同类 shape 候选（不含 query 自身）。 */
  candidatesForBox: (query: ShapeForIoU & { cls: string }) => ShapeForIoU[];
}

/**
 * 按 cls 分桶建 rbush。每桶独立树；查询时仅在 query.cls 桶内 search。
 * 输入 boxes.length === 0 时返回空索引（candidatesForBox 直接返 []）。
 */
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

// 单 polygon 顶点空间索引（归一化 [0,1]）。
// 给定 viewport bbox，O(log n) 返回可见顶点下标集合。
interface IndexedVertex extends BBox {
  idx: number;
}

export interface VertexIndex {
  /** 返回与 viewport bbox 相交的顶点下标集合，按 polygon 顺序 ascending。 */
  verticesInBBox: (bbox: BBox) => number[];
  /** 用于 React.memo 失效判定的 polygon 内容签名。 */
  readonly size: number;
}

/**
 * 为单 polygon 建顶点索引；每个顶点用一个 0-area BBox 入树。
 * polygon 在归一化坐标 [0,1]，调用方传 viewport bbox 时也走同坐标系。
 */
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
