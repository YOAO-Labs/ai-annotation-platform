/**
 * §C.8-A 邻帧点云逐目标位姿补偿。
 *
 * 的「剔除动态点」(cullDynamicPoints)把落在 tracked box 内的邻帧点丢掉,
 * 干净但动态目标完全不显示。本函数更进一步:对**已标注且跨帧成链**的目标,把它的
 * 邻帧点**搬到当前帧位置**一起渲染——静止背景照旧 ego 加密,动态目标也对齐加密、
 * **无拖影**。等于用已有 track 数据做 lite 版 scene flow。
 *
 * 核心洞察:目标点变换**不需要 ego relMatrix**。邻帧点 p 在邻帧 ego 系,邻帧 box
 * (M_nbr=compose(nbrPsr))与当前 box(M_cur=compose(curPsr))分别在各自 ego 系;
 *   T_obj = M_cur · inv(M_nbr)
 * 直接把 p 从「邻帧 ego 系内该目标的位置」搬到「当前 ego 系内该目标的位置」,同时
 * 隐含了 ego 运动 + 目标自身运动。背景点(不属于任何邻帧 box)才走 relMatrix(ego)。
 *
 * point-in-OBB 判定在**邻帧 ego 系**用原始邻帧 box PSR 做(投影法,复用 box3d,
 * 与 cullDynamicPoints 同一套),不预对齐。输出已是当前帧 ego 系 → 渲染走 identity 矩阵。
 *
 * 纯几何,可在 jsdom 下单测。
 */
import * as THREE from "three";

import { boxAxisWorldDir, boxToMatrix4 } from "./box3d";

type Vec3 = [number, number, number];

export interface AlignPsr {
  center: Vec3;
  size: Vec3;
  rotation: Vec3;
}

/** 邻帧 box:原始邻帧 ego 系 PSR + 跨帧链 track_id。 */
export interface AlignNeighborBox extends AlignPsr {
  trackId: string;
}

export interface PerObjectAlignResult {
  /** 已变换到当前帧 ego 系的邻帧点(背景走 ego,目标点搬到当前位置)。 */
  aligned: Float32Array;
  /** 被搬运(命中可配对目标)的点数。 */
  movedCount: number;
  /** 命中目标但当前帧无配对、按 fallback 处理的点数(cull 时即丢弃数)。 */
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

  // 预计算每个邻帧 box:point-in-OBB 用(中心 + 三轴 + 半边长),以及配对目标的搬运矩阵。
  // target 为 null = 当前帧无同 track 框 → 命中后走 fallback。
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
      // T_obj = M_cur · inv(M_nbr):邻帧 ego 系目标点 → 当前 ego 系目标点。
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

    // 在邻帧 ego 系判定属于哪个目标(原始邻帧 box,不预对齐)。
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
        // 可配对目标:搬到当前帧位置。
        p.applyMatrix4(hit.transform);
        movedCount++;
      } else {
        // 命中目标但当前帧无配对 → fallback。
        fallbackCount++;
        if (fallback === "cull") continue; // 丢弃,避免冒拖影
        p.applyMatrix4(relMatrix); // 退背景(ego)
      }
    } else {
      // 背景点:ego 刚体对齐。
      p.applyMatrix4(relMatrix);
    }

    out[write * 3] = p.x;
    out[write * 3 + 1] = p.y;
    out[write * 3 + 2] = p.z;
    write++;
  }

  return { aligned: out.slice(0, write * 3), movedCount, fallbackCount };
}
