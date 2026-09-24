import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const byId = (id) => document.getElementById(id);
const data = await fetch("./data.json").then((response) => {
  if (!response.ok) throw new Error(`无法读取试验数据：${response.status}`);
  return response.json();
});

byId("subtitle").textContent =
  `${data.scene} · ${data.camera} · ${data.frames.length} 帧实验 / 单张 ${data.device}`;
const runBaseText = `模型 ${data.model_id} · 推理 ${data.inference_s} s · PyTorch 峰值预留 ${(data.peak_torch_reserved_mib / 1024).toFixed(1)} GiB`;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1520);
const camera = new THREE.PerspectiveCamera(57, 1, 0.1, 800);
camera.position.set(21, 12, 7);
const host = byId("cloud-view");
let canvas2dMode = false;

function createCanvasRenderer() {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d", { alpha: false });
  const projected = new THREE.Vector3();
  let pixelRatio = 1;
  let lastPaint = 0;
  canvas2dMode = true;
  return {
    domElement: canvas,
    setPixelRatio(value) {
      pixelRatio = value;
    },
    setSize(width, height) {
      canvas.width = Math.floor(width * pixelRatio);
      canvas.height = Math.floor(height * pixelRatio);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    },
    render(_scene, viewCamera) {
      const now = performance.now();
      if (now - lastPaint < 80) return;
      lastPaint = now;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.fillStyle = "#0b1520";
      context.fillRect(0, 0, width, height);
      viewCamera.updateMatrixWorld();
      for (const cloud of clouds) {
        if (!cloud.visible) continue;
        const positions = cloud.geometry.getAttribute("position");
        const colors = cloud.geometry.getAttribute("color");
        context.globalAlpha = cloud.material.opacity;
        for (let i = 0; i < positions.count; i += 2) {
          projected.fromBufferAttribute(positions, i).project(viewCamera);
          if (
            projected.z < -1 ||
            projected.z > 1 ||
            Math.abs(projected.x) > 1 ||
            Math.abs(projected.y) > 1
          )
            continue;
          const x = ((projected.x + 1) * width) / 2;
          const y = ((1 - projected.y) * height) / 2;
          context.fillStyle = `rgb(${Math.round(colors.getX(i) * 255)} ${Math.round(colors.getY(i) * 255)} ${Math.round(colors.getZ(i) * 255)})`;
          context.fillRect(
            x,
            y,
            Math.max(1, cloud.material.size * 0.7),
            Math.max(1, cloud.material.size * 0.7),
          );
        }
      }
      context.globalAlpha = 1;
    },
  };
}

let renderer;
if (new URLSearchParams(window.location.search).has("canvas2d")) {
  renderer = createCanvasRenderer();
} else {
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true });
  } catch {
    renderer = createCanvasRenderer();
  }
}
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
host.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, -18);
controls.enableDamping = true;
controls.minDistance = 2;
controls.maxDistance = 220;
controls.update();

const grid = new THREE.GridHelper(140, 28, 0x4b6170, 0x283c4a);
grid.position.set(0, -1.5, -35);
scene.add(grid);
const axes = new THREE.AxesHelper(4);
scene.add(axes);
let clouds = [];
let selected = 0;
let generation = 0;
let depthValues = null;
let lidarDepthValues = null;

function resize() {
  const width = Math.max(1, host.clientWidth);
  const height = Math.max(1, host.clientHeight);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(host);
resize();

function draw() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(draw);
}
draw();

async function loadCloud(path, size, opacity) {
  const bytes = await fetch(`./${path}`).then((response) => {
    if (!response.ok) throw new Error(`无法读取 ${path}`);
    return response.arrayBuffer();
  });
  if (bytes.byteLength % 16 !== 0) throw new Error(`点云文件长度无效：${path}`);
  const view = new DataView(bytes);
  const count = bytes.byteLength / 16;
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const offset = i * 16;
    positions[i * 3] = view.getFloat32(offset, true);
    positions[i * 3 + 1] = view.getFloat32(offset + 4, true);
    positions[i * 3 + 2] = view.getFloat32(offset + 8, true);
    colors[i * 3] = view.getUint8(offset + 12) / 255;
    colors[i * 3 + 1] = view.getUint8(offset + 13) / 255;
    colors[i * 3 + 2] = view.getUint8(offset + 14) / 255;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  const material = new THREE.PointsMaterial({
    size,
    sizeAttenuation: true,
    vertexColors: true,
    transparent: true,
    opacity,
    depthWrite: false,
  });
  const points = new THREE.Points(geometry, material);
  scene.add(points);
  return points;
}

function disposeClouds() {
  for (const cloud of clouds) {
    scene.remove(cloud);
    cloud.geometry.dispose();
    cloud.material.dispose();
  }
  clouds = [];
}

function refreshCloudVisibility() {
  if (clouds.length !== 3) return;
  const showModel = byId("show-model").checked;
  clouds[0].visible = byId("show-lidar").checked;
  clouds[1].visible = showModel && !byId("confident-only").checked;
  clouds[2].visible = showModel && byId("confident-only").checked;
  const size = Number(byId("point-size").value);
  clouds[0].material.size = size * 0.9;
  clouds[1].material.size = size;
  clouds[2].material.size = size;
}

for (const id of ["show-lidar", "show-model", "confident-only", "point-size"]) {
  byId(id).addEventListener("input", refreshCloudVisibility);
}

function refreshImageControls() {
  const opacity = Number(byId("depth-opacity").value) / 100;
  byId("depth-image").style.opacity = String(opacity);
  byId("depth-opacity-label").textContent = `${Math.round(opacity * 100)}%`;
  byId("prompt-image").style.display = byId("show-prompt").checked ? "block" : "none";
  byId("error-image").style.display = byId("show-errors").checked ? "block" : "none";
}
for (const id of ["depth-opacity", "show-prompt", "show-errors"]) {
  byId(id).addEventListener("input", refreshImageControls);
}
refreshImageControls();

byId("image-stage").addEventListener("pointermove", (event) => {
  const frame = data.frames[selected];
  if (!depthValues || !lidarDepthValues) return;
  const bounds = event.currentTarget.getBoundingClientRect();
  const x = Math.min(
    frame.size[0] - 1,
    Math.max(0, Math.floor(((event.clientX - bounds.left) / bounds.width) * frame.size[0])),
  );
  const y = Math.min(
    frame.size[1] - 1,
    Math.max(0, Math.floor(((event.clientY - bounds.top) / bounds.height) * frame.size[1])),
  );
  const index = y * frame.size[0] + x;
  const predicted = depthValues[index];
  const lidar = lidarDepthValues[index];
  byId("pixel-readout").textContent =
    `(${x}, ${y}) · 模型 ${Number.isFinite(predicted) ? predicted.toFixed(1) : "—"} m · LiDAR ${lidar > 0 ? lidar.toFixed(1) : "无点"}${lidar > 0 ? " m" : ""}`;
});
byId("image-stage").addEventListener("pointerleave", () => {
  byId("pixel-readout").textContent = "";
});

function metric(label, value, note) {
  const item = document.createElement("div");
  item.className = "metric";
  const title = document.createElement("span");
  title.className = "label";
  title.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  const small = document.createElement("small");
  small.textContent = note;
  item.append(title, strong, small);
  return item;
}

async function selectFrame(index) {
  const current = ++generation;
  selected = index;
  const frame = data.frames[index];
  byId("run-meta").textContent =
    `${runBaseText} · 全部留出点模型 / LiDAR MAE ${frame.model.mae_m.toFixed(2)} / ${frame.lidar_nearest.mae_m.toFixed(2)} m${canvas2dMode ? " · Canvas 2D 兼容模式" : ""}`;
  for (const [name, path] of Object.entries(frame.images)) {
    const image = byId(`${name}-image`);
    if (image) image.src = `./${path}`;
  }
  byId("metrics").replaceChildren(
    metric(
      "模型有效点 MAE",
      frame.model_on_valid ? `${frame.model_on_valid.mae_m.toFixed(2)} m` : "—",
      `${frame.valid_holdout_returns} / ${frame.holdout_returns} 个留出点`,
    ),
    metric(
      "LiDAR 邻近 MAE",
      frame.lidar_on_valid ? `${frame.lidar_on_valid.mae_m.toFixed(2)} m` : "—",
      "与模型有效点完全相同的留出点",
    ),
    metric(
      "有效模型像素",
      `${Math.round(frame.model_mask_fraction * 100)}%`,
      "官方输出 mask；不代表几何可靠",
    ),
    metric(
      "模型点 / LiDAR 点",
      `${frame.cloud_counts.model.toLocaleString()} / ${frame.cloud_counts.lidar.toLocaleString()}`,
      "3D 视图已做有界抽样",
    ),
  );
  for (const button of byId("frame-tabs").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.index) === index));
  }
  depthValues = null;
  lidarDepthValues = null;
  const [lidar, model, confident, depth, lidarDepth] = await Promise.all([
    loadCloud(frame.clouds.lidar, 2.7, 0.96),
    loadCloud(frame.clouds.model, 3, 0.75),
    loadCloud(frame.clouds.confident, 3, 0.85),
    fetch(`./${frame.depth_values}`).then((response) => response.arrayBuffer()),
    fetch(`./${frame.lidar_depth_values}`).then((response) => response.arrayBuffer()),
  ]);
  if (current !== generation) {
    for (const cloud of [lidar, model, confident]) {
      scene.remove(cloud);
      cloud.geometry.dispose();
      cloud.material.dispose();
    }
    return;
  }
  disposeClouds();
  clouds = [lidar, model, confident];
  depthValues = new Float32Array(depth);
  lidarDepthValues = new Float32Array(lidarDepth);
  refreshCloudVisibility();
}

data.frames.forEach((frame, index) => {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.index = String(index);
  button.textContent = `第 ${frame.frame_index} 帧`;
  button.setAttribute("aria-pressed", String(index === 0));
  button.addEventListener("click", () => selectFrame(index).catch(showError));
  byId("frame-tabs").append(button);
});

function showError(error) {
  byId("notes").textContent = `页面加载失败：${error.message}`;
  throw error;
}
selectFrame(0).catch(showError);
