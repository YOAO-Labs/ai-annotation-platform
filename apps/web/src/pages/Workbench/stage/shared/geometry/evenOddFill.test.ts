import { describe, expect, it, vi } from "vitest";
import {
  buildEvenOddPaths,
  collectOuterRings,
  drawEvenOddShape,
  type PathCanvasContext,
  type PolygonRing,
} from "./evenOddFill";

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

function countSubpaths(ctx: { calls: { fn: string; args: number[] }[] }): number {
  return ctx.calls.filter((c) => c.fn === "closePath").length;
}

describe("buildEvenOddPaths", () => {
  it("单外环无 holes → 1 个子路径, 坐标按 imgW/imgH 换算", () => {
    const ctx = makeCtx();
    const ring: PolygonRing = {
      points: [
        [0, 0],
        [0.5, 0],
        [0.5, 0.5],
        [0, 0.5],
      ],
    };
    const n = buildEvenOddPaths(ctx, [ring], 1000, 800);
    expect(n).toBe(1);
    expect(countSubpaths(ctx)).toBe(1);
    const moveTo0 = ctx.calls.find((c) => c.fn === "moveTo");
    expect(moveTo0?.args).toEqual([0, 0]);
    const lineTos = ctx.calls.filter((c) => c.fn === "lineTo");
    expect(lineTos[0]?.args).toEqual([500, 0]);
    expect(lineTos[1]?.args).toEqual([500, 400]);
    expect(lineTos[2]?.args).toEqual([0, 400]);
  });

  it("单外环 + 1 个 hole → 2 个子路径 (外环 + 内环)", () => {
    const ctx = makeCtx();
    const ring: PolygonRing = {
      points: [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ],
      holes: [
        [
          [0.25, 0.25],
          [0.75, 0.25],
          [0.75, 0.75],
          [0.25, 0.75],
        ],
      ],
    };
    const n = buildEvenOddPaths(ctx, [ring], 100, 100);
    expect(n).toBe(2);
    expect(countSubpaths(ctx)).toBe(2);
  });

  it("multi_polygon 2 个分量 (各带 1 hole) → 4 个子路径", () => {
    const ctx = makeCtx();
    const rings: PolygonRing[] = [
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
        holes: [
          [
            [0.7, 0.7],
            [0.9, 0.7],
            [0.9, 0.9],
            [0.7, 0.9],
          ],
        ],
      },
    ];
    const n = buildEvenOddPaths(ctx, rings, 100, 100);
    expect(n).toBe(4);
    expect(countSubpaths(ctx)).toBe(4);
  });

  it("退化: 外环 < 2 点或空 → 跳过, 不污染 path", () => {
    const ctx = makeCtx();
    const n = buildEvenOddPaths(
      ctx,
      [
        { points: [[0, 0]] },
        { points: [] },
        {
          points: [
            [0, 0],
            [0.5, 0],
            [0.5, 0.5],
          ],
        },
      ],
      100,
      100,
    );
    expect(n).toBe(1);
    expect(countSubpaths(ctx)).toBe(1);
  });

  it("holes 中 < 2 点的环被跳过, 外环仍画", () => {
    const ctx = makeCtx();
    const ring: PolygonRing = {
      points: [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ],
      holes: [[[0.1, 0.1]], []],
    };
    const n = buildEvenOddPaths(ctx, [ring], 100, 100);
    expect(n).toBe(1);
  });

  it("空 outerRings → 0 子路径, 无任何 ctx 调用", () => {
    const ctx = makeCtx();
    const n = buildEvenOddPaths(ctx, [], 100, 100);
    expect(n).toBe(0);
    expect(ctx.calls).toHaveLength(0);
  });
});

describe("drawEvenOddShape", () => {
  it("hole 边界同时进入填充与描边路径", () => {
    const base = makeCtx();
    const fillShape = vi.fn();
    const strokeShape = vi.fn();
    const ctx = { ...base, fillShape, strokeShape };
    const shape = { id: "polygon-with-hole" };
    const rings: PolygonRing[] = [
      {
        points: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
        holes: [
          [
            [0.2, 0.2],
            [0.8, 0.2],
            [0.8, 0.8],
            [0.2, 0.8],
          ],
        ],
      },
    ];

    const result = drawEvenOddShape(ctx, shape, rings, 100, 100);

    expect(result).toEqual({ fillSubpaths: 2, strokeSubpaths: 2 });
    expect(base.calls.filter((call) => call.fn === "beginPath")).toHaveLength(2);
    expect(base.calls.filter((call) => call.fn === "closePath")).toHaveLength(4);
    expect(fillShape).toHaveBeenCalledWith(shape);
    expect(strokeShape).toHaveBeenCalledWith(shape);
  });
});

describe("collectOuterRings", () => {
  it("multiPolygon 优先: 不重复加入 primaryPoints", () => {
    const rings = collectOuterRings({
      primaryPoints: [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
      multiPolygon: [
        {
          points: [
            [0, 0],
            [0.5, 0],
            [0.5, 0.5],
          ],
        },
      ],
    });
    expect(rings).toHaveLength(1);
    expect(rings[0].points).toEqual([
      [0, 0],
      [0.5, 0],
      [0.5, 0.5],
    ]);
  });

  it("无 multiPolygon 时 primaryPoints + holes 作为唯一外环", () => {
    const rings = collectOuterRings({
      primaryPoints: [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ],
      holes: [
        [
          [0.1, 0.1],
          [0.2, 0.1],
          [0.2, 0.2],
        ],
      ],
    });
    expect(rings).toHaveLength(1);
    expect(rings[0].holes).toHaveLength(1);
  });

  it("primaryPoints < 2 且无 multiPolygon → 空数组", () => {
    expect(collectOuterRings({ primaryPoints: [[0, 0]] })).toEqual([]);
    expect(collectOuterRings({})).toEqual([]);
  });
});
