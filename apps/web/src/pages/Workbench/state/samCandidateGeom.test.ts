/**
 * SAM 候选外接框 —— 类选择器 popover 的定位依据。
 *
 * 图片与视频两侧共用同一份解析（此前只有图片侧有，视频侧接 popover 时若各写一份，
 * 迟早在几何类型上分叉）。
 */
import { describe, expect, it } from "vitest";
import { samCandidateGeom } from "./useWorkbenchShellModel.helpers";

describe("samCandidateGeom", () => {
  it("矩形候选取其 bbox", () => {
    expect(
      samCandidateGeom({
        type: "rectanglelabels",
        bbox: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
      }),
    ).toEqual({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
  });

  it("多边形候选取顶点外接框", () => {
    const g = samCandidateGeom({
      type: "polygonlabels",
      points: [
        [0.2, 0.3],
        [0.6, 0.3],
        [0.6, 0.9],
        [0.2, 0.9],
      ],
    });
    expect(g!.x).toBeCloseTo(0.2);
    expect(g!.y).toBeCloseTo(0.3);
    expect(g!.w).toBeCloseTo(0.4);
    expect(g!.h).toBeCloseTo(0.6);
  });

  it("顶点不足以成面 → null（不该弹 popover）", () => {
    expect(
      samCandidateGeom({
        type: "polygonlabels",
        points: [
          [0, 0],
          [0.1, 0.1],
        ],
      }),
    ).toBeNull();
  });

  it("矩形候选缺 bbox → null", () => {
    expect(samCandidateGeom({ type: "rectanglelabels" })).toBeNull();
  });

  it("候选不存在 → null（候选被 consume 后 popover 应收起）", () => {
    expect(samCandidateGeom(undefined)).toBeNull();
  });

  it("类型为矩形但带 points → 仍以 bbox 为准", () => {
    expect(
      samCandidateGeom({
        type: "rectanglelabels",
        bbox: { x: 0, y: 0, width: 0.5, height: 0.5 },
        points: [
          [0.9, 0.9],
          [1, 0.9],
          [1, 1],
        ],
      }),
    ).toEqual({ x: 0, y: 0, w: 0.5, h: 0.5 });
  });
});
