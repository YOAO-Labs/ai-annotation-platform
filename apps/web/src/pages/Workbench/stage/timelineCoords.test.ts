import { describe, expect, it } from "vitest";
import {
  frameToPct,
  pctToFrame,
  isFullWindow,
  clampWindow,
  zoomWindow,
  panWindow,
  MIN_VISIBLE_SPAN,
  type TimelineWindow,
} from "./timelineCoords";

const FULL: TimelineWindow = { from: 0, to: 100 };

describe("frameToPct / pctToFrame", () => {
  it("maps frame to percent along the full window", () => {
    expect(frameToPct(0, FULL)).toBe(0);
    expect(frameToPct(50, FULL)).toBe(50);
    expect(frameToPct(100, FULL)).toBe(100);
  });

  it("maps within a sub-window (zoomed)", () => {
    const win = { from: 40, to: 60 };
    expect(frameToPct(40, win)).toBe(0);
    expect(frameToPct(50, win)).toBe(50);
    expect(frameToPct(60, win)).toBe(100);
    expect(frameToPct(30, win)).toBeLessThan(0);
    expect(frameToPct(70, win)).toBeGreaterThan(100);
  });

  it("returns 0 when the window has zero/negative span (avoids div-by-zero)", () => {
    expect(frameToPct(5, { from: 10, to: 10 })).toBe(0);
    expect(frameToPct(5, { from: 0, to: 0 })).toBe(0);
  });

  it("reverses pointer ratio back to a rounded, window-clamped frame", () => {
    expect(pctToFrame(0, FULL)).toBe(0);
    expect(pctToFrame(0.5, FULL)).toBe(50);
    expect(pctToFrame(1, FULL)).toBe(100);
    expect(pctToFrame(-0.2, FULL)).toBe(0);
    expect(pctToFrame(1.3, FULL)).toBe(100);
    expect(pctToFrame(0.333, { from: 0, to: 9 })).toBe(3);
  });

  it("round-trips within a sub-window", () => {
    const win = { from: 20, to: 40 };
    const frame = 33;
    const pct = frameToPct(frame, win);
    expect(pctToFrame(pct / 100, win)).toBe(frame);
  });
});

describe("isFullWindow", () => {
  it("is true only when the window covers [0, maxFrame]", () => {
    expect(isFullWindow({ from: 0, to: 100 }, 100)).toBe(true);
    expect(isFullWindow({ from: 10, to: 100 }, 100)).toBe(false);
    expect(isFullWindow({ from: 0, to: 60 }, 100)).toBe(false);
  });

  it("treats an empty video (maxFrame<=0) as full window", () => {
    expect(isFullWindow({ from: 0, to: 0 }, 0)).toBe(true);
  });
});

describe("clampWindow", () => {
  it("keeps an in-bounds window unchanged", () => {
    expect(clampWindow({ from: 20, to: 80 }, 100, MIN_VISIBLE_SPAN)).toEqual({ from: 20, to: 80 });
  });

  it("shifts (not shrinks) a window that overflows the right edge, preserving span", () => {
    expect(clampWindow({ from: 60, to: 120 }, 100, MIN_VISIBLE_SPAN)).toEqual({
      from: 40,
      to: 100,
    });
  });

  it("shifts a window overflowing the left edge to start at 0", () => {
    expect(clampWindow({ from: -30, to: 30 }, 100, MIN_VISIBLE_SPAN)).toEqual({ from: 0, to: 60 });
  });

  it("enforces the minimum span", () => {
    const w = clampWindow({ from: 50, to: 51 }, 100, MIN_VISIBLE_SPAN);
    expect(w.to - w.from).toBe(MIN_VISIBLE_SPAN);
  });

  it("caps span to maxFrame", () => {
    expect(clampWindow({ from: -10, to: 200 }, 100, MIN_VISIBLE_SPAN)).toEqual({
      from: 0,
      to: 100,
    });
  });

  it("returns an empty window for an empty video", () => {
    expect(clampWindow({ from: 0, to: 100 }, 0, MIN_VISIBLE_SPAN)).toEqual({ from: 0, to: 0 });
  });
});

describe("zoomWindow", () => {
  it("keeps the anchor frame at the same pointer ratio (zoom in)", () => {
    const win = { from: 0, to: 100 };
    const anchorRatio = 0.5;
    const next = zoomWindow(win, 100, anchorRatio, 0.5, MIN_VISIBLE_SPAN);
    expect(next.to - next.from).toBeCloseTo(50);
    expect(frameToPct(50, next)).toBeCloseTo(50);
  });

  it("anchors at the pointer even off-center", () => {
    const win = { from: 0, to: 100 };
    const anchorRatio = 0.25;
    const next = zoomWindow(win, 100, anchorRatio, 0.5, MIN_VISIBLE_SPAN);
    expect(frameToPct(25, next)).toBeCloseTo(25);
  });

  it("clamps zoom-out to the full window", () => {
    const win = { from: 40, to: 60 };
    const next = zoomWindow(win, 100, 0.5, 100, MIN_VISIBLE_SPAN);
    expect(next).toEqual({ from: 0, to: 100 });
  });

  it("does not zoom in past the minimum span", () => {
    const win = { from: 0, to: 100 };
    const next = zoomWindow(win, 100, 0.5, 0.0001, MIN_VISIBLE_SPAN);
    expect(next.to - next.from).toBe(MIN_VISIBLE_SPAN);
  });
});

describe("panWindow", () => {
  it("shifts the window by the given frames", () => {
    expect(panWindow({ from: 20, to: 80 }, 100, 10, MIN_VISIBLE_SPAN)).toEqual({
      from: 30,
      to: 90,
    });
  });

  it("clamps at the right edge without shrinking", () => {
    expect(panWindow({ from: 50, to: 100 }, 100, 50, MIN_VISIBLE_SPAN)).toEqual({
      from: 50,
      to: 100,
    });
  });

  it("clamps at the left edge without shrinking", () => {
    expect(panWindow({ from: 0, to: 50 }, 100, -50, MIN_VISIBLE_SPAN)).toEqual({ from: 0, to: 50 });
  });
});
