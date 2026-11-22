import * as THREE from "three";

import { boxAxisWorldDir, boxToMatrix4 } from "./box3d";

type Vec3 = [number, number, number];

export interface AlignPsr {
  center: Vec3;
  size: Vec3;
  rotation: Vec3;
}

export interface AlignNeighborBox extends AlignPsr {
  trackId: string;
}

export interface PerObjectAlignResult {
  aligned: Float32Array;
  movedCount: number;
  fallbackCount: number;
}

/**
 * 逐目标把邻帧点对齐到当前帧 ego 系。
 *
 * @param positions 邻帧点(邻帧 ISO ego 系,xyz 连续 Float32Array)
 * @param relMatrix 邻帧→当前帧 ego 的刚体变换(egoAlign.frameRelMatrix),背景点用
 * @param neighborBoxes 该邻帧的 box(原始邻帧 ego 系 PSR + trackId)
 * @param currentBoxes 当前帧 box:trackId → PSR(当前帧 ego 系)
 * @param opts.margin 框各方向放宽米数,默认 0(align 宜更紧,避免把背景点搬走)
 * @param opts.fallback 命中目标但当前帧无配对时:"cull" 丢弃(默认,视觉最干净)/ "ego" 退背景
 */
export function alignNeighborPointsPerObject(
  positions: Float32Array,
  relMatrix: THREE.Matrix4,
  neighborBoxes: AlignNeighborBox[],
  currentBoxes: Map<string, AlignPsr>,
  opts?: { margin?: number; fallback?: "cull" | "ego" },
): PerObjectAlignResult {
  const margin = opts?.margin ?? 0;
  const fallback = opts?.fallback ?? "cull";

  const precomp = neighborBoxes.map((b) => {
    const cur = currentBoxes.get(b.trackId);
    return {
      center: new THREE.Vector3(b.center[0], b.center[1], b.center[2]),
      axes: [
        boxAxisWorldDir(b.rotation, 0),
        boxAxisWorldDir(b.rotation, 1),
        boxAxisWorldDir(b.rotation, 2),
      ] as const,
      half: [b.size[0] / 2 + margin, b.size[1] / 2 + margin, b.size[2] / 2 + margin] as const,
      transform: cur
        ? boxToMatrix4(cur.center, cur.size, cur.rotation).multiply(
            boxToMatrix4(b.center, b.size, b.rotation).invert(),
          )
        : null,
    };
  });

  const n = Math.floor(positions.length / 3);
  const out = new Float32Array(positions.length);
  const p = new THREE.Vector3();
  const d = new THREE.Vector3();
  let write = 0;
  let movedCount = 0;
  let fallbackCount = 0;

  for (let i = 0; i < n; i++) {
    const x = positions[i * 3];
    const y = positions[i * 3 + 1];
    const z = positions[i * 3 + 2];
    p.set(x, y, z);

    let hit: (typeof precomp)[number] | null = null;
    for (let b = 0; b < precomp.length; b++) {
      const box = precomp[b];
      d.subVectors(p, box.center);
      if (
        Math.abs(d.dot(box.axes[0])) <= box.half[0] &&
        Math.abs(d.dot(box.axes[1])) <= box.half[1] &&
        Math.abs(d.dot(box.axes[2])) <= box.half[2]
      ) {
        hit = box;
        break;
      }
    }

    if (hit) {
      if (hit.transform) {
        p.applyMatrix4(hit.transform);
        movedCount++;
      } else {
        fallbackCount++;
        if (fallback === "cull") continue;
        p.applyMatrix4(relMatrix);
      }
    } else {
      p.applyMatrix4(relMatrix);
    }

    out[write * 3] = p.x;
    out[write * 3 + 1] = p.y;
    out[write * 3 + 2] = p.z;
    write++;
  }

  return { aligned: out.slice(0, write * 3), movedCount, fallbackCount };
}
