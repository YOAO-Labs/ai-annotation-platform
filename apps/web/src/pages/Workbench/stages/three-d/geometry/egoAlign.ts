/**
 * 邻帧参考框的 ego 对齐。
 *
 * 邻帧框 PSR 在"邻帧 ego 系";叠加到当前帧前先经世界系变换到"当前帧 ego 系",
 * 使静止物的历史/未来参考框与当前帧重合(而非沿 ego 运动方向拖出一串影子)。
 * 数学与后端 services/ego_transform.py 同契约:pose 为 ego→global,
 * 框变换 = inv(T_cur) @ T_nbr;euler 用 three.js 默认 XYZ 序。
 */
import * as THREE from "three";
import type { FramePose } from "@/api/generated/types.gen";

export interface AlignablePsr {
  center: [number, number, number];
  rotation: [number, number, number];
}

function poseToMatrix(pose: FramePose): THREE.Matrix4 {
  const [w, x, y, z] = pose.ego_rotation;
  const [tx, ty, tz] = pose.ego_translation;
  return new THREE.Matrix4().compose(
    new THREE.Vector3(tx, ty, tz),
    new THREE.Quaternion(x, y, z, w), // THREE 构造序是 (x,y,z,w);后端存 wxyz
    new THREE.Vector3(1, 1, 1),
  );
}

/**
 * 邻帧→当前帧的相对刚体变换 inv(T_cur) @ T_nbr。
 * 框对齐(alignPsrToFrame)与点云对齐(邻帧点云叠加)共用同一矩阵。
 * 任一帧缺 pose → null(调用方退回不对齐 / 不叠加)。
 */
export function frameRelMatrix(
  fromPose: FramePose | undefined,
  toPose: FramePose | undefined,
): THREE.Matrix4 | null {
  if (!fromPose || !toPose) return null;
  return poseToMatrix(toPose).invert().multiply(poseToMatrix(fromPose));
}

/**
 * 把"邻帧 ego 系"的框 PSR 对齐到"当前帧 ego 系"。
 * 任一帧缺 pose 则返回 null，调用方退回不对齐的叠加。
 */
export function alignPsrToFrame(
  psr: AlignablePsr,
  fromPose: FramePose | undefined,
  toPose: FramePose | undefined,
): AlignablePsr | null {
  const rel = frameRelMatrix(fromPose, toPose);
  if (!rel) return null;

  const center = new THREE.Vector3(...psr.center).applyMatrix4(rel);
  const rot = new THREE.Quaternion()
    .setFromRotationMatrix(rel)
    .multiply(new THREE.Quaternion().setFromEuler(new THREE.Euler(...psr.rotation, "XYZ")));
  const euler = new THREE.Euler().setFromQuaternion(rot, "XYZ");

  return {
    center: [center.x, center.y, center.z],
    rotation: [euler.x, euler.y, euler.z],
  };
}
