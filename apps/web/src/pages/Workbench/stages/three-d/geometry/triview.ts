import * as THREE from "three";

import { boxAxisWorldDir } from "./box3d";

type Vec3 = readonly [number, number, number];

export interface Psr {
  center: Vec3;
  size: Vec3;
  rotation: Vec3;
}

export type TriView = "top" | "side" | "front";

export const VIEW_AXES: Record<TriView, { u: 0 | 1 | 2; v: 0 | 1 | 2; normal: 0 | 1 | 2 }> = {
  top: { u: 0, v: 1, normal: 2 },
  side: { u: 0, v: 2, normal: 1 },
  front: { u: 1, v: 2, normal: 0 },
};

export const MIN_SIZE = 0.05;

export const FRAME_MARGIN = 0.6;

export const TRI_ZOOM_DEFAULT = 1;
export const TRI_ZOOM_STEP = 1.12;
export const TRI_ZOOM_MIN = 0.5;
export const TRI_ZOOM_MAX = 8;

export function clampTriZoom(zoom: number): number {
  if (!Number.isFinite(zoom)) return TRI_ZOOM_DEFAULT;
  return Math.min(TRI_ZOOM_MAX, Math.max(TRI_ZOOM_MIN, zoom));
}

export function stepTriZoom(zoom: number, direction: 1 | -1): number {
  return clampTriZoom(zoom * (direction > 0 ? TRI_ZOOM_STEP : 1 / TRI_ZOOM_STEP));
}

function toVec3(v: THREE.Vector3): Vec3 {
  return [v.x, v.y, v.z];
}

export function frameOrtho(
  size: Vec3,
  view: TriView,
  aspect: number,
  margin = FRAME_MARGIN,
  zoom = TRI_ZOOM_DEFAULT,
): { halfW: number; halfH: number } {
  const { u, v } = VIEW_AXES[view];
  const halfU = size[u] / 2 + margin;
  const halfV = size[v] / 2 + margin;
  const safeZoom = clampTriZoom(zoom);
  if (halfU / halfV > aspect) {
    return { halfW: halfU / safeZoom, halfH: halfU / aspect / safeZoom };
  }
  return { halfW: (halfV * aspect) / safeZoom, halfH: halfV / safeZoom };
}

/**
 * 拖一条边 (对侧固定)。
 * @param axis   box-local 轴 (0=X / 1=Y / 2=Z)
 * @param dir    拖的是 +axis 侧 (+1) 还是 -axis 侧 (-1)
 * @param dMeter 该边沿 local **+axis 世界方向** 的带符号位移 (米); 由上层把屏幕 delta / 缩放投到 +axis 得到
 *
 * 全边长 size[axis] += dir*dMeter (clamp 到 MIN_SIZE); 中心沿世界 +axis 方向移 dir*applied/2。
 */
export function dragEdge(psr: Psr, axis: 0 | 1 | 2, dir: 1 | -1, dMeter: number): Psr {
  const newSize = Math.max(MIN_SIZE, psr.size[axis] + dir * dMeter);
  const applied = newSize - psr.size[axis];
  const w = boxAxisWorldDir(psr.rotation, axis);
  const c = new THREE.Vector3(psr.center[0], psr.center[1], psr.center[2]).addScaledVector(
    w,
    (dir * applied) / 2,
  );
  const size: [number, number, number] = [psr.size[0], psr.size[1], psr.size[2]];
  size[axis] = newSize;
  return { center: toVec3(c), size, rotation: psr.rotation };
}

export function dragCorner(
  psr: Psr,
  axisU: 0 | 1 | 2,
  dirU: 1 | -1,
  dMeterU: number,
  axisV: 0 | 1 | 2,
  dirV: 1 | -1,
  dMeterV: number,
): Psr {
  return dragEdge(dragEdge(psr, axisU, dirU, dMeterU), axisV, dirV, dMeterV);
}

export type Handle = "e" | "w" | "n" | "s" | "ne" | "nw" | "se" | "sw";

/**
 * 把一次屏幕 handle 拖拽映射到边/角拖拽 (UI 不写裸数学, 全收口于此)。
 * @param dU 指针沿屏幕 +u (= box-local +u 世界方向) 的位移, 米 (= 屏幕 dx_px / s)
 * @param dV 指针沿屏幕 +v (= box-local +v 世界方向) 的位移, 米 (= −屏幕 dy_px / s, 屏幕 y 朝下)
 *
 * 应以**拖拽起始 PSR** 为输入、传入"相对起点的累计位移", 因 dragEdge 含 clamp 非增量幂等。
 */
export function dragHandle(psr: Psr, view: TriView, handle: Handle, dU: number, dV: number): Psr {
  const { u, v } = VIEW_AXES[view];
  switch (handle) {
    case "e":
      return dragEdge(psr, u, 1, dU);
    case "w":
      return dragEdge(psr, u, -1, dU);
    case "n":
      return dragEdge(psr, v, 1, dV);
    case "s":
      return dragEdge(psr, v, -1, dV);
    case "ne":
      return dragCorner(psr, u, 1, dU, v, 1, dV);
    case "nw":
      return dragCorner(psr, u, -1, dU, v, 1, dV);
    case "se":
      return dragCorner(psr, u, 1, dU, v, -1, dV);
    case "sw":
      return dragCorner(psr, u, -1, dU, v, -1, dV);
  }
}

export function dragRotation(psr: Psr, view: TriView, deltaTheta: number): Psr {
  const axis = VIEW_AXES[view].normal;
  const qOld = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(psr.rotation[0], psr.rotation[1], psr.rotation[2], "XYZ"),
  );
  const e = new THREE.Vector3(axis === 0 ? 1 : 0, axis === 1 ? 1 : 0, axis === 2 ? 1 : 0);
  const qDelta = new THREE.Quaternion().setFromAxisAngle(e, deltaTheta);
  const qNew = qOld.multiply(qDelta);
  const euler = new THREE.Euler().setFromQuaternion(qNew, "XYZ");
  return {
    center: psr.center,
    size: psr.size,
    rotation: [euler.x, euler.y, euler.z],
  };
}
