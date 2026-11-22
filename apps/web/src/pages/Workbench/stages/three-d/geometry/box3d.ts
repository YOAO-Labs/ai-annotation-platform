import * as THREE from "three";

type Vec3 = readonly [number, number, number];

export function boxToMatrix4(center: Vec3, size: Vec3, rotation: Vec3): THREE.Matrix4 {
  const position = new THREE.Vector3(center[0], center[1], center[2]);
  const euler = new THREE.Euler(rotation[0], rotation[1], rotation[2], "XYZ");
  const quaternion = new THREE.Quaternion().setFromEuler(euler);
  const scale = new THREE.Vector3(size[0], size[1], size[2]);
  return new THREE.Matrix4().compose(position, quaternion, scale);
}

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

export function psrToCorners(center: Vec3, size: Vec3, rotation: Vec3): THREE.Vector3[] {
  const m = boxToMatrix4(center, size, rotation);
  return UNIT_CUBE_CORNERS.map((c) => new THREE.Vector3(c[0], c[1], c[2]).applyMatrix4(m));
}

function unitAxis(axis: 0 | 1 | 2): THREE.Vector3 {
  return new THREE.Vector3(axis === 0 ? 1 : 0, axis === 1 ? 1 : 0, axis === 2 ? 1 : 0);
}

export function boxAxisWorldDir(rotation: Vec3, axis: 0 | 1 | 2): THREE.Vector3 {
  const q = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(rotation[0], rotation[1], rotation[2], "XYZ"),
  );
  return unitAxis(axis).applyQuaternion(q).normalize();
}

export function worldToBox(center: Vec3, size: Vec3, rotation: Vec3): THREE.Matrix4 {
  return boxToMatrix4(center, size, rotation).invert();
}

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
    planes.push(
      new THREE.Plane().setFromNormalAndCoplanarPoint(
        dir.clone().negate(),
        c.clone().addScaledVector(dir, half),
      ),
    );
    planes.push(
      new THREE.Plane().setFromNormalAndCoplanarPoint(
        dir.clone(),
        c.clone().addScaledVector(dir, -half),
      ),
    );
  }
  return planes;
}
