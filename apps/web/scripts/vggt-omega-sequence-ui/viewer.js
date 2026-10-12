import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { calibratedProjection, clearControlsMotion, sourceCamera } from "../textured-mesh.js";
import { decodePoints, SequenceRenderer } from "./sequence-renderer.js";

const $ = (id) => document.getElementById(id);
const labels = ["前视", "右前", "右后", "后视", "左后", "左前"];
const format = (number) => Number(number).toLocaleString("zh-CN");
let data, renderer, camera, controls;
let frame = 0,
  selectedCamera = 0,
  cloud = null,
  actors = null;
let dirty = true,
  interacting = false,
  loaded = false,
  sourceView = false;
let cloudGeneration = 0,
  actorGeneration = 0,
  cloudPending,
  actorPending;
let playing = false,
  previousTime = 0;
const cache = new Map();
let imageGeneration = 0;
window.omegaSequence = { ready: false, errors: [] };

async function points(url, signal) {
  if (cache.has(url)) return cache.get(url);
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`点云读取失败：${response.status} ${url}`);
  const item = decodePoints(await response.arrayBuffer());
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  if (cache.size >= 7) cache.delete(cache.keys().next().value);
  cache.set(url, item);
  return item;
}

function fail(error) {
  if (error.name === "AbortError") return;
  window.omegaSequence.errors.push(String(error));
  $("load-state").textContent = `重建加载失败：${error.message}`;
  $("load-state").classList.add("error");
  $("load-state").hidden = false;
}

function cameraRow() {
  const row = data.cameras[frame * 6 + selectedCamera];
  return {
    ...row,
    size: data.grid,
    intrinsic: $("method").value === "raw" ? row.raw_intrinsic : row.intrinsic,
    viewer_from_camera_cv:
      $("method").value === "raw" ? row.raw_viewer_from_camera_cv : row.viewer_from_camera_cv,
  };
}

function updateCamera() {
  if (sourceView) {
    sourceCamera(camera, controls, cameraRow());
    calibratedProjection(camera, cameraRow(), renderer.width, renderer.height);
  } else {
    camera.aspect = renderer.width / renderer.height;
    camera.updateProjectionMatrix();
  }
  dirty = true;
}

function reset(top = false) {
  sourceView = false;
  clearControlsMotion(controls);
  $("view").value = top ? "top" : "free";
  const lo = data.bounds[0],
    hi = data.bounds[1];
  const centre = new THREE.Vector3()
    .fromArray(lo)
    .add(new THREE.Vector3().fromArray(hi))
    .multiplyScalar(0.5);
  centre.y = 0;
  const distance = Math.max(hi[0] - lo[0], hi[2] - lo[2], 30);
  controls.target.copy(centre);
  camera.up.set(0, 1, 0);
  camera.position
    .copy(centre)
    .add(
      top
        ? new THREE.Vector3(0, distance * 1.05, 0.01)
        : new THREE.Vector3(distance * 0.48, distance * 0.66, distance * 0.77),
    );
  controls.update();
  updateCamera();
}

function activeActors() {
  return $("actors").checked && ["calibrated", "fused"].includes($("method").value) ? actors : null;
}
function updatePoints() {
  renderer.setPoints(cloud, activeActors());
  $("actor-state").hidden =
    !window.omegaSequence.actorError ||
    !$("actors").checked ||
    !["calibrated", "fused"].includes($("method").value);
  dirty = true;
}

async function loadCloud() {
  const generation = ++cloudGeneration;
  cloudPending?.abort();
  cloudPending = new AbortController();
  loaded = false;
  window.omegaSequence.ready = false;
  renderer.canvas.style.visibility = "hidden";
  renderer.overlay.style.visibility = "hidden";
  $("load-state").classList.remove("error");
  $("load-state").textContent = "正在读取整段场景重建…";
  $("load-state").hidden = false;
  const method = $("method").value,
    asset = data.assets[method].lod[$("lod").value];
  $("actors").disabled = !["calibrated", "fused"].includes(method);
  try {
    const result = await points(asset.path, cloudPending.signal);
    if (generation !== cloudGeneration) return;
    cloud = result;
    updatePoints();
    loaded = true;
    $("cloud-label").textContent =
      `${data.methods[method]} · 完整 ${format(data.assets[method].points)} 点 · 当前显示 ${format(result.count)} 点`;
    $("stat-points").textContent = (data.assets.calibrated.points / 1000000).toFixed(2) + "M";
    $("load-state").hidden = true;
    renderer.canvas.style.visibility = "visible";
    renderer.overlay.style.visibility = "visible";
    updateCamera();
    Object.assign(window.omegaSequence, {
      ready: true,
      method,
      cloudPoints: result.count,
      fullPoints: data.assets[method].points,
      cloudGeneration: generation,
    });
  } catch (error) {
    if (generation === cloudGeneration) fail(error);
  }
}

async function loadActors() {
  const generation = ++actorGeneration;
  actorPending?.abort();
  actorPending = new AbortController();
  actors = null;
  window.omegaSequence.actorFrame = null;
  window.omegaSequence.actorError = null;
  updatePoints();
  try {
    const item = await points(`frames/${data.actors[frame].path}`, actorPending.signal);
    if (generation !== actorGeneration) return;
    actors = item;
    updatePoints();
    window.omegaSequence.actorFrame = frame;
  } catch (error) {
    if (generation === actorGeneration && error.name !== "AbortError") {
      window.omegaSequence.actorError = String(error);
      window.omegaSequence.errors.push(String(error));
      $("actor-state").textContent = "当前帧目标候选加载失败，切换帧可重试";
      updatePoints();
    }
  }
}

async function loadImages(row) {
  const generation = ++imageGeneration;
  $("input-image").style.visibility = "hidden";
  $("render-image").style.visibility = "hidden";
  window.omegaSequence.imageFrame = null;
  const next = [new Image(), new Image()];
  next[0].src = row.rgb;
  next[1].src = row.render;
  const decoded = await Promise.allSettled(next.map((image) => image.decode()));
  if (generation !== imageGeneration) return;
  if (decoded.some((result) => result.status === "rejected")) {
    window.omegaSequence.errors.push(`图像读取失败：frame ${row.frame}, ${row.camera}`);
    return;
  }
  $("input-image").src = row.rgb;
  $("render-image").src = row.render;
  await Promise.allSettled([$("input-image").decode(), $("render-image").decode()]);
  if (generation !== imageGeneration) return;
  $("input-link").href = row.rgb;
  $("render-link").href = row.render;
  $("input-image").style.visibility = "visible";
  $("render-image").style.visibility = "visible";
  window.omegaSequence.imageFrame = row.frame;
  window.omegaSequence.imageCamera = row.camera;
}

function setFrame(next, scroll = false) {
  frame = Math.max(0, Math.min(data.frames - 1, next));
  $("timeline").value = frame;
  const row = data.cameras[frame * 6 + selectedCamera];
  $("frame-label").textContent =
    `${String(frame + 1).padStart(2, "0")} / ${data.frames} · ${Math.max(0, data.cameras[frame * 6].seconds).toFixed(2)}s`;
  document
    .querySelectorAll(".frame")
    .forEach((button, i) => button.setAttribute("aria-pressed", i === frame));
  if (scroll) {
    const strip = $("strip"),
      button = strip.children[frame];
    strip.scrollTo({
      left: button.offsetLeft - strip.offsetLeft - strip.clientWidth / 2 + button.clientWidth / 2,
      behavior: "auto",
    });
  }
  loadImages(row);
  $("rgb-camera").textContent = labels[selectedCamera];
  window.omegaSequence.frame = frame;
  window.omegaSequence.selectedCamera = selectedCamera;
  updateCamera();
  loadActors();
  dirty = true;
}

function playState(value) {
  playing = value;
  previousTime = performance.now();
  $("play").textContent = playing ? "Ⅱ 暂停" : "▶ 播放";
  $("play").setAttribute("aria-label", playing ? "暂停序列" : "播放序列");
  window.omegaSequence.playing = playing;
}

try {
  const response = await fetch("sequence-data.json");
  if (!response.ok) throw new Error(`场景清单读取失败 ${response.status}`);
  data = await response.json();
  renderer = new SequenceRenderer($("stage"), data.grid);
  camera = new THREE.PerspectiveCamera(50, 1, 0.05, 1500);
  controls = new OrbitControls(camera, renderer.canvas);
  controls.enableDamping = true;
  controls.maxDistance = 1200;
  controls.minDistance = 0.1;
  controls.addEventListener("change", () => {
    dirty = true;
  });
  controls.addEventListener("start", () => {
    interacting = true;
    if (sourceView) {
      sourceView = false;
      $("view").value = "free";
      camera.up.set(0, 1, 0);
      updateCamera();
    }
  });
  controls.addEventListener("end", () => {
    interacting = false;
    dirty = true;
  });
  new ResizeObserver(() => {
    const r = $("stage").getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    renderer.resize(Math.round(r.width), Math.round(r.height));
    updateCamera();
  }).observe($("stage"));
  const rect = $("stage").getBoundingClientRect();
  renderer.resize(Math.round(rect.width), Math.round(rect.height));
  reset();
  $("stat-frames").textContent = data.frames;
  $("stat-images").textContent = data.input_images;
  $("stat-seconds").textContent = data.seconds.toFixed(1);
  $("stat-runtime").textContent = data.inference_seconds.toFixed(1);
  $("render-grid").textContent = `${data.grid[0]} × ${data.grid[1]}`;
  $("timeline").max = data.frames - 1;
  for (let f = 0; f < data.frames; f++) {
    const button = document.createElement("button");
    button.className = "frame";
    button.setAttribute("aria-label", `关键帧 ${f + 1}`);
    button.setAttribute("aria-pressed", f === 0);
    const image = document.createElement("img");
    image.src = data.cameras[f * 6].rgb;
    image.alt = `第 ${f + 1} 帧前视缩略图`;
    image.loading = "lazy";
    const label = document.createElement("span");
    label.textContent = `${String(f + 1).padStart(2, "0")} · ${data.cameras[f * 6].seconds.toFixed(2)}s`;
    button.append(image, label);
    button.onclick = () => {
      playState(false);
      setFrame(f, true);
    };
    $("strip").append(button);
  }
  labels.forEach((label, i) => {
    const button = document.createElement("button");
    button.textContent = label;
    button.setAttribute("aria-pressed", i === 0);
    button.onclick = () => {
      selectedCamera = i;
      [...$("cameras").children].forEach((b, j) => b.setAttribute("aria-pressed", i === j));
      setFrame(frame);
    };
    $("cameras").append(button);
  });
  $("method").onchange = loadCloud;
  $("lod").onchange = loadCloud;
  $("actors").onchange = updatePoints;
  $("trajectory").onchange = () => {
    dirty = true;
  };
  $("timeline").oninput = () => {
    playState(false);
    setFrame(Number($("timeline").value));
  };
  $("play").onclick = () => {
    if (frame === data.frames - 1 && !playing) setFrame(0);
    playState(!playing);
  };
  $("reset").onclick = () => reset();
  $("view").onchange = () => {
    if ($("view").value === "source") {
      sourceView = true;
      updateCamera();
    } else reset($("view").value === "top");
  };
  const validation = data.validation;
  const items = [
    `${data.checkpoint} · 输入 ${data.grid[0]} × ${data.grid[1]}`,
    `${data.input_images} / ${data.input_images} 图联合参与`,
    `三整帧 LiDAR 留出 · ${format(validation.targets)} 个静态目标`,
    `校准模型 ≤0.5m 正确支持 ${(validation.metrics.calibrated["correct_coverage_0.5m"] * 100).toFixed(1)}%`,
    `原始预测相机 Sim3 拟合残差 ${data.pose.camera_centres_rmse_m.toFixed(2)}m`,
    `显示后端 ${renderer.backend}`,
  ];
  for (const text of items) {
    const span = document.createElement("span");
    span.textContent = text;
    $("evidence").append(span);
  }
  Object.assign(window.omegaSequence, {
    data,
    camera,
    controls,
    renderer,
    backend: renderer.backend,
    setFrame,
  });
  setFrame(0);
  await loadCloud();
  function animate(now) {
    requestAnimationFrame(animate);
    const frameDuration =
      frame < data.frames - 1
        ? (data.cameras[(frame + 1) * 6].seconds - data.cameras[frame * 6].seconds) * 1000
        : 500;
    if (playing && now - previousTime >= frameDuration / Number($("speed").value)) {
      previousTime = now;
      if (frame < data.frames - 1) setFrame(frame + 1, true);
      else playState(false);
    }
    controls.update();
    if (!dirty || !loaded) return;
    if (sourceView) calibratedProjection(camera, cameraRow(), renderer.width, renderer.height);
    const started = performance.now();
    const rendered = renderer.render(
      camera,
      data.cameras,
      frame,
      $("trajectory").checked,
      $("method").value,
      interacting,
    );
    const milliseconds = performance.now() - started;
    $("render-status").textContent =
      `${renderer.backend} · ${milliseconds.toFixed(0)}ms / 重绘 · 白色为缺失几何`;
    window.omegaSequence.render = { ...rendered, milliseconds, frame, sourceView };
    dirty = false;
  }
  requestAnimationFrame(animate);
} catch (error) {
  fail(error);
}
