import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const byId = (id) => document.getElementById(id);
const data = await fetch("./data.json").then((response) => {
  if (!response.ok) throw new Error(`无法读取试验数据：${response.status}`);
  return response.json();
});
const hasTemporalFusion = Boolean(data.temporal_fusion);
if (hasTemporalFusion) {
  byId("notice").textContent =
    "相邻三帧分别输入模型，并用相机标定和位姿把跨帧一致的表面投到中间帧。补全模式保留中间帧原始结果，仅把通过深度、邻近 LiDAR 和 RGB 检查的邻帧像素补入空洞；严格模式只显示有时序或 LiDAR 支持的区域。几何一致不等于语义静态，本页仅供人工审阅。";
  byId("notes").textContent =
    "融合有效像素包含中间帧原始结果和邻帧补洞；严格模式只保留跨帧或本帧 LiDAR 支持的像素。误差只统计各自有效的中间帧留出 LiDAR 点，点集可能不同。页面的 LiDAR 邻近基线只使用中间帧输入点，而模型还读取了邻帧 LiDAR，不能据此认定模型优于多帧 LiDAR。跨帧一致性也无法可靠识别同速运动目标。";
}

byId("subtitle").textContent =
  `${data.scene} · ${hasTemporalFusion ? `${data.input_frames.length} 帧输入 / 中间帧预览` : `${new Set(data.frames.map((view) => view.frame_index)).size} 帧`} · ${data.cameras?.length ?? 1} 路相机 / 单张 ${data.device}`;
const runBaseText = `模型 ${data.model_id} · 推理 ${data.inference_s} s · PyTorch 峰值预留 ${(data.peak_torch_reserved_mib / 1024).toFixed(1)} GiB`;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1520);
const camera = new THREE.PerspectiveCamera(57, 1, 0.1, 800);
camera.position.set(60, 44, 70);
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
      const activeSurfaces = viewModels.flatMap((view) =>
        [view.surface, view.temporalSurface, view.strictSurface].filter(
          (surface) => surface?.visible,
        ),
      );
      for (const view of activeSurfaces) {
        const geometry = view.geometry;
        const positions = geometry.getAttribute("position");
        const colors = geometry.getAttribute("color");
        const indices = view.userData.previewIndices;
        context.globalAlpha = 0.9;
        for (let face = 0; face < indices.length / 3; face++) {
          const vertices = [];
          let clipped = false;
          for (let corner = 0; corner < 3; corner++) {
            const index = indices[face * 3 + corner];
            projected.fromBufferAttribute(positions, index).project(viewCamera);
            if (projected.z < -1 || projected.z > 1) clipped = true;
            vertices.push([
              ((projected.x + 1) * width) / 2,
              ((1 - projected.y) * height) / 2,
              index,
            ]);
          }
          if (clipped) continue;
          const colorIndex = vertices[0][2];
          context.fillStyle = `rgb(${Math.round(colors.getX(colorIndex) * 255)} ${Math.round(colors.getY(colorIndex) * 255)} ${Math.round(colors.getZ(colorIndex) * 255)})`;
          context.beginPath();
          context.moveTo(vertices[0][0], vertices[0][1]);
          context.lineTo(vertices[1][0], vertices[1][1]);
          context.lineTo(vertices[2][0], vertices[2][1]);
          context.closePath();
          context.fill();
        }
      }
      for (const cloud of viewModels.flatMap((view) => [view.lidar, view.model, view.confident])) {
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
controls.target.set(0, 0, 0);
controls.enableDamping = true;
controls.minDistance = 2;
controls.maxDistance = 220;
controls.update();

const grid = new THREE.GridHelper(140, 28, 0x4b6170, 0x283c4a);
grid.position.set(0, -1.5, 0);
scene.add(grid);
const axes = new THREE.AxesHelper(4);
scene.add(axes);
let viewModels = [];
let selected = 0;
let depthValues = null;
let temporalDepthValues = null;
let lidarDepthValues = null;
let viewpoint = "overview";
const cameraNames = {
  CAM_FRONT: "前视",
  CAM_FRONT_RIGHT: "右前",
  CAM_BACK_RIGHT: "右后",
  CAM_BACK: "后视",
  CAM_BACK_LEFT: "左后",
  CAM_FRONT_LEFT: "左前",
};

function setViewpoint(mode) {
  viewpoint = mode;
  if (mode === "camera") {
    const pose = data.frames[selected].camera_view;
    const position = new THREE.Vector3(...pose.position);
    const forward = new THREE.Vector3(...pose.forward);
    camera.position.copy(position);
    camera.up.set(...pose.up);
    controls.target.copy(position).addScaledVector(forward, 20);
    controls.minDistance = 0.2;
  } else {
    camera.position.set(60, 44, 70);
    camera.up.set(0, 1, 0);
    controls.target.set(0, 0, 0);
    controls.minDistance = 2;
  }
  controls.update();
  byId("overview-preset").setAttribute("aria-pressed", String(mode === "overview"));
  byId("camera-preset").setAttribute("aria-pressed", String(mode === "camera"));
}
byId("overview-preset").addEventListener("click", () => setViewpoint("overview"));
byId("camera-preset").addEventListener("click", () => setViewpoint("camera"));

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
    depthTest: false,
  });
  const points = new THREE.Points(geometry, material);
  points.renderOrder = 2;
  scene.add(points);
  return points;
}

async function loadSurface(surface) {
  const [vertexBytes, indexBytes, previewIndexBytes] = await Promise.all([
    fetch(`./${surface.vertices}`).then((response) => {
      if (!response.ok) throw new Error(`无法读取 ${surface.vertices}`);
      return response.arrayBuffer();
    }),
    fetch(`./${surface.indices}`).then((response) => {
      if (!response.ok) throw new Error(`无法读取 ${surface.indices}`);
      return response.arrayBuffer();
    }),
    fetch(`./${surface.preview_indices}`).then((response) => {
      if (!response.ok) throw new Error(`无法读取 ${surface.preview_indices}`);
      return response.arrayBuffer();
    }),
  ]);
  if (vertexBytes.byteLength !== surface.vertex_count * 16) {
    throw new Error(`表面顶点长度无效：${surface.vertices}`);
  }
  if (indexBytes.byteLength !== surface.triangle_count * 12) {
    throw new Error(`表面索引长度无效：${surface.indices}`);
  }
  if (previewIndexBytes.byteLength !== surface.preview_triangle_count * 12) {
    throw new Error(`预览表面索引长度无效：${surface.preview_indices}`);
  }
  const records = new DataView(vertexBytes);
  const positions = new Float32Array(surface.vertex_count * 3);
  const colors = new Float32Array(surface.vertex_count * 3);
  for (let i = 0; i < surface.vertex_count; i++) {
    const offset = i * 16;
    positions[i * 3] = records.getFloat32(offset, true);
    positions[i * 3 + 1] = records.getFloat32(offset + 4, true);
    positions[i * 3 + 2] = records.getFloat32(offset + 8, true);
    colors[i * 3] = records.getUint8(offset + 12) / 255;
    colors[i * 3 + 1] = records.getUint8(offset + 13) / 255;
    colors[i * 3 + 2] = records.getUint8(offset + 14) / 255;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(indexBytes), 1));
  const material = new THREE.MeshBasicMaterial({
    vertexColors: true,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.93,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.userData.previewIndices = new Uint32Array(previewIndexBytes);
  scene.add(mesh);
  return mesh;
}

function refreshCloudVisibility() {
  if (!viewModels.length) return;
  const aggregate = byId("aggregate-cameras").checked;
  const showModel = byId("show-model").checked;
  const displayMode = byId("model-display").value;
  const surfaceMode = displayMode !== "points";
  const size = Number(byId("point-size").value);
  for (const [index, view] of viewModels.entries()) {
    const active = aggregate || index === selected;
    view.lidar.visible = active && byId("show-lidar").checked;
    view.model.visible = active && showModel && !surfaceMode && !byId("confident-only").checked;
    view.confident.visible = active && showModel && !surfaceMode && byId("confident-only").checked;
    view.surface.visible = active && showModel && displayMode === "surface";
    if (view.temporalSurface) {
      view.temporalSurface.visible = active && showModel && displayMode === "temporal";
    }
    if (view.strictSurface) {
      view.strictSurface.visible = active && showModel && displayMode === "strict";
    }
    view.lidar.material.size = size * 0.9;
    view.model.material.size = size;
    view.confident.material.size = size;
  }
  byId("confident-only").disabled = surfaceMode;
  byId("point-size").disabled = surfaceMode;
}

for (const id of [
  "show-lidar",
  "show-model",
  "confident-only",
  "point-size",
  "model-display",
  "aggregate-cameras",
]) {
  byId(id).addEventListener("input", refreshCloudVisibility);
}
byId("model-display").addEventListener("change", () => selectFrame(selected));

function refreshImageControls() {
  const opacity = Number(byId("depth-opacity").value) / 100;
  byId("depth-image").style.opacity = String(opacity);
  byId("depth-opacity-label").textContent = `${Math.round(opacity * 100)}%`;
  byId("prompt-image").style.display = byId("show-prompt").checked ? "block" : "none";
  byId("error-image").style.display = byId("show-errors").checked ? "block" : "none";
  byId("temporal-fill-image").style.display =
    hasTemporalFusion && byId("show-temporal-fill").checked ? "block" : "none";
}
for (const id of ["depth-opacity", "show-prompt", "show-errors", "show-temporal-fill"]) {
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
  const temporalDepth = temporalDepthValues?.[index];
  const lidar = lidarDepthValues[index];
  byId("pixel-readout").textContent =
    `(${x}, ${y}) · ${hasTemporalFusion ? "未融合" : "模型"} ${Number.isFinite(predicted) ? predicted.toFixed(1) : "—"} m${hasTemporalFusion ? ` · 融合 ${Number.isFinite(temporalDepth) ? temporalDepth.toFixed(1) : "—"} m` : ""} · LiDAR ${lidar > 0 ? lidar.toFixed(1) : "无点"}${lidar > 0 ? " m" : ""}`;
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

function selectFrame(index) {
  selected = index;
  const frame = data.frames[index];
  if (viewpoint === "camera") setViewpoint("camera");
  byId("view-label").textContent =
    `第 ${frame.frame_index} 帧 · ${cameraNames[frame.camera_role] ?? frame.camera_role}`;
  byId("run-meta").textContent =
    `${runBaseText} · ${hasTemporalFusion ? "三帧逐相机推理" : "全部留出点模型 / LiDAR MAE " + frame.model.mae_m.toFixed(2) + " / " + frame.lidar_nearest.mae_m.toFixed(2) + " m"}${canvas2dMode ? " · Canvas 2D 兼容模式" : ""}`;
  for (const [name, path] of Object.entries(frame.images)) {
    const image = byId(`${name}-image`);
    if (image) image.src = `./${path}`;
  }
  if (frame.temporal) {
    byId("temporal-fill-image").src = `./${frame.temporal.fill_overlay}`;
  } else {
    byId("temporal-fill-image").removeAttribute("src");
  }
  const metrics = [
    metric(
      hasTemporalFusion ? "中间帧原始输出 MAE" : "模型有效点 MAE",
      frame.model_on_valid ? `${frame.model_on_valid.mae_m.toFixed(2)} m` : "—",
      `LiDAR ${frame.lidar_on_valid?.mae_m.toFixed(2) ?? "—"} m · ${frame.valid_holdout_returns} 个留出点`,
    ),
    metric(
      hasTemporalFusion ? "中间帧原始有效像素" : "有效模型像素",
      `${Math.round(frame.model_mask_fraction * 100)}%`,
      `${frame.surface.triangle_count.toLocaleString()} 个三角片`,
    ),
  ];
  if (frame.temporal) {
    const strictDisplay = byId("model-display").value === "strict";
    const comparison = strictDisplay
      ? frame.temporal.strict_model_on_valid
      : frame.temporal.model_on_valid;
    const lidarComparison = strictDisplay
      ? frame.temporal.strict_lidar_on_valid
      : frame.temporal.lidar_on_valid;
    const comparedReturns = strictDisplay
      ? frame.temporal.strict_valid_holdout_returns
      : frame.temporal.valid_holdout_returns;
    metrics.push(
      metric(
        strictDisplay ? "严格一致区域 MAE" : "融合有效点 MAE",
        comparison ? `${comparison.mae_m.toFixed(2)} m` : "—",
        `中间帧 LiDAR ${lidarComparison?.mae_m.toFixed(2) ?? "—"} m · ${comparedReturns} 个留出点`,
      ),
      metric(
        strictDisplay ? "严格一致像素" : "融合有效像素",
        `${Math.round((strictDisplay ? frame.temporal.strict_fraction : frame.temporal.fused_fraction) * 100)}%`,
        `跨帧补洞 ${Math.round(frame.temporal.filled_fraction * 100)}% · 严格一致 ${Math.round(frame.temporal.strict_fraction * 100)}%`,
      ),
    );
  } else {
    metrics.push(
      metric(
        "LiDAR 邻近 MAE",
        frame.lidar_on_valid ? `${frame.lidar_on_valid.mae_m.toFixed(2)} m` : "—",
        "与模型有效点完全相同的留出点",
      ),
      metric("表面三角片", frame.surface.triangle_count.toLocaleString(), "有效且深度连续"),
    );
  }
  byId("metrics").replaceChildren(...metrics);
  for (const button of byId("frame-tabs").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.index) === index));
  }
  for (const button of byId("overview-grid").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.index) === index));
  }
  depthValues = viewModels[index]?.depth ?? null;
  temporalDepthValues = viewModels[index]?.temporalDepth ?? null;
  lidarDepthValues = viewModels[index]?.lidarDepth ?? null;
  refreshCloudVisibility();
}

async function loadDepth(path, pixelCount) {
  const response = await fetch(`./${path}`);
  if (!response.ok) throw new Error(`无法读取 ${path}`);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength !== pixelCount * 4) throw new Error(`深度文件长度无效：${path}`);
  return new Float32Array(bytes);
}

async function loadView(frame) {
  const pixelCount = frame.size[0] * frame.size[1];
  const [
    lidar,
    model,
    confident,
    surface,
    temporalSurface,
    strictSurface,
    depth,
    temporalDepth,
    lidarDepth,
  ] = await Promise.all([
    loadCloud(frame.clouds.lidar, 2.7, 0.96),
    loadCloud(frame.clouds.model, 3, 0.75),
    loadCloud(frame.clouds.confident, 3, 0.85),
    loadSurface(frame.surface),
    frame.temporal ? loadSurface(frame.temporal.surface) : Promise.resolve(null),
    frame.temporal ? loadSurface(frame.temporal.strict_surface) : Promise.resolve(null),
    loadDepth(frame.depth_values, pixelCount),
    frame.temporal ? loadDepth(frame.temporal.depth_values, pixelCount) : Promise.resolve(null),
    loadDepth(frame.lidar_depth_values, pixelCount),
  ]);
  return {
    lidar,
    model,
    confident,
    surface,
    temporalSurface,
    strictSurface,
    depth,
    temporalDepth,
    lidarDepth,
  };
}

if (hasTemporalFusion) {
  byId("model-display").querySelector('option[value="surface"]').textContent = "中间帧原始表面";
  const option = document.createElement("option");
  option.value = "temporal";
  option.textContent = "三帧补全表面";
  const strictOption = document.createElement("option");
  strictOption.value = "strict";
  strictOption.textContent = "严格一致区域";
  byId("model-display").prepend(option);
  byId("model-display").append(strictOption);
  byId("model-display").value = "temporal";
  byId("temporal-fill-control").hidden = false;
}

data.frames.forEach((frame, index) => {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.index = String(index);
  button.textContent = `第 ${frame.frame_index} 帧 · ${cameraNames[frame.camera_role] ?? frame.camera_role}`;
  button.setAttribute("aria-pressed", String(index === 0));
  button.addEventListener("click", () => selectFrame(index));
  byId("frame-tabs").append(button);
  if ((data.cameras?.length ?? 1) > 1) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "overview-card";
    card.dataset.index = String(index);
    card.setAttribute("aria-pressed", String(index === 0));
    const image = document.createElement("img");
    image.src = `./${frame.images.rgb}`;
    image.alt = `${cameraNames[frame.camera_role] ?? frame.camera_role}相机原图`;
    const label = document.createElement("span");
    label.textContent = `${cameraNames[frame.camera_role] ?? frame.camera_role} · ${frame.temporal ? `融合有效 ${Math.round(frame.temporal.fused_fraction * 100)}%` : `模型有效 ${Math.round(frame.model_mask_fraction * 100)}%`}`;
    card.append(image, label);
    card.addEventListener("click", () => {
      selectFrame(index);
      byId("image-stage").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    byId("overview-grid").append(card);
  }
});

function showError(error) {
  byId("notes").textContent = `页面加载失败：${error.message}`;
  throw error;
}
byId("aggregate-cameras").checked = (data.cameras?.length ?? 1) > 1;
byId("aggregate-control").hidden = (data.cameras?.length ?? 1) < 2;
byId("overview").hidden = (data.cameras?.length ?? 1) < 2;
selectFrame(0);
byId("load-status").textContent = `正在加载 ${data.frames.length} 路表面…`;
Promise.all(data.frames.map(loadView))
  .then((loaded) => {
    viewModels = loaded;
    selectFrame(selected);
    byId("load-status").textContent = hasTemporalFusion
      ? `${loaded.length} 路已对齐 · 拖动查看表面，切换中间帧 / 三帧结果`
      : `${loaded.length} 路已对齐 · 拖动查看表面，切换相机检查接缝`;
  })
  .catch(showError);
