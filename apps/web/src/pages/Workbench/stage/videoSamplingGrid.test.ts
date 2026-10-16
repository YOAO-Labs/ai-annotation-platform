import { describe, expect, it } from "vitest";
import { deriveSamplingStep, gridNext, gridPrev, microStep, snapToGrid } from "./videoSamplingGrid";

describe("deriveSamplingStep", () => {
  it("defaults to 1 for nullish / none / empty config", () => {
    expect(deriveSamplingStep(undefined, 30)).toBe(1);
    expect(deriveSamplingStep(null, 30)).toBe(1);
    expect(deriveSamplingStep({ mode: "none" }, 30)).toBe(1);
    expect(deriveSamplingStep({}, 30)).toBe(1);
  });

  it("mode=step uses frame_step", () => {
    expect(deriveSamplingStep({ mode: "step", frame_step: 5 }, 30)).toBe(5);
    expect(deriveSamplingStep({ mode: "step", frame_step: 1 }, 30)).toBe(1);
  });

  it("mode=step falls back to 1 when frame_step missing / invalid", () => {
    expect(deriveSamplingStep({ mode: "step" }, 30)).toBe(1);
    expect(deriveSamplingStep({ mode: "step", frame_step: null }, 30)).toBe(1);
    expect(deriveSamplingStep({ mode: "step", frame_step: 0 }, 30)).toBe(1);
  });

  it("mode=fps derives round(sourceFps/target_fps), min 1", () => {
    expect(deriveSamplingStep({ mode: "fps", target_fps: 5 }, 60)).toBe(12);
    expect(deriveSamplingStep({ mode: "fps", target_fps: 10 }, 30)).toBe(3);
    // target >= source → clamps to 1
    expect(deriveSamplingStep({ mode: "fps", target_fps: 60 }, 30)).toBe(1);
  });

  it("mode=fps non-integer ratio 60→25 rounds to nearest (step=2)", () => {
    expect(deriveSamplingStep({ mode: "fps", target_fps: 25 }, 60)).toBe(2);
  });

  it("mode=fps uses the server ties-to-even rule", () => {
    expect(deriveSamplingStep({ mode: "fps", target_fps: 12 }, 30)).toBe(2);
    expect(deriveSamplingStep({ mode: "fps", target_fps: 20 }, 30)).toBe(2);
    expect(deriveSamplingStep({ mode: "fps", target_fps: 20 }, 70)).toBe(4);
  });

  it("mode=fps falls back to 1 when target / source invalid", () => {
    expect(deriveSamplingStep({ mode: "fps" }, 30)).toBe(1);
    expect(deriveSamplingStep({ mode: "fps", target_fps: null }, 30)).toBe(1);
    expect(deriveSamplingStep({ mode: "fps", target_fps: 10 }, 0)).toBe(1);
  });
});

describe("grid navigation step=1 (degenerate, backward compat)", () => {
  const max = 9;
  it("gridNext ≡ +1 clamped at max", () => {
    expect(gridNext(0, 1, max)).toBe(1);
    expect(gridNext(5, 1, max)).toBe(6);
    expect(gridNext(9, 1, max)).toBe(9);
  });
  it("gridPrev ≡ -1 clamped at 0", () => {
    expect(gridPrev(5, 1, max)).toBe(4);
    expect(gridPrev(0, 1, max)).toBe(0);
  });
  it("snapToGrid is identity", () => {
    expect(snapToGrid(0, 1, max)).toBe(0);
    expect(snapToGrid(7, 1, max)).toBe(7);
    expect(snapToGrid(9, 1, max)).toBe(9);
  });
});

describe("grid navigation step=5 (e.g. 5fps from 25fps)", () => {
  const step = 5;
  const max = 100;

  it("gridNext jumps to next strict-greater grid point", () => {
    expect(gridNext(0, step, max)).toBe(5);
    expect(gridNext(5, step, max)).toBe(10);
    expect(gridNext(37, step, max)).toBe(40);
  });

  it("gridPrev jumps to previous strict-smaller grid point", () => {
    expect(gridPrev(5, step, max)).toBe(0);
    expect(gridPrev(10, step, max)).toBe(5);
    expect(gridPrev(37, step, max)).toBe(35);
  });

  it("off-grid frame f=3 navigates correctly", () => {
    expect(gridNext(3, step, max)).toBe(5);
    expect(gridPrev(3, step, max)).toBe(0);
    expect(snapToGrid(3, step, max)).toBe(5); // round(3/5)=1 → 5
  });

  it("off-grid frame f=2 snaps down", () => {
    expect(snapToGrid(2, step, max)).toBe(0); // round(2/5)=0
  });

  it("snapToGrid rounds to nearest grid point", () => {
    expect(snapToGrid(7, step, max)).toBe(5);
    expect(snapToGrid(8, step, max)).toBe(10);
    expect(snapToGrid(37, step, max)).toBe(35);
    expect(snapToGrid(38, step, max)).toBe(40);
  });

  it("microStep moves ±1 source frame", () => {
    expect(microStep(5, 1, max)).toBe(6);
    expect(microStep(5, -1, max)).toBe(4);
    expect(microStep(37, 1, max)).toBe(38);
  });
});

describe("grid navigation boundaries", () => {
  const step = 5;
  it("gridNext stays on grid at the tail (never lands off-grid maxFrame)", () => {
    // 网格点 0,5,...,95；max=99 时末网格点是 95。
    expect(gridNext(90, step, 99)).toBe(95);
    expect(gridNext(95, step, 99)).toBe(95); // 末网格点，无更靠后网格 → 停住
    expect(gridNext(98, step, 99)).toBe(98); // off-grid 尾部，不前进、也不跳到 99
    expect(gridNext(99, step, 99)).toBe(99);
  });
  it("gridPrev clamps to 0 at the head", () => {
    expect(gridPrev(0, step, 99)).toBe(0);
    expect(gridPrev(1, step, 99)).toBe(0);
  });
  it("snapToGrid clamps both ends", () => {
    expect(snapToGrid(0, step, 99)).toBe(0);
    expect(snapToGrid(99, step, 99)).toBe(99); // round(99/5)*5=100 → clamp 99
  });
  it("microStep clamps both ends", () => {
    expect(microStep(0, -1, 99)).toBe(0);
    expect(microStep(99, 1, 99)).toBe(99);
  });
});

describe("regression: 2fps from 60fps (step=30, max=299) tail does not stick", () => {
  const step = 30;
  const max = 299; // frame_count=300 → maxFrame=299，末网格点是 270
  it("gridNext climbs grid then stops at 270 (no jump to off-grid 299, no stick)", () => {
    expect(gridNext(240, step, max)).toBe(270);
    expect(gridNext(270, step, max)).toBe(270); // 旧 bug：→299，再 →299 卡死
    expect(gridNext(299, step, max)).toBe(299); // 即便误落 299，也不再反复同值卡死
  });
  it("gridPrev from off-grid tail returns to grid", () => {
    expect(gridPrev(299, step, max)).toBe(270);
    expect(gridPrev(270, step, max)).toBe(240);
  });
});
