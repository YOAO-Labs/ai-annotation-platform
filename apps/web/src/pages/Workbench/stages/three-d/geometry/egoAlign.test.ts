import { describe, it, expect } from "vitest";
import * as THREE from "three";
import { alignPsrToFrame, frameRelMatrix } from "./egoAlign";
import type { FramePose } from "@/api/generated/types.gen";

function pose(fi: number, x: number, yaw = 0): FramePose {
  return {
    frame_index: fi,
    ego_translation: [x, 0, 0],
    ego_rotation: [Math.cos(yaw / 2), 0, 0, Math.sin(yaw / 2)],
  };
}

describe("alignPsrToFrame", () => {
  it("纯平移: 邻帧框对齐到当前帧后 x 减去 ego 前进量", () => {
    // 邻帧(x=0)里框在 ego 前方 10m;当前帧 ego 已前进到 x=4
    const out = alignPsrToFrame(
      { center: [10, 2, 1], rotation: [0, 0, 0.3] },
      pose(0, 0),
      pose(2, 4),
    );
    expect(out).not.toBeNull();
    expect(out!.center[0]).toBeCloseTo(6);
    expect(out!.center[1]).toBeCloseTo(2);
    expect(out!.center[2]).toBeCloseTo(1);
    expect(out!.rotation[2]).toBeCloseTo(0.3);
  });

  it("ego 旋转 90°: 框位置/朝向随世界系守恒地换系", () => {
    // 邻帧 ego 无旋转, 框在正前方 (10,0,0);当前帧 ego 原地左转 90°
    // → 世界系同一点在当前 ego 系应位于右侧 (0,-10,0)
    const out = alignPsrToFrame(
      { center: [10, 0, 0], rotation: [0, 0, 0] },
      pose(0, 0, 0),
      pose(1, 0, Math.PI / 2),
    );
    expect(out!.center[0]).toBeCloseTo(0);
    expect(out!.center[1]).toBeCloseTo(-10);
    expect(out!.rotation[2]).toBeCloseTo(-Math.PI / 2);
  });

  it("同帧 pose → 恒等", () => {
    const p = pose(0, 3, 0.5);
    const out = alignPsrToFrame({ center: [1, 2, 3], rotation: [0, 0, 1] }, p, p);
    expect(out!.center[0]).toBeCloseTo(1);
    expect(out!.center[1]).toBeCloseTo(2);
    expect(out!.center[2]).toBeCloseTo(3);
    expect(out!.rotation[2]).toBeCloseTo(1);
  });

  it("任一帧缺 pose → null(调用方退回不对齐叠加)", () => {
    const psr = {
      center: [1, 2, 3] as [number, number, number],
      rotation: [0, 0, 0] as [number, number, number],
    };
    expect(alignPsrToFrame(psr, undefined, pose(1, 0))).toBeNull();
    expect(alignPsrToFrame(psr, pose(0, 0), undefined)).toBeNull();
  });
});

describe("frameRelMatrix · 点云叠加", () => {
  it("纯平移: 邻帧静止世界点经 rel 变换后 x 减去 ego 前进量", () => {
    // 邻帧 ego 在 x=0, 当前帧 ego 前进到 x=4;邻帧系里 (10,0,0) 的静止点
    // 在当前帧系应为 (6,0,0)——与 alignPsrToFrame 同口径。
    const rel = frameRelMatrix(pose(0, 0), pose(2, 4));
    expect(rel).not.toBeNull();
    const v = new THREE.Vector3(10, 0, 0).applyMatrix4(rel!);
    expect(v.x).toBeCloseTo(6);
    expect(v.y).toBeCloseTo(0);
    expect(v.z).toBeCloseTo(0);
  });

  it("同帧 pose → 恒等矩阵(点不动)", () => {
    const rel = frameRelMatrix(pose(1, 3, 0.5), pose(1, 3, 0.5));
    const v = new THREE.Vector3(7, -2, 1).applyMatrix4(rel!);
    expect(v.x).toBeCloseTo(7);
    expect(v.y).toBeCloseTo(-2);
    expect(v.z).toBeCloseTo(1);
  });

  it("任一帧缺 pose → null(调用方跳过该邻帧点云)", () => {
    expect(frameRelMatrix(undefined, pose(1, 0))).toBeNull();
    expect(frameRelMatrix(pose(0, 0), undefined)).toBeNull();
  });
});
