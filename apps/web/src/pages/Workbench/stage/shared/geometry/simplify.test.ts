import { describe, expect, it } from "vitest";
import { epsilonForScale, simplifyPolygon } from "./simplify";
import type { Pt } from "./polygon";

describe("simplifyPolygon", () => {
  it("returns copy unchanged when n<4 or epsilon<=0", () => {
    const tri: Pt[] = [
      [0, 0],
      [1, 0],
      [0.5, 1],
    ];
    expect(simplifyPolygon(tri, 0.1)).toEqual(tri);
    expect(simplifyPolygon(tri, 0.1)).not.toBe(tri);

    const sq: Pt[] = [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
    ];
    expect(simplifyPolygon(sq, 0)).toEqual(sq);
  });

  it("drops near-collinear midpoints under threshold", () => {
    const pts: Pt[] = [
      [0, 0],
      [0.5, 0.001],
      [1, 0],
      [1, 1],
      [0, 1],
    ];
    const r = simplifyPolygon(pts, 0.01);
    expect(r.length).toBe(4);
    expect(r).toContainEqual([0, 0]);
    expect(r).toContainEqual([1, 0]);
    expect(r).toContainEqual([1, 1]);
    expect(r).toContainEqual([0, 1]);
  });

  it("keeps midpoints that deviate beyond threshold", () => {
    const pts: Pt[] = [
      [0, 0],
      [0.5, 0.2],
      [1, 0],
      [1, 1],
      [0, 1],
    ];
    const r = simplifyPolygon(pts, 0.05);
    expect(r.length).toBe(5);
  });

  it("preserves a polygon at least 3 points wide", () => {
    const pts: Pt[] = [
      [0, 0],
      [0.1, 0],
      [0.2, 0],
      [0.3, 0],
      [0.4, 0],
    ];
    const r = simplifyPolygon(pts, 0.1);
    expect(r.length).toBeGreaterThanOrEqual(3);
  });

  it("massively reduces a dense circle", () => {
    const n = 200;
    const pts: Pt[] = [];
    for (let i = 0; i < n; i++) {
      const t = (i / n) * Math.PI * 2;
      pts.push([0.5 + 0.4 * Math.cos(t), 0.5 + 0.4 * Math.sin(t)]);
    }
    const r = simplifyPolygon(pts, 0.01);
    expect(r.length).toBeLessThan(n / 2);
    expect(r.length).toBeGreaterThanOrEqual(8);
  });
});

describe("epsilonForScale", () => {
  it("returns 1/(scale*dim) for positive inputs", () => {
    expect(epsilonForScale(2, 1000)).toBeCloseTo(0.0005);
    expect(epsilonForScale(1, 500)).toBeCloseTo(0.002);
  });

  it("returns 0 for non-positive inputs", () => {
    expect(epsilonForScale(0, 1000)).toBe(0);
    expect(epsilonForScale(2, 0)).toBe(0);
    expect(epsilonForScale(-1, 1000)).toBe(0);
  });
});
