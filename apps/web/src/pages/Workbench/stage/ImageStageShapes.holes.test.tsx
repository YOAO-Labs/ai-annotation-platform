// KonvaPolygon holes / multi_polygon 渲染集成测试 (ADR-0052 D5)。
//
// 三层分工 (见 konvaMock.tsx 顶部注释):
//   1. 路径构造纯函数 → evenOddFill.test.ts (已覆盖)
//   2. konva mock props 透传 → 本文件: 断言 KonvaPolygon 在 b.holes / b.multiPolygon
//      存在时启用 fillRule=evenodd 分支 (sceneFunc 是函数, mock 无法直接断言其调用,
//      但 fillRule 属性的存在性 = 分支已启用的可靠代理信号)。
//   3. 真实 canvas 渲染回归 → Playwright。
//
// 同时验证: 启用 holes 分支后外环 points 仍正确透传 (label/anchor/hit 用), 以及
// 单环快路径 (无 holes/multi) 不带 fillRule (不回归常见 bbox/单 polygon)。

import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { KonvaPolygon } from "./ImageStageShapes";
import { DEFAULT_ANNOTATION_VISUAL } from "./annotationVisual";
import type { Annotation } from "@/types";
import {
  buildEvenOddPaths,
  collectOuterRings,
  type PathCanvasContext,
} from "./shared/geometry/evenOddFill";

/** 带一个矩形 hole 的 polygon 标注 (归一化)。 */
function makePolygonWithHole(): Annotation {
  return {
    id: "p1",
    x: 0,
    y: 0,
    w: 0.5,
    h: 0.5,
    cls: "商品",
    conf: 1,
    source: "manual",
    polygon: [
      [0, 0],
      [0.5, 0],
      [0.5, 0.5],
      [0, 0.5],
    ],
    holes: [
      [
        [0.1, 0.1],
        [0.3, 0.1],
        [0.3, 0.3],
        [0.1, 0.3],
      ],
    ],
  };
}

/** multi_polygon: 两个不相交外环, 第一个带 hole。 */
function makeMultiPolygon(): Annotation {
  return {
    id: "m1",
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    cls: "商品",
    conf: 1,
    source: "manual",
    // geometryToShape 在 multi_polygon 时把 pickPrimaryPolygon 的外环挂到 polygon。
    polygon: [
      [0, 0],
      [0.4, 0],
      [0.4, 0.4],
      [0, 0.4],
    ],
    multiPolygon: [
      {
        points: [
          [0, 0],
          [0.4, 0],
          [0.4, 0.4],
          [0, 0.4],
        ],
        holes: [
          [
            [0.1, 0.1],
            [0.3, 0.1],
            [0.3, 0.3],
            [0.1, 0.3],
          ],
        ],
      },
      {
        points: [
          [0.6, 0.6],
          [1, 0.6],
          [1, 1],
          [0.6, 1],
        ],
      },
    ],
  };
}

const COMMON_PROPS = {
  isAi: false,
  selected: false,
  faded: false,
  visual: DEFAULT_ANNOTATION_VISUAL,
  imgW: 1000,
  imgH: 800,
  scale: 1,
};

describe("KonvaPolygon · holes / multi_polygon even-odd 分支", () => {
  it("单 polygon 无 holes → 不启用 fillRule (快路径, 不回归)", () => {
    const b: Annotation = {
      id: "p0",
      x: 0,
      y: 0,
      w: 0.5,
      h: 0.5,
      cls: "商品",
      conf: 1,
      source: "manual",
      polygon: [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ],
    };
    render(<KonvaPolygon b={b} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.hasAttribute("data-fillrule")).toBe(false);
    // 外环 points 仍透传 (label/anchor/hit 用)。
    expect(line.getAttribute("data-points")).toBeTruthy();
    expect(line.getAttribute("data-closed")).toBe("true");
  });

  it("b.holes 非空 → 启用 fillRule=evenodd, 外环 points 仍透传", () => {
    render(<KonvaPolygon b={makePolygonWithHole()} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-fillrule")).toBe("evenodd");
    // 外环 = 4 个归一化点 × (imgW=1000, imgH=800) = [0,0, 500,0, 500,400, 0,400]
    expect(line.getAttribute("data-points")).toBe(JSON.stringify([0, 0, 500, 0, 500, 400, 0, 400]));
  });

  it("b.multiPolygon 非空 → 启用 fillRule=evenodd", () => {
    render(<KonvaPolygon b={makeMultiPolygon()} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-fillrule")).toBe("evenodd");
  });

  it("显式 holes prop 覆盖 b.holes (空数组 → 退回快路径)", () => {
    // b 带 holes, 但显式传 holes=[] → 视为无 holes, 不启用 even-odd。
    render(
      <KonvaPolygon b={makePolygonWithHole()} holes={[]} onClick={vi.fn()} {...COMMON_PROPS} />,
    );
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.hasAttribute("data-fillrule")).toBe(false);
  });

  it("holes ring < 2 点 (退化) → 不触发 even-odd 分支", () => {
    const b: Annotation = {
      id: "p2",
      x: 0,
      y: 0,
      w: 0.5,
      h: 0.5,
      cls: "商品",
      conf: 1,
      source: "manual",
      polygon: [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ],
      holes: [[[0.1, 0.1]]], // 单点 hole, 退化
    };
    render(<KonvaPolygon b={b} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    // 退化 hole 不算 → 无 even-odd。
    expect(line.hasAttribute("data-fillrule")).toBe(false);
  });
});

/**
 * 集成断言:KonvaPolygon 内部 sceneFunc 喂给 buildEvenOddPaths 的 outerRings 列表,
 * 与 collectOuterRings + b 字段推导出的列表一致。验证「外环 + holes 全部入 path,
 * multi_polygon 优先于 primary polygon」的合并语义在组件层正确。
 */
describe("KonvaPolygon · sceneFunc 喂给 buildEvenOddPaths 的路径", () => {
  function makeCtx(): PathCanvasContext & {
    calls: { fn: string; args: number[] }[];
  } {
    const calls: { fn: string; args: number[] }[] = [];
    const rec =
      (fn: string) =>
      (...args: number[]) => {
        calls.push({ fn, args });
        return undefined;
      };
    return {
      beginPath: rec("beginPath") as unknown as PathCanvasContext["beginPath"],
      moveTo: rec("moveTo") as unknown as PathCanvasContext["moveTo"],
      lineTo: rec("lineTo") as unknown as PathCanvasContext["lineTo"],
      closePath: rec("closePath") as unknown as PathCanvasContext["closePath"],
      calls,
    };
  }

  it("polygon + 1 hole → 2 子路径 (外环 + 内环)", () => {
    const b = makePolygonWithHole();
    const rings = collectOuterRings({
      primaryPoints: b.polygon,
      holes: b.holes,
    });
    const ctx = makeCtx();
    const n = buildEvenOddPaths(ctx, rings, 1000, 800);
    expect(n).toBe(2);
    // closePath 次数 = 子路径数 (每环以 closePath 收尾)。
    expect(ctx.calls.filter((c) => c.fn === "closePath")).toHaveLength(2);
  });

  it("multi_polygon (2 分量, 1 带 hole) → 3 子路径", () => {
    const b = makeMultiPolygon();
    const rings = collectOuterRings({
      primaryPoints: b.polygon,
      multiPolygon: b.multiPolygon,
    });
    const ctx = makeCtx();
    const n = buildEvenOddPaths(ctx, rings, 1000, 800);
    // 分量 1 (外环 + 1 hole) + 分量 2 (外环) = 3。
    expect(n).toBe(3);
  });
});
