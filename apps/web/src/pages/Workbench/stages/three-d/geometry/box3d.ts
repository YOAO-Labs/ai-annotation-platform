/**
 * 点云 3D 框纯几何 (PSR ↔ 8 角点)。
 *
 * 与 WebGL/renderer 无关 —— 只用 three 的数学类 (Matrix4 / Euler / Vector3),
 * 可在 jsdom 下单测。供主视图渲染框线框 (把 unit cube 经 boxToMatrix4 变换),
 * 以及图像投影 (角点 → 像素)。
 *
 * 坐标系: 点云 Z-up。框用 PSR 表达:
 *   - center   [x, y, z]            米, 框中心世界坐标
 *   - size     [length, width, height] 米, 沿框局部 X / Y / Z 的全边长
 *   - rotation [rx, ry, rz]         弧度, 绕各轴旋转。本期 7-DoF 主要用 yaw = rotation[2] (绕 Z)
 *
 * 欧拉角顺序: **'XYZ'** (THREE.Euler 默认即 'XYZ', 这里显式写出以防默认值变化)。
 * 即先绕 X, 再绕 Y, 最后绕 Z, 应用顺序为内旋 R = Rz · Ry · Rx。
 *
 * 图像投影 (角点 → 相机像素) 必须沿用这里的 'XYZ' 欧拉顺序与
 *   size→local-half-extent 约定; 任何一端改顺序都会让投影框偏移, 改前请同步两端。
 */
import * as THREE from "three";

type Vec3 = readonly [number, number, number];

/**
 * 把 PSR 框编码为一个 Matrix4: 它把「单位立方体」(边长 1、原点居中、
 * 角点为 (±0.5, ±0.5, ±0.5)) 变换成该 3D 框。
 *
 * compose(position, quaternion, scale):
 *   - position = center
 *   - quaternion 来自 Euler(rx, ry, rz, 'XYZ')
 *   - scale = size (因单位立方体边长为 1, 缩放 size 后边长即 size)
 *
 * 渲染用法: 一个 unit `BoxGeometry(1,1,1)` / 其 `EdgesGeometry` 直接
 * `geometry.applyMatrix4(m)`, 或设到 `mesh.matrixAutoUpdate=false; mesh.matrix.copy(m)`。
 */
export function boxToMatrix4(center: Vec3, size: Vec3, rotation: Vec3): THREE.Matrix4 {
  const position = new THREE.Vector3(center[0], center[1], center[2]);
  const euler = new THREE.Euler(rotation[0], rotation[1], rotation[2], "XYZ");
  const quaternion = new THREE.Quaternion().setFromEuler(euler);
  const scale = new THREE.Vector3(size[0], size[1], size[2]);
  return new THREE.Matrix4().compose(position, quaternion, scale);
}

/**
 * 单位立方体的 8 个角点 (±0.5, ±0.5, ±0.5)。
 *
 * 顺序约定 (经 boxToMatrix4 变换后保持一致):
 *   底面 (局部 z = -0.5), 俯视逆时针 (从 -x-y 角起):
 *     0: (-, -, -)  1: (+, -, -)  2: (+, +, -)  3: (-, +, -)
 *   顶面 (局部 z = +0.5), 与底面同 (x, y) 顺序:
 *     4: (-, -, +)  5: (+, -, +)  6: (+, +, +)  7: (-, +, +)
 * 故 0↔4 / 1↔5 / 2↔6 / 3↔7 为同一竖棱的上下端点。
 */
const UNIT_CUBE_CORNERS: ReadonlyArray<Vec3> = [
  [-0.5, -0.5, -0.5],
  [0.5, -0.5, -0.5],
  [0.5, 0.5, -0.5],
  [-0.5, 0.5, -0.5],
  [-0.5, -0.5, 0.5],
  [0.5, -0.5, 0.5],
  [0.5, 0.5, 0.5],
  [-0.5, 0.5, 0.5],
];

/**
 * 返回该 PSR 框的 8 个世界坐标角点。
 *
 * 实现: 对单位立方体角点 (±0.5,…) 应用 boxToMatrix4 —— 矩阵已含 scale=size,
 * 故 ±0.5 经缩放后即半边长 (±length/2, ±width/2, ±height/2), 再旋转 + 平移。
 * 角点顺序见 `UNIT_CUBE_CORNERS` (底面逆时针 4 点, 再顶面 4 点)。
 */
export function psrToCorners(center: Vec3, size: Vec3, rotation: Vec3): THREE.Vector3[] {
  const m = boxToMatrix4(center, size, rotation);
  return UNIT_CUBE_CORNERS.map((c) => new THREE.Vector3(c[0], c[1], c[2]).applyMatrix4(m));
}

/** 局部轴单位向量 (axis: 0=X / 1=Y / 2=Z)。 */
function unitAxis(axis: 0 | 1 | 2): THREE.Vector3 {
  return new THREE.Vector3(axis === 0 ? 1 : 0, axis === 1 ? 1 : 0, axis === 2 ? 1 : 0);
}

/**
 * 框 local 轴在世界系的单位方向 (旋转作用于单位轴, 不含 scale/平移)。
 * 三视图拖边时中心沿该方向移动, 裁剪面法线也由它给出。欧拉顺序与 boxToMatrix4 同 'XYZ'。
 */
export function boxAxisWorldDir(rotation: Vec3, axis: 0 | 1 | 2): THREE.Vector3 {
  const q = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(rotation[0], rotation[1], rotation[2], "XYZ"),
  );
  return unitAxis(axis).applyQuaternion(q).normalize();
}

/**
 * boxToMatrix4 的逆: 世界坐标 → 单位立方体 local 系。
 * 含 scale, 故框内点映到 [-0.5, 0.5]^3 内 (供正交相机 / 命中 / 裁切判断)。
 */
export function worldToBox(center: Vec3, size: Vec3, rotation: Vec3): THREE.Matrix4 {
  return boxToMatrix4(center, size, rotation).invert();
}

/**
 * 框的 6 个世界系裁剪面 (法线指向框内), 喂给 three 点材质 `clippingPlanes`
 * 做 GPU 框内点裁切 (保留满足全部 6 面 `distanceToPoint >= 0` 的点)。
 * margin: 各方向额外放宽米数 (看清框边界外一圈轮廓)。PSR 变只需重算这 6 面。
 */
export function boxLocalClipPlanes(
  center: Vec3,
  size: Vec3,
  rotation: Vec3,
  margin = 0,
): THREE.Plane[] {
  const c = new THREE.Vector3(center[0], center[1], center[2]);
  const planes: THREE.Plane[] = [];
  for (let axis = 0; axis < 3; axis++) {
    const dir = boxAxisWorldDir(rotation, axis as 0 | 1 | 2);
    const half = size[axis] / 2 + margin;
    // +axis 面: 共面点在 center + dir*half, 内法线 = -dir。
    planes.push(
      new THREE.Plane().setFromNormalAndCoplanarPoint(
        dir.clone().negate(),
        c.clone().addScaledVector(dir, half),
      ),
    );
    // -axis 面: 共面点在 center - dir*half, 内法线 = +dir。
    planes.push(
      new THREE.Plane().setFromNormalAndCoplanarPoint(
        dir.clone(),
        c.clone().addScaledVector(dir, -half),
      ),
    );
  }
  return planes;
}
