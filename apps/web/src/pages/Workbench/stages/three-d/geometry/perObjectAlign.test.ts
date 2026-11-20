// §C.8-A perObjectAlign 纯函数单测:背景走 ego、目标搬到当前位置、
// 未配对走 fallback、identity/同位姿恒等、旋转随当前 box。
import { describe, expect, it } from "vitest";
import * as THREE from "three";

import {
  alignNeighborPointsPerObject,
  type AlignNeighborBox,
  type AlignPsr,
} from "./perObjectAlign";

type Vec3 = [number, number, number];

function f32(...xyz: number[]): Float32Array {
  return new Float32Array(xyz);
}

function pt(out: Float32Array, i: number): Vec3 {
  return [out[i * 3], out[i * 3 + 1], out[i * 3 + 2]];
}

function curMap(entries: [string, AlignPsr][]): Map<string, AlignPsr> {
  return new Map(entries);
}

const I = new THREE.Matrix4(); // identity

describe("alignNeighborPointsPerObject", () => {
  it("无邻帧 box 时,所有点按 relMatrix 走背景", () => {
    const rel = new THREE.Matrix4().makeTranslation(5, 0, 0);
    const res = alignNeighborPointsPerObject(f32(1, 2, 3), rel, [], curMap([]));
    expect(res.movedCount).toBe(0);
    expect(res.fallbackCount).toBe(0);
    expect(pt(res.aligned, 0)).toEqual([6, 2, 3]);
  });

  it("框外背景点:经 relMatrix 平移,不受目标影响", () => {
    const rel = new THREE.Matrix4().makeTranslation(0, 0, 10);
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_1", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    const cur = curMap([["trk_1", { center: [50, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] }]]);
    // 点 [20,0,0] 远在邻帧框外 → 背景。
    const res = alignNeighborPointsPerObject(f32(20, 0, 0), rel, nbr, cur);
    expect(res.movedCount).toBe(0);
    const [x, y, z] = pt(res.aligned, 0);
    expect([x, y, z]).toEqual([20, 0, 10]);
  });

  it("可配对目标:邻帧框中心点搬到当前框中心(纯平移)", () => {
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_1", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    const cur = curMap([["trk_1", { center: [10, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] }]]);
    const res = alignNeighborPointsPerObject(f32(0, 0, 0), I, nbr, cur);
    expect(res.movedCount).toBe(1);
    const [x, y, z] = pt(res.aligned, 0);
    expect(x).toBeCloseTo(10, 5);
    expect(y).toBeCloseTo(0, 5);
    expect(z).toBeCloseTo(0, 5);
  });

  it("可配对目标:框内偏移随目标一并搬运(保持相对位置)", () => {
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_1", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    const cur = curMap([["trk_1", { center: [10, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] }]]);
    // 邻帧框内前端 [1,0,0] → 当前框前端 [11,0,0](同尺寸纯平移)。
    const res = alignNeighborPointsPerObject(f32(1, 0, 0), I, nbr, cur);
    const [x, y, z] = pt(res.aligned, 0);
    expect(x).toBeCloseTo(11, 5);
    expect(y).toBeCloseTo(0, 5);
    expect(z).toBeCloseTo(0, 5);
  });

  it("当前框旋转:框内偏移随当前姿态旋转(目标转向)", () => {
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_1", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    // 当前帧目标 yaw 90°。
    const cur = curMap([
      ["trk_1", { center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, Math.PI / 2] }],
    ]);
    // 邻帧 +X 前端 [1,0,0] → 当前帧应朝 +Y。
    const res = alignNeighborPointsPerObject(f32(1, 0, 0), I, nbr, cur);
    const [x, y, z] = pt(res.aligned, 0);
    expect(x).toBeCloseTo(0, 5);
    expect(y).toBeCloseTo(1, 5);
    expect(z).toBeCloseTo(0, 5);
  });

  it("命中邻帧目标但当前帧无配对:fallback=cull(默认)丢弃", () => {
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_7", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    const res = alignNeighborPointsPerObject(f32(0, 0, 0), I, nbr, curMap([]));
    expect(res.movedCount).toBe(0);
    expect(res.fallbackCount).toBe(1);
    expect(res.aligned.length).toBe(0); // 被剔除
  });

  it("命中邻帧目标但无配对:fallback=ego 退背景(保留,走 relMatrix)", () => {
    const rel = new THREE.Matrix4().makeTranslation(0, 0, 3);
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_7", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    const res = alignNeighborPointsPerObject(f32(0, 0, 0), rel, nbr, curMap([]), {
      fallback: "ego",
    });
    expect(res.fallbackCount).toBe(1);
    expect(pt(res.aligned, 0)).toEqual([0, 0, 3]);
  });

  it("relMatrix=identity 且邻帧/当前同位姿 → 目标点恒等不变", () => {
    const psr: AlignPsr = { center: [3, 4, 1], size: [4, 2, 2], rotation: [0, 0, 0.3] };
    const nbr: AlignNeighborBox[] = [{ trackId: "trk_1", ...psr }];
    const cur = curMap([["trk_1", psr]]);
    const res = alignNeighborPointsPerObject(f32(3.5, 4, 1), I, nbr, cur);
    expect(res.movedCount).toBe(1);
    const [x, y, z] = pt(res.aligned, 0);
    expect(x).toBeCloseTo(3.5, 5);
    expect(y).toBeCloseTo(4, 5);
    expect(z).toBeCloseTo(1, 5);
  });

  it("混合:背景点走 ego,目标点搬运,各自计数正确", () => {
    const rel = new THREE.Matrix4().makeTranslation(100, 0, 0);
    const nbr: AlignNeighborBox[] = [
      { trackId: "trk_1", center: [0, 0, 0], size: [4, 2, 2], rotation: [0, 0, 0] },
    ];
    const cur = curMap([["trk_1", { center: [0, 5, 0], size: [4, 2, 2], rotation: [0, 0, 0] }]]);
    // p0 = 目标内 [0,0,0];p1 = 框外背景 [50,50,50]。
    const res = alignNeighborPointsPerObject(f32(0, 0, 0, 50, 50, 50), rel, nbr, cur);
    expect(res.movedCount).toBe(1);
    expect(res.fallbackCount).toBe(0);
    // 目标点搬到当前框中心 [0,5,0](不经 relMatrix)。
    const [x0, y0, z0] = pt(res.aligned, 0);
    expect(x0).toBeCloseTo(0, 5);
    expect(y0).toBeCloseTo(5, 5);
    expect(z0).toBeCloseTo(0, 5);
    // 背景点经 relMatrix(+100 X)。
    expect(pt(res.aligned, 1)).toEqual([150, 50, 50]);
  });
});
