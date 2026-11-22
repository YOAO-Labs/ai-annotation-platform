import * as THREE from "three";

import { boxAxisWorldDir } from "./box3d";

export interface CullBox {
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
}

export interface CullResult {
  kept: Float32Array;
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
    p.set(x, y, z).applyMatrix4(relMatrix);
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
    kept[write * 3] = x;
    kept[write * 3 + 1] = y;
    kept[write * 3 + 2] = z;
    write++;
  }

  return { kept: kept.slice(0, write * 3), culledCount: culled };
}
