import { describe, expect, it, vi } from "vitest";
import { guardDrawnBox } from "./drawGuard";
import type { ShapeForIoU } from "./iou";

describe("guardDrawnBox", () => {
  it("通过：正常框原样返回，不弹 toast", () => {
    const toast = vi.fn();
    const g = guardDrawnBox({ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, [], toast);
    expect(g).toEqual({ x: 0.1, y: 0.1, w: 0.2, h: 0.2 });
    expect(toast).not.toHaveBeenCalled();
  });

  it("② 越界 → clamp 回 [0,1]", () => {
    const toast = vi.fn();
    const g = guardDrawnBox({ x: -0.1, y: -0.2, w: 0.5, h: 0.5 }, [], toast);
    expect(g).toEqual({ x: 0, y: 0, w: 0.5, h: 0.5 });
    const g2 = guardDrawnBox({ x: 0.9, y: 0.9, w: 0.3, h: 0.3 }, [], toast);
    expect(g2!.x).toBeCloseTo(0.9, 6);
    expect(g2!.w).toBeCloseTo(0.1, 6);
    expect(g2!.h).toBeCloseTo(0.1, 6);
  });

  it("① 过小（任一边 < 0.005）→ 拒绝 + 提示", () => {
    const toast = vi.fn();
    expect(guardDrawnBox({ x: 0.1, y: 0.1, w: 0.004, h: 0.2 }, [], toast)).toBeNull();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ msg: "框太小未保存", kind: "error" }),
    );
  });

  it("③ 与已有框 IoU > 0.95 → 拒绝 + 提示", () => {
    const toast = vi.fn();
    const existing: ShapeForIoU[] = [{ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }];
    expect(guardDrawnBox({ x: 0.1005, y: 0.1005, w: 0.2, h: 0.2 }, existing, toast)).toBeNull();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ msg: "疑似重复，未保存", kind: "warning" }),
    );
  });

  it("③ 部分重叠（IoU ≤ 0.95）→ 放行", () => {
    const toast = vi.fn();
    const existing: ShapeForIoU[] = [{ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }];
    const g = guardDrawnBox({ x: 0.2, y: 0.1, w: 0.2, h: 0.2 }, existing, toast);
    expect(g).not.toBeNull();
    expect(toast).not.toHaveBeenCalled();
  });

  it("过小闸优先于重复闸（过小时不计算 IoU）", () => {
    const toast = vi.fn();
    const existing: ShapeForIoU[] = [{ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }];
    guardDrawnBox({ x: 0.1, y: 0.1, w: 0.001, h: 0.001 }, existing, toast);
    expect(toast).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ msg: "框太小未保存" }));
  });
});
