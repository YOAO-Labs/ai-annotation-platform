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
    new THREE.Quaternion(x, y, z, w),
    new THREE.Vector3(1, 1, 1),
  );
}

export function frameRelMatrix(
  fromPose: FramePose | undefined,
  toPose: FramePose | undefined,
): THREE.Matrix4 | null {
  if (!fromPose || !toPose) return null;
  return poseToMatrix(toPose).invert().multiply(poseToMatrix(fromPose));
}

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
