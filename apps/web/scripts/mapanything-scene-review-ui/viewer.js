import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const element = (id) => document.getElementById(id);
const response = await fetch("./data.json");
if (!response.ok) throw new Error(`无法读取场景数据：${response.status}`);
const data = await response.json();
const names = {
  CAM_FRONT: "前视",
  CAM_FRONT_RIGHT: "右前",
  CAM_BACK_RIGHT: "右后",
  CAM_BACK: "后视",
  CAM_BACK_LEFT: "左后",
  CAM_FRONT_LEFT: "左前",
};
const state = { frame: 0, camera: 0, scope: "scene", mode: "fused", lidar: false };
const stage = new THREE.Scene();
stage.background = new THREE.Color(0x0c1720);
const camera = new THREE.PerspectiveCamera(58, 1, 0.1, 1200);
camera.position.set(35, 38, 55);
const host = element("cloud-view");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
host.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxDistance = 300;
const grid = new THREE.GridHelper(240, 48, 0x506777, 0x2a3b46);
stage.add(grid);
const marker = new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(), 3, 0xffd378);
stage.add(marker);
let meshes = [];
let lidarPoints = null;
let activeKey = "";
let generation = 0;
let controller = null;

function view() {
  return data.frames[state.frame].views[state.camera];
}
function setCameraMarker() {
  const pose = view().camera_view;
  marker.position.set(...pose.position);
  marker.setDirection(new THREE.Vector3(...pose.forward).normalize());
}
function globalPreset() {
  camera.up.set(0, 1, 0);
  const bounds = data.scene_mesh[state.mode].bounds;
  const center = new THREE.Vector3(...bounds[0])
    .add(new THREE.Vector3(...bounds[1]))
    .multiplyScalar(0.5);
  center.y = 1.5;
  const extent = Math.max(bounds[1][0] - bounds[0][0], bounds[1][2] - bounds[0][2]);
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(extent * 0.4, extent * 0.35, extent * 0.5));
  controls.update();
}
function cameraPreset() {
  const pose = view().camera_view;
  const position = new THREE.Vector3(...pose.position);
  camera.position.copy(position);
  camera.up.set(...pose.up);
  controls.target.copy(position).addScaledVector(new THREE.Vector3(...pose.forward), 20);
  controls.update();
}
function disposeMeshes() {
  for (const mesh of meshes) {
    stage.remove(mesh);
    mesh.geometry.dispose();
    mesh.material.dispose();
  }
  meshes = [];
}
async function buffer(filename, signal) {
  const response = await fetch(`./${filename}`, { signal });
  if (!response.ok) throw new Error(`${filename}: HTTP ${response.status}`);
  return response.arrayBuffer();
}
async function surfaceMesh(surface, signal) {
  const [vertices, indices] = await Promise.all([
    buffer(surface.vertices, signal),
    buffer(surface.indices, signal),
  ]);
  if (
    vertices.byteLength !== surface.vertex_count * 16 ||
    indices.byteLength !== surface.triangle_count * 12
  )
    throw new Error(`网格尺寸不匹配：${surface.vertices}`);
  const records = new DataView(vertices);
  const count = surface.vertex_count;
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  for (let index = 0; index < count; index++) {
    const offset = index * 16;
    for (let axis = 0; axis < 3; axis++)
      positions[index * 3 + axis] = records.getFloat32(offset + axis * 4, true);
    for (let channel = 0; channel < 3; channel++)
      colors[index * 3 + channel] = records.getUint8(offset + 12 + channel) / 255;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(indices), 1));
  geometry.computeBoundingSphere();
  return new THREE.Mesh(
    geometry,
    new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide }),
  );
}
async function loadLidar(signal) {
  if (!state.lidar) {
    if (lidarPoints) {
      stage.remove(lidarPoints);
      lidarPoints.geometry.dispose();
      lidarPoints.material.dispose();
      lidarPoints = null;
    }
    return;
  }
  if (lidarPoints) return;
  const vertices = await buffer(data.scene_lidar.file, signal);
  if (vertices.byteLength !== data.scene_lidar.point_count * 16)
    throw new Error("LiDAR 数据尺寸不匹配");
  const records = new DataView(vertices);
  const positions = new Float32Array(data.scene_lidar.point_count * 3);
  for (let index = 0; index < data.scene_lidar.point_count; index++)
    for (let axis = 0; axis < 3; axis++)
      positions[index * 3 + axis] = records.getFloat32(index * 16 + axis * 4, true);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  lidarPoints = new THREE.Points(
    geometry,
    new THREE.PointsMaterial({ color: 0x45cdeb, size: 0.11, transparent: true, opacity: 0.58 }),
  );
  stage.add(lidarPoints);
}
async function updateGeometry() {
  const key = `${state.scope}/${state.mode}/${state.scope === "scene" ? "" : state.frame}/${state.scope === "camera" ? state.camera : ""}`;
  if (key === activeKey && state.lidar === Boolean(lidarPoints)) return;
  generation += 1;
  const ownGeneration = generation;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  element("status").textContent = "读取表面中…";
  try {
    if (key !== activeKey) {
      const surfaces =
        state.scope === "scene"
          ? [data.scene_mesh[state.mode]]
          : (state.scope === "frame" ? data.frames[state.frame].views : [view()]).map(
              (item) => item.temporal[state.mode === "strict" ? "strict_surface" : "surface"],
            );
      const settled = await Promise.allSettled(
        surfaces.map((surface) => surfaceMesh(surface, signal)),
      );
      const nextMeshes = settled
        .filter((item) => item.status === "fulfilled")
        .map((item) => item.value);
      const failure = settled.find((item) => item.status === "rejected");
      if (failure) {
        nextMeshes.forEach((item) => {
          item.geometry.dispose();
          item.material.dispose();
        });
        throw failure.reason;
      }
      if (ownGeneration !== generation) {
        nextMeshes.forEach((item) => {
          item.geometry.dispose();
          item.material.dispose();
        });
        return;
      }
      disposeMeshes();
      meshes = nextMeshes;
      meshes.forEach((item) => stage.add(item));
      activeKey = key;
    }
    await loadLidar(signal);
    if (ownGeneration === generation)
      element("status").textContent =
        `${meshes.length} 个表面 · ${meshes.reduce((count, item) => count + item.geometry.index.count / 3, 0).toLocaleString()} 三角片`;
  } catch (error) {
    if (error.name !== "AbortError") element("status").textContent = `加载失败：${error.message}`;
  }
}
function refreshSelection() {
  const frame = data.frames[state.frame];
  const current = view();
  element("frame-slider").value = String(state.frame);
  element("frame-label").textContent = `${state.frame + 1} / ${data.frames.length}`;
  element("camera-select").value = String(state.camera);
  element("image").src = `./${current.images.rgb}`;
  element("image-label").textContent =
    `${names[current.camera_role]} · ${current.size.join(" × ")}`;
  const temporal = current.temporal;
  const model = temporal.model_on_valid;
  const baseline = temporal.lidar_on_valid;
  element("metrics").innerHTML =
    `补洞有效像素 <strong>${(temporal.fused_fraction * 100).toFixed(1)}%</strong> · 严格区域 <strong>${(temporal.strict_fraction * 100).toFixed(1)}%</strong><br>留出点：模型 <strong>${model?.mae_m ?? "—"} m</strong> / 本帧 LiDAR 邻近 <strong>${baseline?.mae_m ?? "—"} m</strong> · ${temporal.valid_holdout_returns} 点<br>推理窗口：${frame.input_frames.map((index) => index + 1).join("、")} 帧`;
  element("camera-grid").replaceChildren(
    ...frame.views.map((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("aria-pressed", String(index === state.camera));
      const image = document.createElement("img");
      image.src = `./${item.images.rgb}`;
      image.alt = names[item.camera_role];
      const label = document.createElement("span");
      label.textContent = names[item.camera_role];
      button.append(image, label);
      button.addEventListener("click", () => {
        state.camera = index;
        refreshSelection();
      });
      return button;
    }),
  );
  setCameraMarker();
  updateGeometry();
}

element("subtitle").textContent =
  `${data.scene} · ${data.frames.length} 个关键帧 × ${data.cameras.length} 路相机 · ${data.device}`;
element("run-meta").textContent =
  `MapAnything ${data.model_revision.slice(0, 12)} · 累计模型推理 ${data.total_inference_s} s · 场景网格 ${data.scene_mesh.fused.triangle_count.toLocaleString()} 三角片 · LiDAR ${data.scene_lidar.point_count.toLocaleString()} 点。原始数据与审阅资源只在本机。`;
element("frame-slider").max = String(data.frames.length - 1);
element("camera-select").replaceChildren(
  ...data.cameras.map((role, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = names[role];
    return option;
  }),
);
element("frame-slider").addEventListener("input", (event) => {
  state.frame = Number(event.target.value);
  refreshSelection();
});
element("camera-select").addEventListener("change", (event) => {
  state.camera = Number(event.target.value);
  refreshSelection();
});
element("previous").addEventListener("click", () => {
  state.frame = Math.max(0, state.frame - 1);
  refreshSelection();
});
element("next").addEventListener("click", () => {
  state.frame = Math.min(data.frames.length - 1, state.frame + 1);
  refreshSelection();
});
element("scope").addEventListener("change", (event) => {
  state.scope = event.target.value;
  updateGeometry();
});
element("mode").addEventListener("change", (event) => {
  state.mode = event.target.value;
  updateGeometry();
});
element("show-lidar").addEventListener("change", (event) => {
  state.lidar = event.target.checked;
  updateGeometry();
});
element("overview-preset").addEventListener("click", globalPreset);
element("camera-preset").addEventListener("click", cameraPreset);
const resize = () => {
  const width = host.clientWidth,
    height = host.clientHeight;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
};
new ResizeObserver(resize).observe(host);
resize();
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(stage, camera);
}
animate();
globalPreset();
refreshSelection();
