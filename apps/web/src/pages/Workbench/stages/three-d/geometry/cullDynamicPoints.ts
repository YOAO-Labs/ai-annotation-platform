/**
 * §C.8-B 邻帧点云动态点剔除。
 *
 * 邻帧点云用 ego-only 刚体补偿对齐到当前帧后,静止背景重合加密、动态目标留拖影
 * 。本函数把落在「当前帧 tracked box」内的邻帧点剔除,只留静止背景——
 * 动态目标的拖影点随之消失。仅对已标注目标有效(未标注动态物无框可判,仍留拖影)。
 *
 * 判定在「当前帧 ego 系」做:邻帧点是 ISO ego(邻帧系)坐标,先经 relMatrix
 * (= inv(T_当前)·T_邻帧,与渲染 GPU 端同一刚体变换)对齐到当前帧 ego 系,再做
 * point-in-OBB。保留的点返回**原始 ISO ego 坐标**(GPU 仍按 relMatrix 渲染,
 * 不改变原有渲染路径)。
 *
 * 纯几何,可在 jsdom 下单测。OBB 测试用投影法(点-中心 在框三条世界轴上的投影
 * 绝对值 ≤ 半边长 + margin),复用 box3d.boxAxisWorldDir,无矩阵求逆。
 */
import * as THREE from "three";

import { boxAxisWorldDir } from "./box3d";

export interface CullBox {
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
}

export interface CullResult {
  /** 剔除动态点后保留的邻帧点(原始 ISO ego 坐标,GPU 仍施加 relMatrix)。 */
  kept: Float32Array;
  /** 被剔除(对齐到当前帧后落在某 box 内)的点数。 */
  culledCount: number;
}

/**
 * 剔除落在任一 box 内的邻帧点。
 *
 * @param positions 邻帧点(ISO ego 系,xyz 连续 Float32Array)
 * @param relMatrix 邻帧→当前帧 ego 的刚体变换(egoAlign.frameRelMatrix)
 * @param boxes 当前帧 tracked box(PSR,当前帧 ego 系)
 * @param opts.margin 框各方向放宽米数(吃标注松边),默认 0
 */
export function cullPointsInBoxes(
  positions: Float32Array,
  relMatrix: THREE.Matrix4,
  boxes: CullBox[],
  opts?: { margin?: number },
): CullResult {
  if (boxes.length === 0) return { kept: positions, culledCount: 0 };
  const margin = opts?.margin ?? 0;

  // 预计算每框:中心向量 + 三条世界轴单位向量 + 半边长(含 margin)。
  const precomp = boxes.map((b) => ({
    center: new THREE.Vector3(b.center[0], b.center[1], b.center[2]),
    axes: [
      boxAxisWorldDir(b.rotation, 0),
      boxAxisWorldDir(b.rotation, 1),
      boxAxisWorldDir(b.rotation, 2),
    ] as const,
    half: [b.size[0] / 2 + margin, b.size[1] / 2 + margin, b.size[2] / 2 + margin] as const,
  }));

  const n = Math.floor(positions.length / 3);
  const kept = new Float32Array(positions.length);
  const p = new THREE.Vector3();
  const d = new THREE.Vector3();
  let write = 0;
  let culled = 0;

  for (let i = 0; i < n; i++) {
    const x = positions[i * 3];
    const y = positions[i * 3 + 1];
    const z = positions[i * 3 + 2];
    p.set(x, y, z).applyMatrix4(relMatrix); // 邻帧 ISO ego → 当前帧 ego 系
    let inside = false;
    for (let b = 0; b < precomp.length; b++) {
      const box = precomp[b];
      d.subVectors(p, box.center);
      if (
        Math.abs(d.dot(box.axes[0])) <= box.half[0] &&
        Math.abs(d.dot(box.axes[1])) <= box.half[1] &&
        Math.abs(d.dot(box.axes[2])) <= box.half[2]
      ) {
        inside = true;
        break;
      }
    }
    if (inside) {
      culled++;
      continue;
    }
    // 保留:写回原始 ISO ego 坐标(渲染仍走 relMatrix)。
    kept[write * 3] = x;
    kept[write * 3 + 1] = y;
    kept[write * 3 + 2] = z;
    write++;
  }

  return { kept: kept.slice(0, write * 3), culledCount: culled };
}
