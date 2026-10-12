import { Matrix4, Vector3 } from "three";

function rasterMesh(object, projected, frame, zbuffer, width, height) {
  const indices = object.geometry.index.array;
  const uv = object.geometry.attributes.uv.array;
  const texture = object.userData.texturePixels;
  const provenance = object.userData.provenance;
  const colour = object.userData.omegaKind === 1 ? [103, 220, 192] : [244, 167, 76];
  const pixels = frame.data;
  for (let t = 0; t < indices.length; t += 3) {
    const a = indices[t],
      b = indices[t + 1],
      c = indices[t + 2];
    const ax = projected[a * 3],
      ay = projected[a * 3 + 1],
      za = projected[a * 3 + 2];
    const bx = projected[b * 3],
      by = projected[b * 3 + 1],
      zb = projected[b * 3 + 2];
    const cx = projected[c * 3],
      cy = projected[c * 3 + 1],
      zc = projected[c * 3 + 2];
    if (Math.min(za, zb, zc) <= 0.05) continue;
    const denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
    if (Math.abs(denominator) < 1e-8) continue;
    const left = Math.max(0, Math.ceil(Math.min(ax, bx, cx))),
      right = Math.min(width - 1, Math.floor(Math.max(ax, bx, cx)));
    const top = Math.max(0, Math.ceil(Math.min(ay, by, cy))),
      bottom = Math.min(height - 1, Math.floor(Math.max(ay, by, cy)));
    for (let y = top; y <= bottom; y++)
      for (let x = left; x <= right; x++) {
        const wa = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denominator;
        const wb = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denominator;
        const wc = 1 - wa - wb;
        if (Math.min(wa, wb, wc) < -1e-7) continue;
        const iz = wa / za + wb / zb + wc / zc,
          depth = 1 / iz;
        const index = y * width + x;
        if (depth >= zbuffer[index]) continue;
        zbuffer[index] = depth;
        if (provenance) {
          pixels[index * 4] = colour[0];
          pixels[index * 4 + 1] = colour[1];
          pixels[index * 4 + 2] = colour[2];
        } else {
          const u = ((wa * uv[a * 2]) / za + (wb * uv[b * 2]) / zb + (wc * uv[c * 2]) / zc) / iz;
          const v =
            1 -
            ((wa * uv[a * 2 + 1]) / za + (wb * uv[b * 2 + 1]) / zb + (wc * uv[c * 2 + 1]) / zc) /
              iz;
          const tx = Math.min(texture.width - 1.000001, Math.max(0, u * texture.width - 0.5));
          const ty = Math.min(texture.height - 1.000001, Math.max(0, v * texture.height - 0.5));
          const ix = Math.floor(tx),
            iy = Math.floor(ty),
            fx = tx - ix,
            fy = ty - iy;
          const p = (iy * texture.width + ix) * 4,
            down = p + texture.width * 4;
          for (let channel = 0; channel < 3; channel++)
            pixels[index * 4 + channel] = Math.round(
              texture.data[p + channel] * (1 - fx) * (1 - fy) +
                texture.data[p + 4 + channel] * fx * (1 - fy) +
                texture.data[down + channel] * (1 - fx) * fy +
                texture.data[down + 4 + channel] * fx * fy,
            );
        }
      }
  }
}

// This offline viewer retains an interactive path on hosts without a GL context.
export class SoftwarePointRenderer {
  constructor() {
    this.domElement = document.createElement("canvas");
    this.context = this.domElement.getContext("2d", { alpha: false });
    if (!this.context) throw new Error("浏览器无法创建 Canvas2D 点云预览");
    this.isSoftwareRenderer = true;
    this.needsRedraw = true;
    this.capabilities = { isWebGL2: false };
    this.viewport = [0, 0, 1, 1];
  }
  getPixelRatio() {
    return 1;
  }
  setPixelRatio() {}
  setScissorTest() {}
  setScissor() {}
  setSize(width, height) {
    this.domElement.width = Math.round(width);
    this.domElement.height = Math.round(height);
    this.needsRedraw = true;
  }
  setViewport(x, y, width, height) {
    this.viewport = [Math.round(x), Math.round(y), Math.round(width), Math.round(height)];
  }
  render(scene, camera) {
    const [left, , width, height] = this.viewport;
    if (width <= 0 || height <= 0) return;
    camera.updateMatrixWorld();
    const projection = new Matrix4().multiplyMatrices(
      camera.projectionMatrix,
      camera.matrixWorldInverse,
    );
    const e = projection.elements;
    const frame = this.context.createImageData(width, height);
    const pixels = frame.data;
    for (let i = 0; i < pixels.length; i += 4) {
      pixels[i] = 16;
      pixels[i + 1] = 24;
      pixels[i + 2] = 32;
      pixels[i + 3] = 255;
    }
    const zbuffer = new Float32Array(width * height).fill(Infinity);
    const projections = new Map();
    const show = ["measured", "generated", "actors", "oracle"];
    scene.traverseVisible((object) => {
      if (object.isMesh && object.visible && object.userData.texturePixels) {
        const positions = object.geometry.attributes.position.array;
        let projected = projections.get(positions);
        if (!projected) {
          projected = new Float32Array(positions.length);
          for (let i = 0; i < positions.length; i += 3) {
            const x = positions[i],
              y = positions[i + 1],
              z = positions[i + 2];
            const w = e[3] * x + e[7] * y + e[11] * z + e[15];
            projected[i] = (((e[0] * x + e[4] * y + e[8] * z + e[12]) / w + 1) * width) / 2 - 0.5;
            projected[i + 1] =
              ((1 - (e[1] * x + e[5] * y + e[9] * z + e[13]) / w) * height) / 2 - 0.5;
            projected[i + 2] = w;
          }
          projections.set(positions, projected);
        }
        rasterMesh(object, projected, frame, zbuffer, width, height);
        return;
      }
      if (!object.isPoints || !object.visible) return;
      const positions = object.geometry.attributes.position.array;
      const colours = object.geometry.attributes.color.array;
      const kinds = object.geometry.attributes.kind.array;
      const uniforms = object.material.uniforms;
      const visibility = show.map((name) => uniforms[name].value);
      const provenance = uniforms.provenance.value;
      const radius = Math.max(0, Math.round(uniforms.pointSize.value / 2) - 1);
      for (let i = 0; i < kinds.length; i++) {
        const kind = Math.round(kinds[i]);
        if (!visibility[kind]) continue;
        const x = positions[i * 3],
          y = positions[i * 3 + 1],
          z = positions[i * 3 + 2];
        const clipW = e[3] * x + e[7] * y + e[11] * z + e[15];
        if (clipW <= 0) continue;
        const clipZ = (e[2] * x + e[6] * y + e[10] * z + e[14]) / clipW;
        if (clipZ < -1 || clipZ > 1) continue;
        const u = Math.round(
          (((e[0] * x + e[4] * y + e[8] * z + e[12]) / clipW + 1) * width) / 2 - 0.5,
        );
        const v = Math.round(
          ((1 - (e[1] * x + e[5] * y + e[9] * z + e[13]) / clipW) * height) / 2 - 0.5,
        );
        if (u < 0 || u >= width || v < 0 || v >= height) continue;
        const rgb = provenance
          ? kind === 0
            ? [73, 200, 236]
            : kind === 1
              ? [103, 220, 192]
              : [244, 167, 76]
          : null;
        const r = rgb ? rgb[0] : Math.round(colours[i * 3] * 255);
        const g = rgb ? rgb[1] : Math.round(colours[i * 3 + 1] * 255);
        const b = rgb ? rgb[2] : Math.round(colours[i * 3 + 2] * 255);
        for (let dy = -radius; dy <= radius; dy++)
          for (let dx = -radius; dx <= radius; dx++) {
            const px = u + dx,
              py = v + dy;
            if (px < 0 || px >= width || py < 0 || py >= height) continue;
            const index = py * width + px;
            if (clipW >= zbuffer[index]) continue;
            zbuffer[index] = clipW;
            pixels[index * 4] = r;
            pixels[index * 4 + 1] = g;
            pixels[index * 4 + 2] = b;
          }
      }
    });
    this.context.putImageData(frame, left, 0);
    const origin = new Vector3(0, 0, 0).project(camera);
    if (origin.z >= -1 && origin.z <= 1) {
      const x = left + ((origin.x + 1) * width) / 2,
        y = ((1 - origin.y) * height) / 2;
      this.context.strokeStyle = "#e8f1f4";
      this.context.beginPath();
      this.context.moveTo(x - 4, y);
      this.context.lineTo(x + 4, y);
      this.context.moveTo(x, y - 4);
      this.context.lineTo(x, y + 4);
      this.context.stroke();
    }
  }
}
