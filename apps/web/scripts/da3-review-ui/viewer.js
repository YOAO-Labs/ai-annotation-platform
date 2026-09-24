import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const element = (id) => document.getElementById(id);
const response = await fetch("./data.json");
if (!response.ok) throw new Error(`审阅数据读取失败：${response.status}`);
const data = await response.json();
element("title").textContent = `${data.model_id.split("/")[1]} · 场景模型审阅`;
element("notice").textContent =
  `${data.input_mode === "single_rgb_metric" ? "单张 RGB 与内参输入 DA3，按处理后焦距 × 模型输出 / 300 换算为米。" : "相邻三帧 RGB、标定和位姿输入 DA3。"}LiDAR 仅用于留出点评分和显示网格的冲突检查。模型未读取稀疏 LiDAR，数值不能当作与 MapAnything 相同输入条件的胜负结论。`;
const cameraNames = {
  CAM_FRONT: "前视",
  CAM_FRONT_RIGHT: "右前",
  CAM_BACK_RIGHT: "右后",
  CAM_BACK: "后视",
  CAM_BACK_LEFT: "左后",
  CAM_FRONT_LEFT: "左前",
};
let selectedFrame = data.anchors[0];
let selectedCamera = data.cameras[0];
let scope = "camera";
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1720);
const camera = new THREE.PerspectiveCamera(58, 1, 0.1, 800);
const host = element("scene");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
host.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxDistance = 220;
scene.add(new THREE.GridHelper(140, 28, 0x4b6170, 0x283c4a));
let meshes = [];
let generation = 0;
let controller = null;

const current = () =>
  data.views.find(
    (view) => view.frame_index === selectedFrame && view.camera_role === selectedCamera,
  );
function cameraPreset() {
  const pose = current().camera_view;
  const position = new THREE.Vector3(...pose.position);
  camera.position.copy(position);
  camera.up.set(...pose.up);
  controls.target.copy(position).addScaledVector(new THREE.Vector3(...pose.forward), 20);
  controls.update();
}
function overviewPreset() {
  const pose = current().camera_view;
  const position = new THREE.Vector3(...pose.position);
  camera.up.set(0, 1, 0);
  controls.target.copy(position).addScaledVector(new THREE.Vector3(...pose.forward), 15);
  camera.position.copy(controls.target).add(new THREE.Vector3(35, 24, 42));
  controls.update();
}
async function fetchBuffer(filename, signal) {
  const response = await fetch(`./${filename}`, { signal });
  if (!response.ok) throw new Error(`${filename}: HTTP ${response.status}`);
  return response.arrayBuffer();
}
async function loadSurface(surface, signal) {
  const [vertices, indices] = await Promise.all([
    fetchBuffer(surface.vertices, signal),
    fetchBuffer(surface.indices, signal),
  ]);
  if (
    vertices.byteLength !== surface.vertex_count * 16 ||
    indices.byteLength !== surface.triangle_count * 12
  )
    throw new Error("表面网格尺寸不匹配");
  const records = new DataView(vertices);
  const xyz = new Float32Array(surface.vertex_count * 3);
  const rgb = new Float32Array(surface.vertex_count * 3);
  for (let index = 0; index < surface.vertex_count; index++) {
    const offset = index * 16;
    for (let axis = 0; axis < 3; axis++)
      xyz[index * 3 + axis] = records.getFloat32(offset + axis * 4, true);
    for (let channel = 0; channel < 3; channel++)
      rgb[index * 3 + channel] = records.getUint8(offset + 12 + channel) / 255;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(xyz, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(rgb, 3));
  geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(indices), 1));
  geometry.computeBoundingSphere();
  return new THREE.Mesh(
    geometry,
    new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide }),
  );
}
async function update() {
  const view = current();
  element("rgb").src = `./${view.images.rgb}`;
  element("depth").src = `./${view.images.depth}`;
  element("metrics").innerHTML =
    `帧 ${selectedFrame + 1} / ${data.scene_sample_count} · ${cameraNames[selectedCamera]} · ${view.size.join(" × ")}<br>有效像素 <strong>${(view.valid_fraction * 100).toFixed(1)}%</strong> · 留出点 ${view.valid_holdout_returns} / ${view.holdout_returns}<br>模型 MAE <strong>${view.model_on_valid?.mae_m ?? "—"} m</strong> · 同点 LiDAR 邻近 <strong>${view.lidar_on_valid?.mae_m ?? "—"} m</strong><br>本组推理 ${view.inference_s} s`;
  generation += 1;
  const ownGeneration = generation;
  controller?.abort();
  controller = new AbortController();
  element("status").textContent = "读取表面中…";
  try {
    const views =
      scope === "frame" ? data.views.filter((item) => item.frame_index === selectedFrame) : [view];
    const settled = await Promise.allSettled(
      views.map((item) => loadSurface(item.surface, controller.signal)),
    );
    const loaded = settled.filter((item) => item.status === "fulfilled").map((item) => item.value);
    const failure = settled.find((item) => item.status === "rejected");
    if (failure) {
      loaded.forEach((item) => {
        item.geometry.dispose();
        item.material.dispose();
      });
      throw failure.reason;
    }
    if (ownGeneration !== generation) {
      loaded.forEach((item) => {
        item.geometry.dispose();
        item.material.dispose();
      });
      return;
    }
    meshes.forEach((item) => {
      scene.remove(item);
      item.geometry.dispose();
      item.material.dispose();
    });
    meshes = loaded;
    meshes.forEach((item) => scene.add(item));
    element("status").textContent =
      `${meshes.length} 个表面 · ${meshes.reduce((count, item) => count + item.geometry.index.count / 3, 0).toLocaleString()} 三角片`;
  } catch (error) {
    if (error.name !== "AbortError") element("status").textContent = `加载失败：${error.message}`;
  }
}

element("frame").replaceChildren(
  ...data.anchors.map((frame) => {
    const option = document.createElement("option");
    option.value = String(frame);
    option.textContent = `${frame + 1} / ${data.scene_sample_count}`;
    return option;
  }),
);
element("camera").replaceChildren(
  ...data.cameras.map((role) => {
    const option = document.createElement("option");
    option.value = role;
    option.textContent = cameraNames[role];
    return option;
  }),
);
element("frame").addEventListener("change", (event) => {
  selectedFrame = Number(event.target.value);
  update();
});
element("camera").addEventListener("change", (event) => {
  selectedCamera = event.target.value;
  update();
});
element("scope").addEventListener("change", (event) => {
  scope = event.target.value;
  update();
});
element("overview").addEventListener("click", overviewPreset);
element("camera-view").addEventListener("click", cameraPreset);
element("meta").textContent =
  `${data.scene} · ${data.model_id} ${data.model_revision.slice(0, 12)} · ${data.device} · ${data.views.length} 个预览视角。`;
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
  renderer.render(scene, camera);
}
animate();
cameraPreset();
update();
