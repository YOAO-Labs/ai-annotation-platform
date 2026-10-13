import { describe, expect, it } from "vitest";
import {
  applyMaskBrush,
  applyMaskComponent,
  applyMaskFloodFill,
  applyMaskMorphology,
  applyMaskPolygon,
  fillMaskHoles,
  labelMaskRegions,
  removeSmallMaskComponents,
  smoothMaskBoundary,
} from "./maskOperations";

function alpha(rows: number[][]): Uint8Array {
  return Uint8Array.from(rows.flat().map((value) => (value ? 255 : 0)));
}

function rows(value: Uint8Array, width: number): number[][] {
  const result: number[][] = [];
  for (let offset = 0; offset < value.length; offset += width) {
    result.push([...value.slice(offset, offset + width)].map((pixel) => (pixel ? 1 : 0)));
  }
  return result;
}

describe("Mask operations · brush and polygon", () => {
  it("draws a square brush on a clipped non-square canvas", () => {
    const result = applyMaskBrush(new Uint8Array(15), 5, 3, {
      cx: 0,
      cy: 1,
      radius: 1,
      shape: "square",
      value: 255,
    });

    expect(rows(result.alpha, 5)).toEqual([
      [1, 1, 0, 0, 0],
      [1, 1, 0, 0, 0],
      [1, 1, 0, 0, 0],
    ]);
    expect(result.report).toMatchObject({
      beforeArea: 0,
      afterArea: 6,
      changedPixels: 6,
      bounds: { x0: 0, y0: 0, x1: 2, y1: 3 },
    });
  });

  it("does not expand a square brush by ceil at fractional pointer coordinates", () => {
    const result = applyMaskBrush(new Uint8Array(15), 5, 3, {
      cx: 1.5,
      cy: 1,
      radius: 1,
      shape: "square",
      value: 255,
    });
    expect(rows(result.alpha, 5)).toEqual([
      [0, 1, 1, 0, 0],
      [0, 1, 1, 0, 0],
      [0, 1, 1, 0, 0],
    ]);
  });

  it("uses one pixel-center even-odd rule for polygon add and subtract", () => {
    const added = applyMaskPolygon(new Uint8Array(24), 6, 4, {
      points: [
        [1, 1],
        [5, 1],
        [5, 3],
        [1, 3],
      ],
      value: 255,
    });
    expect(rows(added.alpha, 6)).toEqual([
      [0, 0, 0, 0, 0, 0],
      [0, 1, 1, 1, 1, 0],
      [0, 1, 1, 1, 1, 0],
      [0, 0, 0, 0, 0, 0],
    ]);

    const subtracted = applyMaskPolygon(added.alpha, 6, 4, {
      points: [
        [2, 0],
        [4, 0],
        [4, 4],
        [2, 4],
      ],
      value: 0,
    });
    expect(rows(subtracted.alpha, 6)).toEqual([
      [0, 0, 0, 0, 0, 0],
      [0, 1, 0, 0, 1, 0],
      [0, 1, 0, 0, 1, 0],
      [0, 0, 0, 0, 0, 0],
    ]);
    expect(subtracted.report.changedPixels).toBe(4);
  });

  it("does not mutate the source and reports a no-op polygon", () => {
    const source = alpha([[1, 0, 0]]);
    const result = applyMaskPolygon(source, 3, 1, {
      points: [
        [0, 0],
        [1, 0],
      ],
      value: 0,
    });
    expect([...source]).toEqual([255, 0, 0]);
    expect([...result.alpha]).toEqual([255, 0, 0]);
    expect(result.report.changedPixels).toBe(0);
    expect(result.report.bounds).toBeNull();
  });

  it("rejects brush radii outside the editor contract", () => {
    expect(() =>
      applyMaskBrush(new Uint8Array(1), 1, 1, {
        cx: 0,
        cy: 0,
        radius: 0,
        shape: "circle",
        value: 255,
      }),
    ).toThrow(/radius/);
  });
});

describe("Mask operations · connectivity and membership", () => {
  it("keeps diagonal foreground separate in 4-connectivity and joins it in 8", () => {
    const source = alpha([
      [1, 0, 0, 0],
      [0, 1, 0, 0],
      [0, 0, 0, 1],
    ]);

    const four = labelMaskRegions(source, 4, 3, { value: 255, connectivity: 4 });
    const eight = labelMaskRegions(source, 4, 3, { value: 255, connectivity: 8 });

    expect(four.regions.map((region) => region.area)).toEqual([1, 1, 1]);
    expect(eight.regions.map((region) => region.area)).toEqual([2, 1]);
    expect(four.hit(0, 0)?.id).toBe(1);
    expect(four.hit(1, 1)?.id).toBe(2);
    expect(four.hit(3, 2)?.bounds).toEqual({ x0: 3, y0: 2, x1: 4, y1: 3 });
  });

  it("marks edge background separately from enclosed holes", () => {
    const source = alpha([
      [0, 0, 0, 0, 0, 0],
      [0, 1, 1, 1, 1, 0],
      [0, 1, 0, 0, 1, 0],
      [0, 1, 1, 1, 1, 0],
    ]);
    const background = labelMaskRegions(source, 6, 4, { value: 0, connectivity: 4 });

    expect(background.regions).toHaveLength(2);
    expect(background.regions.find((region) => !region.touchesBoundary)?.area).toBe(2);
    expect(background.hit(2, 2)?.touchesBoundary).toBe(false);
    expect(background.hit(0, 0)?.touchesBoundary).toBe(true);
  });

  it("flood fill changes only the selected 4- or 8-connected region", () => {
    const source = alpha([
      [1, 0, 0],
      [0, 1, 0],
      [0, 0, 0],
    ]);
    const four = applyMaskFloodFill(source, 3, 3, {
      x: 0,
      y: 0,
      value: 0,
      connectivity: 4,
    });
    const eight = applyMaskFloodFill(source, 3, 3, {
      x: 0,
      y: 0,
      value: 0,
      connectivity: 8,
    });

    expect(four.report.changedPixels).toBe(1);
    expect(eight.report.changedPixels).toBe(2);
    expect(four.report.beforeComponents).toBe(2);
    expect(eight.report.beforeComponents).toBe(1);
    expect(four.report.afterComponents).toBe(1);
    expect(eight.report.afterComponents).toBe(0);
    expect(rows(four.alpha, 3)[1][1]).toBe(1);
    expect(rows(eight.alpha, 3)[1][1]).toBe(0);
  });

  it("keeps the hit diagonal component in 4-connectivity while the 8-connected merge makes the same keep a no-op", () => {
    // diagonal_two topology from the Mask advanced E2E matrix: two equal
    // components touching diagonally, so connectivity decides the outcome.
    const source = alpha([
      [1, 1, 0, 0],
      [1, 1, 0, 0],
      [0, 0, 1, 1],
      [0, 0, 1, 1],
    ]);

    const four = applyMaskComponent(source, 4, 4, {
      action: "keep",
      x: 0,
      y: 0,
      connectivity: 4,
    });
    const eight = applyMaskComponent(source, 4, 4, {
      action: "keep",
      x: 0,
      y: 0,
      connectivity: 8,
    });

    expect(rows(four.alpha, 4)).toEqual([
      [1, 1, 0, 0],
      [1, 1, 0, 0],
      [0, 0, 0, 0],
      [0, 0, 0, 0],
    ]);
    expect(four.report.changedPixels).toBe(4);
    expect(four.report.beforeArea).toBe(8);
    expect(four.report.afterArea).toBe(4);
    expect(four.report.beforeComponents).toBe(2);
    expect(four.report.afterComponents).toBe(1);
    // Under 8-connectivity the diagonal blocks are one component: keep is a no-op.
    expect([...eight.alpha]).toEqual([...source]);
    expect(eight.report.changedPixels).toBe(0);
    expect(eight.report.beforeArea).toBe(8);
    expect(eight.report.afterArea).toBe(8);
    expect(eight.report.beforeComponents).toBe(1);
    expect(eight.report.afterComponents).toBe(1);
  });
});

describe("Mask operations · morphology", () => {
  it("dilates with exact square and disk kernels", () => {
    const source = alpha([
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
      [0, 0, 1, 0, 0],
      [0, 0, 0, 0, 0],
      [0, 0, 0, 0, 0],
    ]);
    const square = applyMaskMorphology(source, 5, 5, {
      operation: "dilate",
      kernelShape: "square",
      radius: 1,
    });
    const disk = applyMaskMorphology(source, 5, 5, {
      operation: "dilate",
      kernelShape: "disk",
      radius: 1,
    });

    expect(square.report.afterArea).toBe(9);
    expect(disk.report.afterArea).toBe(5);
  });

  it("treats pixels outside the canvas as background during erosion", () => {
    const source = alpha([
      [1, 1, 1, 1, 1],
      [1, 1, 1, 1, 1],
      [1, 1, 1, 1, 1],
    ]);
    const result = applyMaskMorphology(source, 5, 3, {
      operation: "erode",
      kernelShape: "square",
      radius: 1,
    });

    expect(rows(result.alpha, 5)).toEqual([
      [0, 0, 0, 0, 0],
      [0, 1, 1, 1, 0],
      [0, 0, 0, 0, 0],
    ]);
  });

  it("open removes a spur while close fills a one-pixel hole", () => {
    const spur = alpha([
      [0, 0, 0, 0, 0],
      [0, 1, 1, 1, 0],
      [0, 1, 1, 1, 1],
      [0, 1, 1, 1, 0],
      [0, 0, 0, 0, 0],
    ]);
    const opened = applyMaskMorphology(spur, 5, 5, {
      operation: "open",
      kernelShape: "square",
      radius: 1,
    });
    expect(opened.alpha[2 * 5 + 4]).toBe(0);

    const hole = alpha([
      [0, 0, 0, 0, 0],
      [0, 1, 1, 1, 0],
      [0, 1, 0, 1, 0],
      [0, 1, 1, 1, 0],
      [0, 0, 0, 0, 0],
    ]);
    const closed = applyMaskMorphology(hole, 5, 5, {
      operation: "close",
      kernelShape: "square",
      radius: 1,
    });
    expect(closed.alpha[2 * 5 + 2]).toBe(255);
  });

  it("rejects non-binary input and out-of-contract radii", () => {
    expect(() =>
      applyMaskMorphology(Uint8Array.of(128), 1, 1, {
        operation: "dilate",
        kernelShape: "disk",
        radius: 1,
      }),
    ).toThrow(/binary/);
    expect(() =>
      applyMaskMorphology(Uint8Array.of(255), 1, 1, {
        operation: "dilate",
        kernelShape: "disk",
        radius: 33,
      }),
    ).toThrow(/radius/);
  });

  it("clips a radius-32 kernel on empty and full non-square masks", () => {
    const empty = applyMaskMorphology(new Uint8Array(6), 3, 2, {
      operation: "dilate",
      kernelShape: "disk",
      radius: 32,
    });
    expect(empty.report.afterArea).toBe(0);

    const full = applyMaskMorphology(
      Uint8Array.from({ length: 6 }, () => 255),
      3,
      2,
      {
        operation: "erode",
        kernelShape: "disk",
        radius: 32,
      },
    );
    expect(full.report.afterArea).toBe(0);
  });
});

describe("Mask operations · component and hole editing", () => {
  it("keeps or deletes the foreground component hit by alpha membership", () => {
    const source = alpha([
      [1, 1, 0, 0, 0, 0],
      [1, 1, 0, 1, 1, 0],
      [0, 0, 0, 1, 1, 0],
    ]);

    const kept = applyMaskComponent(source, 6, 3, {
      action: "keep",
      x: 3,
      y: 1,
      connectivity: 4,
    });
    const deleted = applyMaskComponent(source, 6, 3, {
      action: "delete",
      x: 0,
      y: 0,
      connectivity: 4,
    });

    expect(rows(kept.alpha, 6)).toEqual([
      [0, 0, 0, 0, 0, 0],
      [0, 0, 0, 1, 1, 0],
      [0, 0, 0, 1, 1, 0],
    ]);
    expect(rows(deleted.alpha, 6)).toEqual(rows(kept.alpha, 6));
    expect(kept.report.changedPixels).toBe(4);
  });

  it("does not use a component AABB as hit membership", () => {
    const source = alpha([
      [1, 1, 1],
      [1, 0, 1],
      [1, 1, 1],
    ]);
    const result = applyMaskComponent(source, 3, 3, {
      action: "delete",
      x: 1,
      y: 1,
      connectivity: 4,
    });
    expect([...result.alpha]).toEqual([...source]);
    expect(result.report.changedPixels).toBe(0);
  });

  it("removes every foreground component at or below the threshold", () => {
    const source = alpha([
      [1, 0, 0, 1, 1],
      [0, 0, 0, 1, 1],
      [1, 1, 0, 1, 1],
    ]);
    const result = removeSmallMaskComponents(source, 5, 3, {
      maxArea: 2,
      connectivity: 4,
    });
    expect(rows(result.alpha, 5)).toEqual([
      [0, 0, 0, 1, 1],
      [0, 0, 0, 1, 1],
      [0, 0, 0, 1, 1],
    ]);
    expect(result.report.changedPixels).toBe(3);
  });

  it("fills only enclosed background and supports hit / threshold / all modes", () => {
    const source = alpha([
      [0, 0, 0, 0, 0, 0, 0],
      [0, 1, 1, 1, 1, 1, 0],
      [0, 1, 0, 1, 0, 1, 0],
      [0, 1, 1, 1, 0, 1, 0],
      [0, 1, 1, 1, 1, 1, 0],
      [0, 0, 0, 0, 0, 0, 0],
    ]);
    const hit = fillMaskHoles(source, 7, 6, { mode: "hit", x: 2, y: 2 });
    const threshold = fillMaskHoles(source, 7, 6, { mode: "max_area", maxArea: 1 });
    const all = fillMaskHoles(source, 7, 6, { mode: "all" });

    expect(hit.report.changedPixels).toBe(1);
    expect(threshold.report.changedPixels).toBe(1);
    expect(all.report.changedPixels).toBe(3);
    expect(all.alpha[0]).toBe(0);
    expect(fillMaskHoles(source, 7, 6, { mode: "hit", x: 0, y: 0 }).report.changedPixels).toBe(0);
  });

  it("drives the donut-and-islands matrix: hole count, threshold pruning and keep-to-one", () => {
    // donut_three topology from the Mask advanced E2E matrix: a donut with one
    // enclosed hole plus two separate islands (three foreground components).
    const source = alpha([
      [0, 0, 0, 0, 0, 0, 0, 0, 0],
      [0, 1, 1, 1, 1, 1, 1, 1, 0],
      [0, 1, 0, 0, 1, 1, 1, 1, 0],
      [0, 1, 0, 0, 1, 1, 1, 1, 0],
      [0, 1, 1, 1, 1, 1, 1, 1, 0],
      [0, 0, 0, 0, 0, 0, 0, 0, 0],
      [0, 1, 1, 1, 0, 0, 1, 1, 0],
      [0, 1, 0, 0, 0, 0, 0, 1, 0],
      [0, 0, 0, 0, 0, 0, 0, 0, 0],
    ]);
    const labels = labelMaskRegions(source, 9, 9, { value: 255, connectivity: 4 });
    expect(labels.regions.map((region) => region.area)).toEqual([24, 4, 3]);
    const background = labelMaskRegions(source, 9, 9, { value: 0, connectivity: 4 });
    expect(background.regions.find((region) => !region.touchesBoundary)?.area).toBe(4);

    const filled = fillMaskHoles(source, 9, 9, { mode: "all" });
    expect(filled.report.beforeArea).toBe(31);
    expect(filled.report.afterArea).toBe(35);
    expect(filled.report.changedPixels).toBe(4);
    const filledBackground = labelMaskRegions(filled.alpha, 9, 9, {
      value: 0,
      connectivity: 4,
    });
    expect(filledBackground.regions.every((region) => region.touchesBoundary)).toBe(true);

    const pruned = removeSmallMaskComponents(source, 9, 9, { maxArea: 3, connectivity: 4 });
    expect(pruned.report.changedPixels).toBe(3);
    expect(pruned.report.beforeComponents).toBe(3);
    expect(pruned.report.afterComponents).toBe(2);
    expect(pruned.report.afterArea).toBe(28);

    const kept = applyMaskComponent(source, 9, 9, {
      action: "keep",
      x: 1,
      y: 2,
      connectivity: 4,
    });
    expect(kept.report.changedPixels).toBe(7);
    expect(kept.report.beforeComponents).toBe(3);
    expect(kept.report.afterComponents).toBe(1);
    expect(kept.report.afterArea).toBe(24);
    expect(rows(kept.alpha, 9)[6]).toEqual([0, 0, 0, 0, 0, 0, 0, 0, 0]);
  });

  it("smooth is one close-then-open result and keeps the source immutable", () => {
    const source = alpha([
      [0, 0, 0, 0, 0, 0, 0],
      [0, 1, 1, 1, 1, 1, 0],
      [0, 1, 1, 0, 1, 1, 0],
      [0, 1, 1, 1, 1, 1, 0],
      [0, 0, 0, 1, 0, 0, 0],
      [0, 0, 0, 0, 0, 0, 0],
    ]);
    const before = source.slice();
    const result = smoothMaskBoundary(source, 7, 6, { kernelShape: "square", radius: 1 });
    expect([...source]).toEqual([...before]);
    expect(result.alpha[2 * 7 + 3]).toBe(255);
    expect(result.alpha[4 * 7 + 3]).toBe(0);
  });
});
