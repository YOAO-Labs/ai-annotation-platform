import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { SoftwarePointRenderer } from "./software-points.js";
import {
  calibratedProjection,
  clearControlsMotion,
  disposeLayer,
  loadTexturedMesh,
  sourceCamera,
} from "./textured-mesh.js";

const $ = (id) => document.getElementById(id);
const response = await fetch("./data.json");
if (!response.ok) throw new Error(`无法读取试验数据：${response.status}`);
const data = await response.json();
const labels = {
  CAM_FRONT: "前视",
  CAM_FRONT_RIGHT: "右前",
  CAM_BACK_RIGHT: "右后",
  CAM_BACK: "后视",
  CAM_BACK_LEFT: "左后",
  CAM_FRONT_LEFT: "左前",
};
for (const [value, name] of Object.entries(data.methods)) $("method").add(new Option(name, value));
$("method").value = "fused";
const host = $("stage");
const renderer = (() => {
  try {
    return new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  } catch {
    return new SoftwarePointRenderer();
  }
})();
if (renderer) {
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setScissorTest(true);
  host.prepend(renderer.domElement);
}
const camera = new THREE.PerspectiveCamera(55, 1, 0.05, 300);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxDistance = 200;
controls.addEventListener("change", () => {
  renderer.needsRedraw = true;
});
const scenes = [new THREE.Scene(), new THREE.Scene()];
for (const scene of scenes) {
  scene.background = new THREE.Color(0x101820);
  const grid = new THREE.GridHelper(160, 32, 0x405563, 0x24323e);
  grid.position.y = -1.8;
  scene.add(grid);
  scene.add(new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(), 4, 0xe8f1f4));
}
let layers = [[], []];
let generation = 0;
let pending = null;
let loaded = false;
let currentRun;
let meshLayer = null;
let activeSourceCamera = null;
const isMesh = () => $("mode").value === "mesh" && currentRun?.mesh;
const percent = (value) => (value == null ? "—" : `${(value * 100).toFixed(1)}%`);
const metres = (value) => (value == null ? "—" : `${value.toFixed(2)}m`);
const number = (value) => Number(value).toLocaleString("zh-CN");
function reset() {
  clearControlsMotion(controls);
  activeSourceCamera = null;
  $("view").value = "free";
  camera.up.set(0, 1, 0);
  camera.position.set(24, 18, 28);
  controls.target.set(0, -1, -12);
  controls.update();
  if (window.omegaReview) window.omegaReview.meshCamera = null;
}
reset();
$("view").value = "CAM_FRONT";
$("reset").onclick = reset;

function makePoints(buffer, kind) {
  const count = buffer.byteLength / 16;
  if (!Number.isInteger(count) || kind.length !== count) throw new Error("点云缓冲区长度不一致");
  const view = new DataView(buffer);
  const xyz = new Float32Array(count * 3),
    rgb = new Float32Array(count * 3);
  for (let i = 0; i < count; i++)
    for (let axis = 0; axis < 3; axis++) {
      xyz[i * 3 + axis] = view.getFloat32(i * 16 + axis * 4, true);
      rgb[i * 3 + axis] = view.getUint8(i * 16 + 12 + axis) / 255;
    }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(xyz, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(rgb, 3));
  geometry.setAttribute("kind", new THREE.BufferAttribute(new Float32Array(kind), 1));
  const material = new THREE.ShaderMaterial({
    uniforms: {
      pointSize: { value: 1.5 },
      measured: { value: true },
      generated: { value: true },
      actors: { value: false },
      oracle: { value: false },
      provenance: { value: true },
    },
    vertexShader: `attribute vec3 color; attribute float kind; varying vec3 vColor; varying float vKind; uniform float pointSize;
      void main(){vColor=color;vKind=kind;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);gl_PointSize=pointSize;}`,
    fragmentShader: `varying vec3 vColor; varying float vKind; uniform bool measured; uniform bool generated; uniform bool actors; uniform bool oracle; uniform bool provenance;
      void main(){if(length(gl_PointCoord-vec2(.5))>.5)discard;
        if(vKind<.5&&!measured)discard;if(vKind>.5&&vKind<1.5&&!generated)discard;
        if(vKind>1.5&&vKind<2.5&&!actors)discard;if(vKind>2.5&&!oracle)discard;
        vec3 c=vColor;if(provenance){c=vKind<.5?vec3(.286,.784,.925):(vKind<1.5?vec3(.404,.863,.753):vec3(.957,.655,.298));}gl_FragColor=vec4(c,1.);}`,
  });
  return new THREE.Points(geometry, material);
}
function updateVisibility() {
  renderer.needsRedraw = true;
  for (const layer of layers.flat()) {
    for (const name of ["measured", "generated", "actors", "oracle", "provenance"])
      layer.material.uniforms[name].value = $(name).checked;
    layer.material.uniforms.pointSize.value = Number($("size").value) * renderer.getPixelRatio();
  }
  if (layers[1][0]) layers[1][0].visible = !isMesh();
  if (meshLayer) {
    meshLayer.visible = Boolean(isMesh());
    meshLayer.traverse((object) => {
      if (!object.isMesh) return;
      object.visible =
        object.userData.omegaKind === 1 ? $("generated").checked : $("actors").checked;
      object.userData.provenance = $("provenance").checked;
      const map = object.userData.provenance ? null : object.userData.sourceTexture;
      if (object.material.map !== map) {
        object.material.map = map;
        object.material.needsUpdate = true;
      }
      object.material.color.set(
        object.userData.provenance
          ? object.userData.omegaKind === 1
            ? 0x67dcc0
            : 0xf4a74c
          : 0xffffff,
      );
    });
  }
}
function images() {
  $("images").replaceChildren();
  for (const image of currentRun.images) {
    const figure = document.createElement("figure"),
      link = document.createElement("a"),
      img = document.createElement("img"),
      caption = document.createElement("figcaption");
    const render = currentRun.mesh?.renders.find((r) => r.camera === image.camera);
    const renderPath = $("actors").checked
      ? $("generated").checked
        ? render?.all_render
        : render?.actors_render
      : render?.render;
    img.src =
      isMesh() && $("overlay").checked
        ? renderPath
        : `./runs/${currentRun.id}/${$("overlay").checked && !isMesh() ? image.overlay : image.rgb}`;
    if (isMesh() && $("overlay").checked && !$("actors").checked && !$("generated").checked)
      img.style.visibility = "hidden";
    img.alt = `${labels[image.camera]} ${$("overlay").checked ? (isMesh() ? "纹理网格 RGB 重建" : "融合点云留出深度误差") : "来源 RGB"}`;
    link.href = img.src;
    link.target = "_blank";
    link.rel = "noopener";
    caption.textContent = labels[image.camera];
    link.append(img);
    figure.append(link, caption);
    $("images").append(figure);
  }
}
function metrics() {
  $("delta-label").textContent = isMesh() ? "纹理网格三角面" : "相对多帧 LiDAR · 新增 / 损失";
  $("density-label").textContent = isMesh() ? "网格使用顶点" : "生成静态点 · 新增体素";
  $("image-mode-label").textContent = isMesh()
    ? "网格 RGB 重建（取消勾选看来源 RGB）"
    : "融合点云留出评分：绿 ≤0.5m，红超差，灰无支持";
  const method = $("method").value;
  const row = currentRun.metrics[method],
    base = currentRun.metrics.temporal_lidar.static,
    stat = row.static;
  const delta = row.relative_to_temporal_lidar["0.5m"];
  const count = currentRun.clouds[method].counts;
  $("right-title").textContent = data.methods[method];
  $("left-count").textContent =
    `${number(currentRun.clouds.temporal_lidar.counts.measured_static)} 个实测静态点`;
  $("right-count").textContent =
    `${number(count.measured_static + count.generated_static)} 个静态点 · ${number(count.actor_diagnostic)} 个动态诊断点`;
  $("correct").textContent = percent(stat["correct_coverage_0.5m"]);
  $("correct-baseline").textContent =
    `多帧 LiDAR ${percent(base["correct_coverage_0.5m"])} · 固定 ${number(stat.targets)} 个目标`;
  $("delta").textContent =
    `+${percent(delta.new_correct_all_target_fraction)} / −${percent(delta.lost_correct_all_target_fraction)}`;
  $("delta-caption").textContent =
    `新增 ${number(delta.new_correct_targets)}，损失 ${number(delta.lost_correct_targets)} 个目标`;
  $("error").textContent = `${metres(stat.mae_m)} / ${metres(stat.p90_m)}`;
  $("support").textContent = `支持率 ${percent(stat.coverage)} · 误差仅计算有支持目标`;
  $("density").textContent = number(count.generated_static);
  $("density-caption").textContent =
    `筛选候选在实测以外新增 ${number(currentRun.new_static_voxels)} 个静态体素`;
  $("method").disabled = Boolean(isMesh());
  $("novel-render").hidden = !isMesh();
  if (isMesh()) $("novel-render").href = currentRun.mesh.novel_render;
  if (isMesh()) {
    const stat = currentRun.mesh.metrics.static;
    $("right-title").textContent = "RGB 纹理网格 · 筛选几何";
    $("right-count").textContent =
      `${number(currentRun.mesh.triangles)} 个总三角面 · 原始 RGB 纹理`;
    $("correct").textContent = percent(stat["correct_coverage_0.5m"]);
    $("correct-baseline").textContent =
      `网格实际覆盖像素评分 · 固定 ${number(stat.targets)} 个目标`;
    $("delta").textContent = number(currentRun.mesh.triangles);
    $("delta-caption").textContent = "总三角面（含动态诊断层）· 不跨筛选孔洞";
    $("error").textContent = `${metres(stat.mae_m)} / ${metres(stat.p90_m)}`;
    $("support").textContent = `网格支持率 ${percent(stat.coverage)} · 足迹与点云评分不同`;
    $("density").textContent = number(currentRun.mesh.used_vertices);
    $("density-caption").textContent = "使用的冻结顶点 · 坐标未经 RGB 优化";
  }
  $("table").replaceChildren();
  for (const [key, name] of Object.entries(data.methods)) {
    const stat = currentRun.metrics[key].static,
      count = currentRun.clouds[key].counts;
    const tr = document.createElement("tr");
    for (const value of [
      name,
      number(count.measured_static + count.generated_static),
      percent(stat.coverage),
      percent(stat["correct_coverage_0.2m"]),
      percent(stat["correct_coverage_0.5m"]),
      percent(stat["correct_coverage_1m"]),
      metres(stat.mae_m),
      metres(stat.p90_m),
      metres(currentRun.metrics[key].common_all_methods_static.mae_m),
    ]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    $("table").append(tr);
  }
  const resource = data.inference.runs.find((r) => r.id === currentRun.id);
  $("evidence").textContent =
    `${currentRun.id} · ${currentRun.split === "validation" ? "独立场景验证" : "开发场景"}\n${currentRun.window * 6} 张 RGB，输入包含中心图像；整次中心 LiDAR 未参与校正与基线。\n唯一可见回波 ${number(currentRun.unique_visible_holdout_returns)}；各相机合计 ${number(currentRun.view_return_observations)}，已去除重复计数。\n校准尺度 ${currentRun.scale_fit.scale.toFixed(4)}；独立锚点 ${number(currentRun.scale_fit.unique_anchor_returns)}。\nRGB−中心LiDAR时间差：${currentRun.centre_rgb_minus_lidar_us.map((t) => `${(t / 1000).toFixed(1)}ms`).join("，")}。\n前向 ${resource.seconds.toFixed(2)}s；PyTorch峰值分配 ${(resource.peak_allocated_mib / 1024).toFixed(2)}GiB。\n同一模型窗口内的时间支持属于一致性证据；动态层不跨时间累计。`;
  images();
}
async function fetchPoints(asset, signal) {
  const responses = await Promise.all([
    fetch(asset.url, { signal }),
    fetch(asset.kind_url, { signal }),
  ]);
  for (const response of responses)
    if (!response.ok) throw new Error(`点云读取失败：HTTP ${response.status}`);
  const buffers = await Promise.all(responses.map((r) => r.arrayBuffer()));
  return makePoints(buffers[0], new Uint8Array(buffers[1]));
}
async function load() {
  const mine = ++generation;
  pending?.abort();
  pending = new AbortController();
  const signal = pending.signal;
  loaded = false;
  renderer.domElement.style.visibility = "hidden";
  if (window.omegaReview) window.omegaReview.loaded = false;
  $("status").textContent = "正在加载三维几何与纹理…";
  currentRun = data.runs.find(
    (r) =>
      r.scene === $("scene").value &&
      r.centre === Number($("centre").value) &&
      r.window === Number($("window").value),
  );
  metrics();
  const method = $("method").value;
  const assets = [
    currentRun.clouds.temporal_lidar,
    currentRun.clouds[method],
    currentRun.clouds.heldout_oracle,
    currentRun.clouds.heldout_oracle,
  ];
  const requests = assets.map((asset) => fetchPoints(asset, signal));
  if (currentRun.mesh) requests.push(loadTexturedMesh(currentRun.mesh, signal));
  const settled = await Promise.allSettled(requests);
  const points = settled.filter((r) => r.status === "fulfilled").map((r) => r.value);
  if (mine !== generation || settled.some((r) => r.status === "rejected")) {
    for (const point of points) {
      disposeLayer(point);
    }
    if (mine === generation) $("status").textContent = "加载失败，请检查本地服务与控制台";
    return;
  }
  for (let i = 0; i < 2; i++) {
    for (const point of layers[i]) {
      scenes[i].remove(point);
      disposeLayer(point);
    }
    layers[i] = [settled[i].value, settled[i + 2].value];
    for (const point of layers[i]) scenes[i].add(point);
  }
  if (meshLayer) {
    scenes[1].remove(meshLayer);
    disposeLayer(meshLayer);
    meshLayer = null;
  }
  if (currentRun.mesh) {
    meshLayer = settled[4].value;
    scenes[1].add(meshLayer);
  }
  if ($("view").value !== "free" && currentRun.mesh) {
    activeSourceCamera = currentRun.mesh.cameras.find((r) => r.camera === $("view").value);
    sourceCamera(camera, controls, activeSourceCamera);
  }
  updateVisibility();
  loaded = true;
  renderer.domElement.style.visibility = "visible";
  $("status").textContent =
    `${currentRun.id} · 已加载 · 左右视角同步${renderer.isSoftwareRenderer ? " · CPU 兼容绘制" : ""}`;
  window.omegaReview = {
    run: currentRun.id,
    method,
    loaded,
    pointCounts: assets.slice(0, 2).map((a) => a.counts.total),
    renderer: renderer.isSoftwareRenderer ? "Canvas2D (CPU)" : "WebGL2",
    camera,
    controls,
    mode: $("mode").value,
    meshCamera: activeSourceCamera,
    triangles: currentRun.mesh?.triangles,
  };
}
for (const name of ["scene", "centre", "window", "method"])
  $(name).onchange = () =>
    load().catch((e) => {
      console.error(e);
      $("status").textContent = e.message;
    });
for (const name of ["measured", "generated", "actors", "oracle", "provenance", "size"])
  $(name).oninput = () => {
    updateVisibility();
    if (["generated", "actors"].includes(name)) images();
  };
$("overlay").onchange = images;
$("mode").onchange = () => {
  metrics();
  updateVisibility();
  if (window.omegaReview) window.omegaReview.mode = $("mode").value;
};
$("view").onchange = () => {
  if ($("view").value === "free") reset();
  else if (currentRun.mesh) {
    activeSourceCamera = currentRun.mesh.cameras.find((r) => r.camera === $("view").value);
    sourceCamera(camera, controls, activeSourceCamera);
  }
  renderer.needsRedraw = true;
  if (window.omegaReview) window.omegaReview.meshCamera = activeSourceCamera;
};
function render() {
  const width = host.clientWidth,
    height = host.clientHeight;
  if (width < 2 || height < 1) {
    requestAnimationFrame(render);
    return;
  }
  if (
    renderer.domElement.clientWidth !== width ||
    renderer.domElement.clientHeight !== height ||
    renderer.domElement.width !== Math.round(width * renderer.getPixelRatio()) ||
    renderer.domElement.height !== Math.round(height * renderer.getPixelRatio())
  )
    renderer.setSize(width, height, false);
  controls.update();
  if (renderer.isSoftwareRenderer && !renderer.needsRedraw) {
    requestAnimationFrame(render);
    return;
  }
  camera.aspect = width / 2 / height;
  if (activeSourceCamera) calibratedProjection(camera, activeSourceCamera, width / 2, height);
  else camera.updateProjectionMatrix();
  for (let i = 0; i < 2; i++) {
    renderer.setViewport((i * width) / 2, 0, width / 2, height);
    renderer.setScissor((i * width) / 2, 0, width / 2, height);
    renderer.render(scenes[i], camera);
  }
  renderer.needsRedraw = false;
  requestAnimationFrame(render);
}
if (renderer) render();
await load();
