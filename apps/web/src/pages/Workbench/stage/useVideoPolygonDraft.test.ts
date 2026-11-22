import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { draftCanCommit, draftMinPoints, useVideoPolygonDraft } from "./useVideoPolygonDraft";

describe("useVideoPolygonDraft helpers", () => {
  it("draftMinPoints: polygon(闭合)=3, polyline(开)=2", () => {
    expect(draftMinPoints(true)).toBe(3);
    expect(draftMinPoints(false)).toBe(2);
  });

  it("draftCanCommit: polygon 需 ≥3 点", () => {
    expect(draftCanCommit(null)).toBe(false);
    expect(
      draftCanCommit({
        closed: true,
        points: [
          [0, 0],
          [1, 0],
        ],
      }),
    ).toBe(false);
    expect(
      draftCanCommit({
        closed: true,
        points: [
          [0, 0],
          [1, 0],
          [1, 1],
        ],
      }),
    ).toBe(true);
  });

  it("draftCanCommit: polyline 需 ≥2 点", () => {
    expect(draftCanCommit({ closed: false, points: [[0, 0]] })).toBe(false);
    expect(
      draftCanCommit({
        closed: false,
        points: [
          [0, 0],
          [1, 0],
        ],
      }),
    ).toBe(true);
  });
});

describe("useVideoPolygonDraft.removeLastPoint", () => {
  it("撤销最后一点; 撤空后草稿清为 null", () => {
    const { result } = renderHook(() => useVideoPolygonDraft());
    act(() => {
      result.current.addPoint({ x: 0.1, y: 0.1 }, true);
      result.current.addPoint({ x: 0.2, y: 0.2 }, true);
    });
    expect(result.current.draft?.points).toHaveLength(2);
    act(() => result.current.removeLastPoint());
    expect(result.current.draft?.points).toEqual([[0.1, 0.1]]);
    act(() => result.current.removeLastPoint());
    expect(result.current.draft).toBeNull();
    act(() => result.current.removeLastPoint());
    expect(result.current.draft).toBeNull();
  });
});
