import * as THREE from "three";

export type PointCloudDisplayStyle = "points" | "surface";

export const POINT_CLOUD_SURFACE_SIZE_MULTIPLIER = 1.6;

const SURFACE_PROFILE_SIZE = 32;
const SURFACE_ALPHA_TEST = 0.08;
const surfaceProfiles = new WeakMap<THREE.PointsMaterial, THREE.DataTexture>();

function createSurfaceProfile(): THREE.DataTexture {
  const data = new Uint8Array(SURFACE_PROFILE_SIZE * SURFACE_PROFILE_SIZE * 4);
  for (let y = 0; y < SURFACE_PROFILE_SIZE; y += 1) {
    for (let x = 0; x < SURFACE_PROFILE_SIZE; x += 1) {
      const nx = ((x + 0.5) / SURFACE_PROFILE_SIZE) * 2 - 1;
      const ny = ((y + 0.5) / SURFACE_PROFILE_SIZE) * 2 - 1;
      const radius = Math.hypot(nx, ny);
      // Keep a solid center and feather the outer half. Alpha testing removes
      // the transparent border before it can write depth.
      const alpha = THREE.MathUtils.smoothstep(1 - radius, 0, 0.55);
      const value = Math.round(alpha * 255);
      const offset = (y * SURFACE_PROFILE_SIZE + x) * 4;
      // PointsMaterial samples the green channel of alphaMap, so keep every
      // color channel identical and the texture format explicit.
      data[offset] = value;
      data[offset + 1] = value;
      data[offset + 2] = value;
      data[offset + 3] = value;
    }
  }
  const texture = new THREE.DataTexture(
    data,
    SURFACE_PROFILE_SIZE,
    SURFACE_PROFILE_SIZE,
    THREE.RGBAFormat,
  );
  texture.magFilter = THREE.LinearFilter;
  texture.minFilter = THREE.LinearFilter;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  return texture;
}

export function pointCloudDisplaySize(size: number, style: PointCloudDisplayStyle): number {
  return style === "surface" ? size * POINT_CLOUD_SURFACE_SIZE_MULTIPLIER : size;
}

/** Mutates an existing WebGL point material; geometry and picking stay untouched. */
export function applyLegacyPointCloudDisplayStyle(
  material: THREE.PointsMaterial,
  style: PointCloudDisplayStyle,
): void {
  if (style === "surface") {
    let profile = surfaceProfiles.get(material);
    if (!profile) {
      profile = createSurfaceProfile();
      surfaceProfiles.set(material, profile);
    }
    material.alphaMap = profile;
    material.alphaTest = SURFACE_ALPHA_TEST;
    material.alphaToCoverage = true;
  } else {
    material.alphaMap = null;
    material.alphaTest = 0;
    material.alphaToCoverage = false;
  }
  material.needsUpdate = true;
}

export function disposeLegacyPointCloudDisplayStyle(material: THREE.PointsMaterial): void {
  surfaceProfiles.get(material)?.dispose();
  surfaceProfiles.delete(material);
}
