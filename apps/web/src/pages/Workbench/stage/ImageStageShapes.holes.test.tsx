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
    expect(line.getAttribute("data-points")).toBeTruthy();
    expect(line.getAttribute("data-closed")).toBe("true");
  });

  it("b.holes 非空 → 启用 fillRule=evenodd, 外环 points 仍透传", () => {
    render(<KonvaPolygon b={makePolygonWithHole()} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-fillrule")).toBe("evenodd");
    expect(line.getAttribute("data-points")).toBe(JSON.stringify([0, 0, 500, 0, 500, 400, 0, 400]));
  });

  it("b.multiPolygon 非空 → 启用 fillRule=evenodd", () => {
    render(<KonvaPolygon b={makeMultiPolygon()} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.getAttribute("data-fillrule")).toBe("evenodd");
  });

  it("显式 holes prop 覆盖 b.holes (空数组 → 退回快路径)", () => {
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
      holes: [[[0.1, 0.1]]],
    };
    render(<KonvaPolygon b={b} onClick={vi.fn()} {...COMMON_PROPS} />);
    const line = document.querySelector('[data-konva="Line"]')!;
    expect(line.hasAttribute("data-fillrule")).toBe(false);
  });
});

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
    expect(n).toBe(3);
  });
});
