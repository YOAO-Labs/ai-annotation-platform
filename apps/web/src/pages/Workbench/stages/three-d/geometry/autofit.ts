/**
 * 3D 框自动贴合纯几何 (与 WebGL / renderer 无关, jsdom 可单测)。
 *
 * 坐标系: 点云 Z-up。PSR size 是 **全边长** (与 box3d.ts / triview.ts 一致)。
 *
 * 提供三个原子动作 (均为纯函数, 不修改入参; 返回新 Psr):
 *   - fitSize   保中心 + 朝向, 把 size 收到「框内点 box-local AABB + 2×padding」, size 分量下限 MIN_SIZE。
 *   - fitBottom 保中心 cx/cy + size + 朝向, 把 cz 下移到「框内点 world 系最低 Z = box 下沿」。
 *   - fitYaw    (实验) 保中心 + size + pitch/roll, 仅改 yaw = 框内点 XY 平面 PCA 主轴方向角。
 *
 * inside 判定: 用 quaternion (XYZ 欧拉) 把 world 点反变换到 box-local 米制系
 *   p_local = q⁻¹ · (p - center); 若三分量绝对值都 ≤ size/2 则 inside (与
 *   box3d.boxLocalClipPlanes 同口径)。注意不用 worldToBox —— 它带 scale, 会把点映到 ±0.5,
 *   不便于直接拿 box-local AABB extent (米制)。
 *
 * 框内空 / 点数不足 → 返回原 PSR 不动 (不抛错)。
 */
import * as THREE from "three";

import type { Psr } from "./triview";
import { MIN_SIZE } from "./triview";

type Vec3 = readonly [number, number, number];

/** fitYaw PCA 主轴方向稳定的最少点数 (低于此返回原 psr, 避免稀疏点 PCA 反转)。 */
const FIT_YAW_MIN_POINTS = 20;

/**
 * 把 PSR rotation (XYZ 欧拉) 转成 quaternion 与其逆 (供 inside 判定与坐标系反变换)。
 * 欧拉顺序必须 'XYZ', 与 box3d.ts / triview.ts 同 (任何一端改顺序都会与既有几何脱节)。
 */
function rotationQuaternions(rotation: Vec3): {
  q: THREE.Quaternion;
  qInv: THREE.Quaternion;
} {
  const q = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(rotation[0], rotation[1], rotation[2], "XYZ"),
  );
  return { q, qInv: q.clone().invert() };
}

/**
 * 收集框内点 (用 quaternion 反变换到 box-local 米制, 不走 worldToBox 避开 scale 陷阱)。
 * 返回 inside 点的 world 与 local 坐标 (各为 Float32Array 长度=3N) + 计数。
 */
function collectInsidePoints(
  positions: Float32Array,
  psr: Psr,
): {
  count: number;
  worldX: Float32Array;
  worldY: Float32Array;
  worldZ: Float32Array;
  localX: Float32Array;
  localY: Float32Array;
  localZ: Float32Array;
} {
  const { center, size } = psr;
  const { qInv } = rotationQuaternions(psr.rotation);
  const hx = size[0] / 2;
  const hy = size[1] / 2;
  const hz = size[2] / 2;
  const n = Math.floor(positions.length / 3);
  // 先按总点数分配, 末尾再 slice 到实际 count (避免两次遍历)。
  const wx = new Float32Array(n);
  const wy = new Float32Array(n);
  const wz = new Float32Array(n);
  const lx = new Float32Array(n);
  const ly = new Float32Array(n);
  const lz = new Float32Array(n);
  const tmp = new THREE.Vector3();
  let count = 0;
  for (let i = 0; i < n; i++) {
    const px = positions[i * 3];
    const py = positions[i * 3 + 1];
    const pz = positions[i * 3 + 2];
    tmp.set(px - center[0], py - center[1], pz - center[2]).applyQuaternion(qInv);
    if (Math.abs(tmp.x) <= hx && Math.abs(tmp.y) <= hy && Math.abs(tmp.z) <= hz) {
      wx[count] = px;
      wy[count] = py;
      wz[count] = pz;
      lx[count] = tmp.x;
      ly[count] = tmp.y;
      lz[count] = tmp.z;
      count++;
    }
  }
  return {
    count,
    worldX: wx.subarray(0, count),
    worldY: wy.subarray(0, count),
    worldZ: wz.subarray(0, count),
    localX: lx.subarray(0, count),
    localY: ly.subarray(0, count),
    localZ: lz.subarray(0, count),
  };
}

/** size 各分量 clamp 到 MIN_SIZE 下限。 */
function clampSize(s: Vec3): Vec3 {
  return [Math.max(MIN_SIZE, s[0]), Math.max(MIN_SIZE, s[1]), Math.max(MIN_SIZE, s[2])];
}

/**
 * fit_size: 保朝向 + 收尺寸, **中心对齐到框内点 box-local AABB 中心**
 * (= 旧中心 + R·AABBcenter_local;只有 inside 点云在 box 内对称分布时新中心 == 旧中心,
 * 不对称分布时中心会沿 inside 主体方向漂移以让 size 紧贴点云)。
 * 框内空 / 点数 < 3 → 返回原 PSR 不动。padding 默认 0.05m。
 *
 * 计算: 对每个 world 点 p, p_local = q⁻¹ · (p - center); 仅保留 |p_local.x| ≤ sx/2 ...
 * 的"框内"点 (与 box3d.boxLocalClipPlanes 同口径); 取这些点的 AABB (min/max)。
 * 新中心 = 旧中心 + R · ((AABBmin + AABBmax)/2) (world 系平移到 AABB 中心),
 * 新 size = (AABBmax - AABBmin) + 2×padding。size 各分量下限 MIN_SIZE。
 */
export function fitSize(positions: Float32Array, psr: Psr, padding: number = 0.05): Psr {
  const inside = collectInsidePoints(positions, psr);
  if (inside.count < 3) {
    return { center: psr.center, size: psr.size, rotation: psr.rotation };
  }
  // box-local AABB (米制)。
  let minX = inside.localX[0];
  let maxX = minX;
  let minY = inside.localY[0];
  let maxY = minY;
  let minZ = inside.localZ[0];
  let maxZ = minZ;
  for (let i = 1; i < inside.count; i++) {
    const x = inside.localX[i];
    const y = inside.localY[i];
    const z = inside.localZ[i];
    if (x < minX) minX = x;
    else if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    else if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
    else if (z > maxZ) maxZ = z;
  }
  // box-local AABB 中心 (米制)。
  const cxLocal = (minX + maxX) / 2;
  const cyLocal = (minY + maxY) / 2;
  const czLocal = (minZ + maxZ) / 2;
  // 新 world center = 旧 center + R · AABBcenter_local (旋转作用于 local 偏移)。
  const { q } = rotationQuaternions(psr.rotation);
  const offset = new THREE.Vector3(cxLocal, cyLocal, czLocal).applyQuaternion(q);
  const newCenter: Vec3 = [
    psr.center[0] + offset.x,
    psr.center[1] + offset.y,
    psr.center[2] + offset.z,
  ];
  const newSize = clampSize([
    maxX - minX + 2 * padding,
    maxY - minY + 2 * padding,
    maxZ - minZ + 2 * padding,
  ]);
  return { center: newCenter, size: newSize, rotation: psr.rotation };
}

/**
 * fit_bottom: 保中心 cx/cy + 朝向 + size 不动, 把 cz 下移到「框内点最低 Z = box 下沿」。
 * 框内空 → 返回原 PSR。仅改 center[2]; size[2] 与 rotation 完全不动。
 *
 * 注: 这里"最低 Z"指 **world 系 Z** (点云 Z-up), 不是 box-local 系。直接对 inside 点取 min(world.z),
 * 然后新 cz = z_min_world + sz/2 (让 box 世界下沿 = 点云最低点)。
 *
 * **严格性 contract**: "box 下沿 = z_min" 仅在 box 与世界 Z 共轴 (pitch == roll == 0,只 yaw)
 * 时严格成立；这是车载 LiDAR 数据集（含平台示例集）的通用约束。
 * 若以后支持 box 整体倾斜 (pitch/roll ≠ 0),此公式会让 box 下顶点低于 z_min,需改为用
 * box-local Z 最低面坐标反算 world cz。
 */
export function fitBottom(positions: Float32Array, psr: Psr): Psr {
  const inside = collectInsidePoints(positions, psr);
  if (inside.count === 0) {
    return { center: psr.center, size: psr.size, rotation: psr.rotation };
  }
  let zMin = inside.worldZ[0];
  for (let i = 1; i < inside.count; i++) {
    if (inside.worldZ[i] < zMin) zMin = inside.worldZ[i];
  }
  const newCz = zMin + psr.size[2] / 2;
  return {
    center: [psr.center[0], psr.center[1], newCz],
    size: psr.size,
    rotation: psr.rotation,
  };
}

/**
 * fit_yaw (实验): 保中心 + size + pitch/roll 不动, 仅改 yaw = rotation[2]。
 * 框内点投到 XY 平面做 2D PCA, 主轴方向角 → 新 yaw = atan2(主轴.y, 主轴.x)。
 * 点数 < 20 → 返回原 PSR 不动 (稀疏点 PCA 易反转)。协方差 2x2 用闭式特征值。
 *
 * PCA 口径: 对 inside 点 (world 系) 取均值 → 去中心 → 算 2x2 协方差矩阵
 *   [[Σdx², Σdxdy], [Σdxdy, Σdy²]]
 * → 特征值 λ1 ≥ λ2, 主轴方向 v1 取大特征值对应的特征向量。
 * 闭式解: tr = a+d; det = ad - bc; λ1 = tr/2 + sqrt((tr/2)² - det);
 *         v1 ∝ (b, λ1 - a) 若 b≠0, 否则 (1, 0) 当 a≥d 否则 (0, 1)。
 * 新 yaw = atan2(v1.y, v1.x)；不区分相差 180° 的朝向。
 */
export function fitYaw(positions: Float32Array, psr: Psr): Psr {
  const inside = collectInsidePoints(positions, psr);
  if (inside.count < FIT_YAW_MIN_POINTS) {
    return { center: psr.center, size: psr.size, rotation: psr.rotation };
  }
  // 均值 (world 系 XY)。
  let sumX = 0;
  let sumY = 0;
  for (let i = 0; i < inside.count; i++) {
    sumX += inside.worldX[i];
    sumY += inside.worldY[i];
  }
  const meanX = sumX / inside.count;
  const meanY = sumY / inside.count;
  // 协方差 2x2: a = Σdx², b = c = Σdxdy, d = Σdy² (除以 N 不影响特征向量方向, 省一步)。
  let a = 0;
  let b = 0;
  let d = 0;
  for (let i = 0; i < inside.count; i++) {
    const dx = inside.worldX[i] - meanX;
    const dy = inside.worldY[i] - meanY;
    a += dx * dx;
    b += dx * dy;
    d += dy * dy;
  }
  // 闭式特征值: λ1 ≥ λ2, λ1 = tr/2 + sqrt((tr/2)² - det)。
  const tr = a + d;
  const det = a * d - b * b;
  const disc = Math.max(0, (tr / 2) * (tr / 2) - det);
  const lambda1 = tr / 2 + Math.sqrt(disc);
  // 主轴方向: v1 ∝ (b, λ1 - a) 若 b≠0; 否则按主轴沿 X 或 Y 退化。
  let vx: number;
  let vy: number;
  if (Math.abs(b) > 1e-12) {
    vx = b;
    vy = lambda1 - a;
  } else {
    // 对角矩阵: 大特征值对应方向沿 X (a≥d) 或沿 Y (a<d)。
    if (a >= d) {
      vx = 1;
      vy = 0;
    } else {
      vx = 0;
      vy = 1;
    }
  }
  const newYaw = Math.atan2(vy, vx);
  return {
    center: psr.center,
    size: psr.size,
    rotation: [psr.rotation[0], psr.rotation[1], newYaw],
  };
}

/**
 * 框选选点拟合: 由一组 world 点 (Float32Array, len=3K) 直接取轴对齐 AABB → PSR。
 * center = AABB 中心, size = (max - min) + 2×padding (clamp MIN_SIZE), rotation = [0,0,0]。
 *
 * 用于 frustum 框选 (selectPointsInScreenRect → 本函数): 选中点即用户圈住的真实物体点,
 * 取其包围盒最直接、零视差。yaw 暂取 0 (留 fitYaw / 手调); 朝向斜的物体可建框后按「朝向⚗」。
 * 入参点数应 ≥ 1 (调用方在选不到点时已走兜底, 不会传空数组)。
 */
export function psrFromPoints(points: Float32Array, padding: number = 0.05): Psr {
  let minX = points[0];
  let maxX = minX;
  let minY = points[1];
  let maxY = minY;
  let minZ = points[2];
  let maxZ = minZ;
  const n = Math.floor(points.length / 3);
  for (let i = 1; i < n; i++) {
    const x = points[i * 3];
    const y = points[i * 3 + 1];
    const z = points[i * 3 + 2];
    if (x < minX) minX = x;
    else if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    else if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
    else if (z > maxZ) maxZ = z;
  }
  const size = clampSize([
    maxX - minX + 2 * padding,
    maxY - minY + 2 * padding,
    maxZ - minZ + 2 * padding,
  ]);
  return {
    center: [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2],
    size,
    rotation: [0, 0, 0],
  };
}

/**
 * 便捷: fit_size 后再 fit_bottom (默认连击, Q 键)。不嵌入 fit_yaw。
 */
export function fitSizeAndBottom(positions: Float32Array, psr: Psr, padding: number = 0.05): Psr {
  return fitBottom(positions, fitSize(positions, psr, padding));
}
