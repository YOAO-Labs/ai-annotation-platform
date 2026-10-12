import * as THREE from "three";

async function buffer(path, signal) {
  const response = await fetch(path, { signal });
  if (!response.ok) throw new Error(`网格读取失败 ${response.status}`);
  return response.arrayBuffer();
}

export async function loadTexturedMesh(record, signal) {
  const [vertices, uvBytes, indices] = await Promise.all([
    buffer(record.vertex_buffer, signal),
    buffer(record.uv, signal),
    buffer(record.indices, signal),
  ]);
  const xyz = new Float32Array((vertices.byteLength / 16) * 3),
    view = new DataView(vertices);
  for (let i = 0; i < xyz.length / 3; i++)
    for (let a = 0; a < 3; a++) xyz[i * 3 + a] = view.getFloat32(i * 16 + a * 4, true);
  const uv = new Float32Array(uvBytes);
  for (let i = 1; i < uv.length; i += 2) uv[i] = 1 - uv[i];
  const triangles = new Uint32Array(indices),
    group = new THREE.Group(),
    textures = new Map();
  const loadTexture = async (url) => {
    const response = await fetch(url, { signal });
    if (!response.ok) throw new Error(`纹理读取失败 ${response.status}`);
    const bitmap = await createImageBitmap(await response.blob());
    if (signal.aborted) {
      bitmap.close();
      throw new DOMException("Aborted", "AbortError");
    }
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(bitmap, 0, 0);
    bitmap.close();
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return { texture, pixels };
  };
  try {
    for (const row of record.groups)
      if (!textures.has(row.texture)) textures.set(row.texture, loadTexture(row.texture));
    await Promise.all(textures.values());
    for (const row of record.groups) {
      const { texture, pixels } = await textures.get(row.texture);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(xyz, 3));
      geometry.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
      geometry.setIndex(
        new THREE.BufferAttribute(
          triangles.subarray(row.offset * 3, (row.offset + row.triangles) * 3),
          1,
        ),
      );
      const material = new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.frustumCulled = false;
      mesh.userData = {
        omegaKind: row.kind,
        texturePixels: pixels,
        sourceTexture: texture,
        provenance: false,
      };
      group.add(mesh);
    }
    return group;
  } catch (error) {
    for (const promise of textures.values())
      promise.then(({ texture }) => texture.dispose()).catch(() => {});
    disposeLayer(group);
    throw error;
  }
}

export function disposeLayer(layer) {
  const textures = new Set();
  layer.traverse((object) => {
    object.geometry?.dispose();
    object.material?.dispose();
    if (object.userData.sourceTexture) textures.add(object.userData.sourceTexture);
  });
  for (const texture of textures) texture.dispose();
}

export function clearControlsMotion(controls) {
  const damping = controls.enableDamping;
  controls.enableDamping = false;
  controls.update();
  controls.enableDamping = damping;
}

export function sourceCamera(camera, controls, row) {
  clearControlsMotion(controls);
  const m = new THREE.Matrix4().set(...row.viewer_from_camera_cv.flat());
  m.multiply(new THREE.Matrix4().makeScale(1, -1, -1));
  camera.position.setFromMatrixPosition(m);
  camera.quaternion.setFromRotationMatrix(m);
  camera.up.set(0, 1, 0).applyQuaternion(camera.quaternion);
  controls.target
    .copy(camera.position)
    .add(new THREE.Vector3(0, 0, -12).applyQuaternion(camera.quaternion));
  controls.update();
}

export function calibratedProjection(camera, row, width, height) {
  const [sw, sh] = row.size,
    k = row.intrinsic;
  const scale = Math.min(width / sw, height / sh),
    dx = (width - sw * scale) / 2,
    dy = (height - sh * scale) / 2;
  const fx = k[0][0] * scale,
    fy = k[1][1] * scale;
  const cx = (k[0][2] + 0.5) * scale - 0.5 + dx,
    cy = (k[1][2] + 0.5) * scale - 0.5 + dy;
  const near = 0.05,
    far = 300;
  camera.projectionMatrix.set(
    (2 * fx) / width,
    0,
    1 - (2 * (cx + 0.5)) / width,
    0,
    0,
    (2 * fy) / height,
    (2 * (cy + 0.5)) / height - 1,
    0,
    0,
    0,
    -(far + near) / (far - near),
    (-2 * far * near) / (far - near),
    0,
    0,
    -1,
    0,
  );
  camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
}
