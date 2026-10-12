import * as THREE from "three";

export function decodePoints(buffer) {
  if (buffer.byteLength % 16) throw new Error("点云数据长度错误");
  const count = buffer.byteLength / 16;
  const view = new DataView(buffer);
  const xyz = new Float32Array(count * 3),
    rgb = new Uint8Array(count * 3);
  for (let i = 0; i < count; i++)
    for (let a = 0; a < 3; a++) {
      xyz[i * 3 + a] = view.getFloat32(i * 16 + a * 4, true);
      rgb[i * 3 + a] = view.getUint8(i * 16 + 12 + a);
    }
  return { xyz, rgb, count };
}

export class SequenceRenderer {
  constructor(host, grid) {
    this.host = host;
    this.grid = grid;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xffffff);
    this.points = [];
    this.gpuPoints = [];
    try {
      this.gpu = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
      this.gpu.setPixelRatio(Math.min(devicePixelRatio, 2));
      this.canvas = this.gpu.domElement;
      this.backend = "WebGL";
    } catch {
      this.canvas = document.createElement("canvas");
      this.ctx = this.canvas.getContext("2d", { alpha: false });
      if (!this.ctx) throw new Error("无法创建点云绘制上下文");
      this.backend = "Canvas2D · CPU";
    }
    host.prepend(this.canvas);
    this.overlay = document.createElement("canvas");
    this.overlay.className = "trajectory";
    this.overlay.setAttribute("aria-hidden", "true");
    host.append(this.overlay);
    this.lines = this.overlay.getContext("2d");
  }
  resize(width, height) {
    this.width = width;
    this.height = height;
    if (this.gpu) this.gpu.setSize(width, height, false);
    else {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.overlay.width = width;
    this.overlay.height = height;
  }
  setPoints(main, actors) {
    this.points = [main, actors].filter(Boolean);
    if (this.gpu) {
      for (const object of this.gpuPoints) {
        this.scene.remove(object);
        object.geometry.dispose();
        object.material.dispose();
      }
      this.gpuPoints = this.points.map((item) => {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(item.xyz, 3));
        const linear = new Float32Array(item.rgb.length);
        for (let i = 0; i < linear.length; i++) {
          const srgb = item.rgb[i] / 255;
          linear[i] = srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
        }
        geometry.setAttribute("color", new THREE.BufferAttribute(linear, 3));
        const material = new THREE.PointsMaterial({
          size: 2,
          sizeAttenuation: false,
          vertexColors: true,
        });
        const object = new THREE.Points(geometry, material);
        this.scene.add(object);
        return object;
      });
    }
  }
  render(camera, cameras, frame, showTrajectory, method, interaction = false) {
    if (!this.width || !this.height) return;
    camera.updateMatrixWorld();
    const m = new THREE.Matrix4().multiplyMatrices(
      camera.projectionMatrix,
      camera.matrixWorldInverse,
    ).elements;
    const w = this.width,
      h = this.height;
    let drawn = 0;
    if (this.gpu) this.gpu.render(this.scene, camera);
    else {
      const image = this.ctx.createImageData(w, h),
        pixels = image.data;
      pixels.fill(255);
      const zbuffer = new Float32Array(w * h).fill(Infinity);
      for (const points of this.points) {
        const { xyz, rgb, count } = points;
        const step = interaction ? Math.max(1, Math.ceil(count / 120000)) : 1;
        for (let i = 0; i < count; i += step) {
          const k = i * 3,
            x = xyz[k],
            y = xyz[k + 1],
            z = xyz[k + 2];
          const pw = m[3] * x + m[7] * y + m[11] * z + m[15];
          if (pw <= 0) continue;
          const px = (m[0] * x + m[4] * y + m[8] * z + m[12]) / pw;
          const py = (m[1] * x + m[5] * y + m[9] * z + m[13]) / pw;
          const pz = (m[2] * x + m[6] * y + m[10] * z + m[14]) / pw;
          if (Math.abs(px) > 1 || Math.abs(py) > 1 || Math.abs(pz) > 1) continue;
          const u = Math.round(((px + 1) * w) / 2 - 0.5),
            v = Math.round(((1 - py) * h) / 2 - 0.5);
          drawn++;
          for (let dy = 0; dy < 2; dy++)
            for (let dx = 0; dx < 2; dx++) {
              const a = u + dx,
                b = v + dy;
              if (a < 0 || a >= w || b < 0 || b >= h) continue;
              const index = b * w + a;
              if (pw >= zbuffer[index]) continue;
              zbuffer[index] = pw;
              pixels[index * 4] = rgb[k];
              pixels[index * 4 + 1] = rgb[k + 1];
              pixels[index * 4 + 2] = rgb[k + 2];
            }
        }
      }
      this.ctx.putImageData(image, 0, 0);
    }
    const ctx = this.lines;
    ctx.clearRect(0, 0, w, h);
    if (showTrajectory) {
      const pose = (row) =>
        method === "raw" ? row.raw_viewer_from_camera_cv : row.viewer_from_camera_cv;
      const screen = (point) => {
        const [x, y, z] = point;
        const pw = m[3] * x + m[7] * y + m[11] * z + m[15];
        if (pw <= 0.05) return null;
        return [
          (((m[0] * x + m[4] * y + m[8] * z + m[12]) / pw + 1) * w) / 2,
          ((1 - (m[1] * x + m[5] * y + m[9] * z + m[13]) / pw) * h) / 2,
        ];
      };
      const centre = (row) => {
        const p = pose(row);
        return [p[0][3], p[1][3], p[2][3]];
      };
      for (let i = 6; i < cameras.length; i += 6) {
        const a = screen(centre(cameras[i - 6])),
          b = screen(centre(cameras[i]));
        if (!a || !b) continue;
        ctx.strokeStyle = `hsl(${285 - (i / (cameras.length - 6)) * 285} 84% 49%)`;
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(...a);
        ctx.lineTo(...b);
        ctx.stroke();
        ctx.fillStyle = ctx.strokeStyle;
        ctx.fillRect(b[0] - 2, b[1] - 2, 4, 4);
      }
      for (let cam = 0; cam < 6; cam++) {
        const row = cameras[frame * 6 + cam],
          p = pose(row),
          o = centre(row);
        const world = (x, y, z) =>
          p.slice(0, 3).map((axis) => axis[0] * x + axis[1] * y + axis[2] * z + axis[3]);
        const intrinsic = method === "raw" ? row.raw_intrinsic : row.intrinsic;
        const inverse = new THREE.Matrix3().set(...intrinsic.flat()).invert();
        const [imageWidth, imageHeight] = this.grid;
        const corners = [
          [-0.5, -0.5],
          [imageWidth - 0.5, -0.5],
          [imageWidth - 0.5, imageHeight - 0.5],
          [-0.5, imageHeight - 0.5],
        ].map(([u, v]) => {
          const ray = new THREE.Vector3(u, v, 1).applyMatrix3(inverse).multiplyScalar(0.9);
          return screen(world(ray.x, ray.y, ray.z));
        });
        const c = screen(o);
        if (!c || corners.some((v) => !v)) continue;
        ctx.strokeStyle = "#244d61";
        ctx.lineWidth = 1.3;
        ctx.beginPath();
        for (const corner of corners) {
          ctx.moveTo(...c);
          ctx.lineTo(...corner);
        }
        ctx.moveTo(...corners[0]);
        for (const corner of [...corners.slice(1), corners[0]]) ctx.lineTo(...corner);
        ctx.stroke();
        ctx.fillStyle = "#1e5368";
        ctx.beginPath();
        ctx.arc(...c, 4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    return {
      drawn,
      points: this.points.reduce((n, p) => n + p.count, 0),
      backend: this.backend,
      width: w,
      height: h,
    };
  }
}
