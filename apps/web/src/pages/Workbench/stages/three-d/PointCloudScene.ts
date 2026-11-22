import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { TransformControls } from "three/examples/jsm/controls/TransformControls.js";

import type { LidarAxisConvention } from "./geometry/axisConvention";
import { isPointInPolygon, type ScreenPoint } from "./geometry/pointInPolygon";
import { framePerspectiveBox } from "./geometry/viewFraming";
import type { Psr, TriView } from "./geometry/triview";
import { loadTimedDecodedPointCloudFrame } from "./pointCloudAssetCache";
import {
  clientRectToCanvasClipPath,
  clientRectToRendererScissors,
  setClientCameraViewport,
  subtractClientRect,
  PointCloudTriViewPass,
  type ClientRectSnapshot,
  type PointCloudVisibleRegions,
  type TriViewClientLayout,
} from "./PointCloudTriViewPass";
import {
  createPointCloudRenderer,
  disposePointCloudRenderer,
  type PointCloudRenderer,
  type PointCloudRendererMode,
  type PointCloudRendererStatus,
  type PointCloudRendererSurface,
} from "./rendering/pointCloudRenderer";
import {
  createWebGpuPointCloudLayer,
  type WebGpuPointCloudLayer,
} from "./rendering/webgpuPointCloudLayer";
import type { GpuCameraTextureSample } from "./rendering/cameraTextureColorNode";
import type { ColorAdjust } from "./geometry/colorize";
import type { MeasurementPosition } from "./geometry/measurement";
import {
  POINT_CLOUD_RENDER_ALL,
  POINT_CLOUD_RENDER_MAIN,
  POINT_CLOUD_RENDER_TRI,
  PointCloudRenderScheduler,
  resolvePointCloudRenderPlan,
} from "./rendering/pointCloudRenderScheduler";

const DEFAULT_DECIMATE_THRESHOLD = 500_000;
const WEBGPU_MIN_POINT_CAPACITY = 65_536;
const POINT_CLOUD_TEST_PROBES_ENABLED = import.meta.env.DEV || import.meta.env.MODE === "e2e";

function webGpuPointCapacity(pointCount: number): number {
  let capacity = WEBGPU_MIN_POINT_CAPACITY;
  while (capacity < pointCount) capacity *= 2;
  return capacity;
}

const NEIGHBOR_PAST_COLOR = 0x4a90d9;
const NEIGHBOR_FUTURE_COLOR = 0xd98a4a;
function neighborOpacity(distance: number): number {
  return Math.max(0.15, 0.5 - (Math.max(1, distance) - 1) * 0.12);
}

interface NeighborPointLayer {
  object: THREE.Object3D;
  geometry: THREE.BufferGeometry;
  material: THREE.PointsMaterial | null;
  webGpuLayer: WebGpuPointCloudLayer | null;
}

const SELECTED_EDGE_COLOR = 0xffd54a;
const TRANSFORM_SIZE_MIN = 0.35;
const TRANSFORM_SIZE_MAX = 1.15;

export interface PointCloudStats {
  totalPoints: number;
  renderedPoints: number;
  decimated: boolean;
  decimateStride: number;
}

export interface SceneBox {
  id: string;
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  color: string;
  selected: boolean;
  label?: string;
}

export interface ReferenceBox {
  id: string;
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  color: string;
  dim?: boolean;
}

export interface SceneMeasurementPath {
  id: string;
  positions: readonly MeasurementPosition[];
  active: boolean;
}

export interface PointCloudPick {
  pointIndex: number;
  position: MeasurementPosition;
}

export interface BoxPsr {
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
}

export interface PointMaskSelection {
  pointIndices: number[];
  decimateStride: number;
  sourcePointCount: number;
}

export interface PointCloudViewState {
  position: [number, number, number];
  target: [number, number, number];
  up: [number, number, number];
  mode: "orbit" | "bev";
}

export interface PointCloudLoadOptions {
  shouldCommit?: () => boolean;
  visible?: boolean;
}

type TransformMode = "translate" | "rotate" | "scale";

export interface PointCloudSceneOptions {
  decimateThreshold?: number;
  rendererMode?: PointCloudRendererMode;
  onDeviceLost?: (reason: string) => void;
  renderSurface?: HTMLElement | null;
  getVisibleRegions?: PointCloudVisibleRegions;
}

export class PointCloudScene {
  private renderer: PointCloudRenderer;
  private readonly rendererStatus: PointCloudRendererStatus;
  private scene: THREE.Scene;
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private points: THREE.Object3D | null = null;
  private pointRaycastObject: THREE.Points<THREE.BufferGeometry, THREE.PointsMaterial> | null =
    null;
  private pointGeometry: THREE.BufferGeometry | null = null;
  private webGpuPointLayer: WebGpuPointCloudLayer | null = null;
  private pointIndexStride = 1;
  private sourcePointCount = 0;
  private renderedPointCount = 0;
  private baseColors: Float32Array | null = null;
  private renderScheduler!: PointCloudRenderScheduler;
  private readonly triViewPass: PointCloudTriViewPass;
  private triViewLayout: TriViewClientLayout | null = null;
  private triViewElevated = false;
  private renderSubmitCount = 0;
  private mainPassCount = 0;
  private triPassCount = 0;
  private disposed = false;
  private container: HTMLElement;
  private renderSurface: HTMLElement;
  private getVisibleRegions?: PointCloudVisibleRegions;
  private surfaceWidth = 0;
  private surfaceHeight = 0;

  private boxLayer = new THREE.Group();
  private boxGroups = new Map<string, THREE.Group>();
  private boxSignature = "";
  private readonly unitEdges = new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1));
  private readonly unitBox = new THREE.BoxGeometry(1, 1, 1);
  private readonly raycaster = new THREE.Raycaster();

  private labelLayer = new THREE.Group();
  private boxLabels = new Map<string, THREE.Sprite>();

  private referenceLayer = new THREE.Group();
  private referenceBoxes: THREE.LineSegments[] = [];
  private referenceBoxSignature = "";

  private measurementLayer = new THREE.Group();

  private neighborLayer = new THREE.Group();
  private neighborPoints: NeighborPointLayer[] = [];

  private readonly viewCenter = new THREE.Vector3();
  private viewRadius = 10;

  private readonly forward = new THREE.Vector3(0, 1, 0);

  private groundZ = 0;

  private readonly axisScene = new THREE.Scene();
  private readonly axisCamera = new THREE.PerspectiveCamera(35, 1, 0.1, 20);
  private readonly axisGroup = new THREE.Group();
  private readonly grid: THREE.GridHelper;
  private readonly qualityGroundGrid: THREE.GridHelper;
  private axisGizmoVisible = true;
  private pointSize = 0.06;
  private decimateThreshold = DEFAULT_DECIMATE_THRESHOLD;
  private orbitMode: PointCloudViewState["mode"] = "orbit";
  private onViewChange: ((view: PointCloudViewState) => void) | null = null;

  private readonly transform: TransformControls;
  private onTransformChange: ((id: string, psr: BoxPsr, commit: boolean) => void) | null = null;
  private transformDragging = false;
  private transformChangedDuringDrag = false;
  private suppressClickAfterDrag = false;

  static async create(
    container: HTMLElement,
    options: PointCloudSceneOptions = {},
  ): Promise<PointCloudScene> {
    const surface = await createPointCloudRenderer({
      mode: options.rendererMode ?? "legacy",
      antialias: true,
      onDeviceLost: options.onDeviceLost,
    });
    return new PointCloudScene(container, options, surface);
  }

  private constructor(
    container: HTMLElement,
    options: PointCloudSceneOptions,
    surface: PointCloudRendererSurface,
  ) {
    this.container = container;
    this.renderSurface = options.renderSurface ?? container;
    this.getVisibleRegions = options.getVisibleRegions;
    if (POINT_CLOUD_TEST_PROBES_ENABLED) {
      (this.container as HTMLElement & { __pointCloudScene?: PointCloudScene }).__pointCloudScene =
        this;
    }
    this.setDecimateThreshold(options.decimateThreshold ?? DEFAULT_DECIMATE_THRESHOLD);
    const { clientWidth: w, clientHeight: h } = container;

    this.renderer = surface.renderer;
    this.rendererStatus = surface.status;
    const pixelRatio = Math.min(window.devicePixelRatio, 2);
    this.renderer.setPixelRatio(pixelRatio);
    this.surfaceWidth = this.renderSurface.clientWidth;
    this.surfaceHeight = this.renderSurface.clientHeight;
    this.renderer.setSize(this.surfaceWidth, this.surfaceHeight);
    this.renderer.setClearColor(0x0b0d12, 1);
    if ("localClippingEnabled" in this.renderer) {
      (
        this.renderer as PointCloudRenderer & {
          localClippingEnabled: boolean;
        }
      ).localClippingEnabled = true;
    }
    this.renderSurface.appendChild(this.renderer.domElement);
    this.renderer.domElement.style.position = "absolute";
    this.renderer.domElement.style.inset = "0";
    this.renderer.domElement.style.pointerEvents = "none";
    this.triViewPass = new PointCloudTriViewPass(this.rendererStatus.actualBackend, pixelRatio);
    if (POINT_CLOUD_TEST_PROBES_ENABLED) {
      this.container.dataset.pointcloudRendererCount = "1";
      this.container.dataset.pointcloudSubmitCount = "0";
      this.container.dataset.pointcloudMainPassCount = "0";
      this.container.dataset.pointcloudTriPassCount = "0";
    }

    this.scene = new THREE.Scene();

    this.camera = new THREE.PerspectiveCamera(60, w / h || 1, 0.1, 5000);
    this.camera.position.set(0, -20, 12);
    this.camera.up.set(0, 0, 1);

    this.controls = new OrbitControls(this.camera, container);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.1;
    this.setOrbitMouseMode("orbit");
    this.controls.addEventListener("change", () => {
      this.onViewChange?.(this.getViewState());
      this.invalidateMain("orbit-controls");
    });

    this.grid = new THREE.GridHelper(100, 50, 0x2a2f3a, 0x1a1d24);
    this.grid.rotation.x = Math.PI / 2;
    this.scene.add(this.grid);
    this.qualityGroundGrid = new THREE.GridHelper(20, 20, 0xf59e0b, 0xf59e0b);
    this.qualityGroundGrid.rotation.x = Math.PI / 2;
    this.qualityGroundGrid.visible = false;
    this.scene.add(this.qualityGroundGrid);

    this.scene.add(this.boxLayer);
    this.scene.add(this.labelLayer);
    this.scene.add(this.referenceLayer);
    this.scene.add(this.measurementLayer);
    this.scene.add(this.neighborLayer);
    this.initAxisGizmo();

    this.transform = new TransformControls(this.camera, container);
    this.transform.setSpace("local");
    this.transform.addEventListener("objectChange", () => {
      this.invalidateMain("transform-object");
      if (!this.transformDragging) return;
      this.transformChangedDuringDrag = true;
      this.emitTransform(false);
    });
    this.transform.addEventListener("dragging-changed", (event) => {
      const dragging = (event as unknown as { value: boolean }).value;
      this.transformDragging = dragging;
      this.controls.enabled = !dragging;
      if (dragging) {
        this.transformChangedDuringDrag = false;
      } else {
        this.suppressClickAfterDrag = true;
        if (this.transformChangedDuringDrag) this.emitTransform(true);
      }
      this.invalidateMain("transform-dragging");
    });
    this.scene.add(this.transform.getHelper());

    this.renderScheduler = new PointCloudRenderScheduler(this.renderFrame);
    this.invalidateAll("scene-init");
  }

  getRendererStatus(): PointCloudRendererStatus {
    return this.rendererStatus;
  }

  setDecimateThreshold(value: number): void {
    if (!Number.isFinite(value) || value <= 0) {
      this.decimateThreshold = DEFAULT_DECIMATE_THRESHOLD;
      return;
    }
    this.decimateThreshold = Math.max(1, Math.round(value));
  }

  private markInvalidation(reason: string): void {
    if (!POINT_CLOUD_TEST_PROBES_ENABLED) return;
    this.container.dataset.pointcloudLastInvalidateReason = reason;
    this.container.dataset.pointcloudLastInvalidateAt = String(performance.now());
  }

  private invalidateMain(reason = "main-state"): void {
    if (this.disposed || !this.renderScheduler) return;
    this.markInvalidation(reason);
    this.renderScheduler.invalidate(POINT_CLOUD_RENDER_MAIN);
  }

  private invalidateTri(reason = "tri-state"): void {
    if (this.disposed || !this.renderScheduler) return;
    this.markInvalidation(reason);
    this.renderScheduler.invalidate(POINT_CLOUD_RENDER_TRI);
  }

  private invalidateAll(reason: string): void {
    if (this.disposed || !this.renderScheduler) return;
    this.markInvalidation(reason);
    this.renderScheduler.invalidate(POINT_CLOUD_RENDER_ALL);
  }

  private renderFrame = (dirtyMask: number): boolean => {
    if (this.disposed) return false;
    const controlsChanged = this.controls.update();
    const { renderMain, renderTri } = resolvePointCloudRenderPlan(dirtyMask, controlsChanged);
    const canvasRect = this.renderer.domElement.getBoundingClientRect();
    const mainRect = this.container.getBoundingClientRect();
    let mainRegions = this.getVisibleRegions?.(this.container) ?? [mainRect];
    for (const region of this.triViewPass.getOccludingRegions()) {
      mainRegions = mainRegions.flatMap((piece) => subtractClientRect(piece, region));
    }
    let mainPasses = 0;
    let triPasses = 0;
    const previousAutoClear = this.renderer.autoClear;
    this.renderer.autoClear = false;
    try {
      this.renderer.setScissorTest(false);
      this.renderer.setViewport(0, 0, canvasRect.width, canvasRect.height);
      this.renderer.clear(true, true, true);
      if (renderMain) {
        this.updateTransformSize();
        const scissors = clientRectToRendererScissors(
          mainRect,
          canvasRect,
          this.rendererStatus.actualBackend,
          mainRegions,
        );
        if (scissors.length > 0) {
          this.camera.aspect = mainRect.width / mainRect.height;
          this.camera.updateProjectionMatrix();
          const pointSize = (this.pointSize * mainRect.height) / canvasRect.height;
          if (this.webGpuPointLayer) this.webGpuPointLayer.setPointSize(pointSize);
          else if (this.points) {
            ((this.points as THREE.Points).material as THREE.PointsMaterial).size = pointSize;
          }
          for (const neighbor of this.neighborPoints) {
            if (neighbor.material) neighbor.material.size = pointSize * 0.8;
            neighbor.webGpuLayer?.setPointSize(pointSize * 0.8);
          }
          const viewport = setClientCameraViewport(
            this.camera,
            mainRect,
            canvasRect,
            this.rendererStatus.actualBackend,
          );
          if (viewport) {
            this.renderer.setScissorTest(true);
            this.renderer.setViewport(viewport.x, viewport.y, viewport.width, viewport.height);
            for (const scissor of scissors) {
              this.renderer.setScissor(scissor.x, scissor.y, scissor.width, scissor.height);
              this.renderer.render(this.scene, this.camera);
              mainPasses += 1;
            }
            this.camera.clearViewOffset();
            this.renderAxisGizmo(mainRect, canvasRect, mainRegions);
          }
        }
      }
      if (renderTri) triPasses = this.triViewPass.render(this.renderer, canvasRect);
    } finally {
      this.camera.clearViewOffset();
      this.renderer.setScissorTest(false);
      this.renderer.setViewport(0, 0, canvasRect.width, canvasRect.height);
      this.renderer.autoClear = previousAutoClear;
    }
    if (POINT_CLOUD_TEST_PROBES_ENABLED && mainPasses + triPasses > 0) {
      this.renderSubmitCount += mainPasses + triPasses;
      this.mainPassCount += mainPasses;
      this.triPassCount += triPasses;
      this.container.dataset.pointcloudSubmitCount = String(this.renderSubmitCount);
      this.container.dataset.pointcloudMainPassCount = String(this.mainPassCount);
      this.container.dataset.pointcloudTriPassCount = String(this.triPassCount);
      this.container.dataset.pointcloudLastSubmitAt = String(performance.now());
      if (triPasses > 0) {
        this.container.dataset.pointcloudTriActiveRenderAt = String(performance.now());
      }
    }
    return controlsChanged;
  };

  private initAxisGizmo() {
    this.axisScene.add(this.axisGroup);
    const axes: Array<{
      label: "X" | "Y" | "Z";
      dir: THREE.Vector3;
      color: number;
    }> = [
      { label: "X", dir: new THREE.Vector3(1, 0, 0), color: 0xff5c68 },
      { label: "Y", dir: new THREE.Vector3(0, 1, 0), color: 0x39e98a },
      { label: "Z", dir: new THREE.Vector3(0, 0, 1), color: 0x44a6ff },
    ];
    for (const axis of axes) {
      const arrow = new THREE.ArrowHelper(
        axis.dir,
        new THREE.Vector3(0, 0, 0),
        1.18,
        axis.color,
        0.22,
        0.1,
      );
      this.axisGroup.add(arrow);
      const label = this.createAxisLabel(axis.label, axis.color);
      label.position.copy(axis.dir).multiplyScalar(1.48);
      this.axisGroup.add(label);
    }
    const ringPoints = Array.from({ length: 49 }, (_, i) => {
      const t = ((i % 48) / 48) * Math.PI * 2;
      return new THREE.Vector3(Math.cos(t) * 1.42, Math.sin(t) * 1.42, 0);
    });
    const ring = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(ringPoints),
      new THREE.LineBasicMaterial({
        color: 0x44a6ff,
        transparent: true,
        opacity: 0.22,
        depthTest: false,
      }),
    );
    this.axisGroup.add(ring);
  }

  private createAxisLabel(label: string, color: number) {
    const canvas = document.createElement("canvas");
    canvas.width = 128;
    canvas.height = 128;
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.font = "700 72px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.shadowColor = "rgba(0, 0, 0, 0.75)";
      ctx.shadowBlur = 12;
      ctx.fillStyle = `#${new THREE.Color(color).getHexString()}`;
      ctx.fillText(label, canvas.width / 2, canvas.height / 2);
    }
    const texture = new THREE.CanvasTexture(canvas);
    texture.needsUpdate = true;
    const material = new THREE.SpriteMaterial({
      map: texture,
      transparent: true,
      depthTest: false,
      depthWrite: false,
    });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(0.82, 0.82, 0.82);
    return sprite;
  }

  private renderAxisGizmo(
    mainRect: ClientRectSnapshot,
    canvasRect: ClientRectSnapshot,
    mainRegions: readonly ClientRectSnapshot[],
  ) {
    if (!this.axisGizmoVisible) return;
    const { width: w, height: h } = mainRect;
    if (!w || !h) return;
    const size = Math.min(128, Math.max(96, Math.floor(Math.min(w, h) * 0.18)));
    const margin = 14;
    const bottomMargin = 54;
    const offset = new THREE.Vector3().copy(this.camera.position).sub(this.controls.target);
    if (offset.lengthSq() < 1e-6) offset.set(2, -3, 2);
    this.axisCamera.position.copy(offset.normalize().multiplyScalar(5));
    this.axisCamera.up.copy(this.camera.up);
    this.axisCamera.lookAt(0, 0, 0);
    this.axisCamera.updateProjectionMatrix();

    const r = this.renderer;
    const rect = {
      left: mainRect.left + margin,
      top: mainRect.top + h - bottomMargin - size,
      width: size,
      height: size,
    };
    const scissors = clientRectToRendererScissors(
      rect,
      canvasRect,
      this.rendererStatus.actualBackend,
      mainRegions,
    );
    if (scissors.length === 0) return;
    const viewport = setClientCameraViewport(
      this.axisCamera,
      rect,
      canvasRect,
      this.rendererStatus.actualBackend,
    );
    if (!viewport) return;
    const prevAutoClear = r.autoClear;
    r.autoClear = false;
    r.clearDepth();
    try {
      r.setScissorTest(true);
      r.setViewport(viewport.x, viewport.y, viewport.width, viewport.height);
      for (const scissor of scissors) {
        r.setScissor(scissor.x, scissor.y, scissor.width, scissor.height);
        r.render(this.axisScene, this.axisCamera);
      }
    } finally {
      r.setScissorTest(false);
      this.axisCamera.clearViewOffset();
      r.autoClear = prevAutoClear;
    }
  }

  private updateTransformSize() {
    const obj = this.transform.object;
    if (!obj) return;
    const maxDim = Math.max(
      Math.abs(obj.scale.x),
      Math.abs(obj.scale.y),
      Math.abs(obj.scale.z),
      0.5,
    );
    const dist = this.camera.position.distanceTo(obj.position);
    const size = THREE.MathUtils.clamp(
      (maxDim / Math.max(dist, 0.001)) * 4.8,
      TRANSFORM_SIZE_MIN,
      TRANSFORM_SIZE_MAX,
    );
    this.transform.setSize(size);
  }

  async loadPcd(
    url: string,
    convention: LidarAxisConvention = "iso_8855",
    options: PointCloudLoadOptions = {},
  ): Promise<PointCloudStats> {
    const frame = await loadTimedDecodedPointCloudFrame(url, convention, this.decimateThreshold);
    const positions = frame.positions;
    const total = frame.totalPoints;
    const rendered = frame.renderedPoints;
    const stride = frame.decimateStride;
    const decimated = stride > 1;

    const stats = {
      totalPoints: total,
      renderedPoints: rendered,
      decimated,
      decimateStride: stride,
    };
    if (options.shouldCommit && !options.shouldCommit()) return stats;

    let material: THREE.PointsMaterial | null = null;
    let webGpuLayer: WebGpuPointCloudLayer | null = null;
    let pointRaycastObject: THREE.Points<THREE.BufferGeometry, THREE.PointsMaterial> | null = null;
    let pointObject: THREE.Object3D;
    let geom: THREE.BufferGeometry;
    let reusedWebGpuLayer = false;
    if (this.rendererStatus.actualBackend === "legacy-webgl2") {
      geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geom.setAttribute("color", new THREE.BufferAttribute(frame.heightColors.slice(), 3));
      geom.computeBoundingBox();
      material = new THREE.PointsMaterial({
        size: this.pointSize,
        vertexColors: true,
        sizeAttenuation: true,
      });
      pointObject = new THREE.Points(geom, material);
    } else if (
      this.webGpuPointLayer &&
      this.pointGeometry &&
      this.points &&
      this.pointRaycastObject &&
      this.webGpuPointLayer.updatePointData(positions, frame.heightColors)
    ) {
      geom = this.pointGeometry;
      webGpuLayer = this.webGpuPointLayer;
      pointObject = this.points;
      pointRaycastObject = this.pointRaycastObject;
      reusedWebGpuLayer = true;
    } else {
      const capacity = webGpuPointCapacity(rendered);
      const positionBuffer = new Float32Array(capacity * 3);
      const colorBuffer = new Float32Array(capacity * 3);
      positionBuffer.set(positions);
      colorBuffer.set(frame.heightColors);
      geom = new THREE.BufferGeometry();
      const positionAttribute = new THREE.BufferAttribute(positionBuffer, 3);
      const colorAttribute = new THREE.BufferAttribute(colorBuffer, 3);
      positionAttribute.setUsage(THREE.DynamicDrawUsage);
      colorAttribute.setUsage(THREE.DynamicDrawUsage);
      geom.setAttribute("position", positionAttribute);
      geom.setAttribute("color", colorAttribute);
      geom.setDrawRange(0, rendered);
      webGpuLayer = createWebGpuPointCloudLayer(geom, {
        pointSize: this.pointSize,
        sizeAttenuation: true,
        selection: true,
      });
      pointObject = webGpuLayer.object;
      pointRaycastObject = new THREE.Points(
        geom,
        new THREE.PointsMaterial({ size: this.pointSize, vertexColors: false }),
      );
    }

    if (!reusedWebGpuLayer) this.removePoints();
    this.pointIndexStride = stride;
    this.sourcePointCount = total;
    this.renderedPointCount = rendered;
    this.baseColors = frame.heightColors;
    this.pointGeometry = geom;
    this.webGpuPointLayer = webGpuLayer;
    this.pointRaycastObject = pointRaycastObject;
    this.points = pointObject;
    this.points.visible = options.visible ?? true;
    this.triViewPass.setVisible(options.visible ?? true);
    if (!reusedWebGpuLayer) this.scene.add(this.points);
    this.triViewPass.setGeometry(geom);
    this.prewarmTriView();
    this.invalidateTri();
    this.viewCenter.fromArray(frame.viewCenter);
    this.viewRadius = frame.viewRadius;
    this.groundZ = frame.groundZ;
    this.frameView();
    return stats;
  }

  private frameView() {
    const c = this.viewCenter;
    const r = this.viewRadius;
    const f = this.forward;
    this.controls.target.copy(c);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(c.x - f.x * r * 2.2, c.y - f.y * r * 2.2, c.z + r * 1.2);
    this.camera.near = Math.max(r / 100, 0.1);
    this.camera.far = r * 50;
    this.camera.updateProjectionMatrix();
    this.setOrbitMouseMode("orbit");
    this.controls.update();
  }

  setViewForward(x: number, y: number) {
    if (Math.hypot(x, y) < 1e-3) return;
    this.forward.set(x, y, 0).normalize();
  }

  resetView() {
    this.frameView();
  }

  bevView() {
    const c = this.viewCenter;
    const r = this.viewRadius;
    this.controls.target.copy(c);
    this.camera.up.copy(this.forward);
    this.camera.position.set(c.x, c.y, c.z + r * 2.5);
    this.camera.near = Math.max(r / 100, 0.1);
    this.camera.far = r * 50;
    this.camera.updateProjectionMatrix();
    this.setOrbitMouseMode("bev");
    this.controls.update();
  }

  private setOrbitMouseMode(mode: "orbit" | "bev") {
    this.orbitMode = mode;
    if (mode === "bev") {
      this.controls.mouseButtons = {
        LEFT: THREE.MOUSE.PAN,
        MIDDLE: THREE.MOUSE.DOLLY,
        RIGHT: THREE.MOUSE.PAN,
      };
      this.controls.enableRotate = false;
      this.controls.screenSpacePanning = true;
      return;
    }
    this.controls.enableRotate = true;
    this.controls.mouseButtons = {
      LEFT: THREE.MOUSE.ROTATE,
      MIDDLE: THREE.MOUSE.DOLLY,
      RIGHT: THREE.MOUSE.PAN,
    };
    this.controls.screenSpacePanning = true;
  }

  private collectPointsInScreenRect(
    x0: number,
    y0: number,
    x1: number,
    y1: number,
  ): { positions: Float32Array; indices: number[] } | null {
    const rect = this.container.getBoundingClientRect();
    const toNdcX = (cx: number) => ((cx - rect.left) / rect.width) * 2 - 1;
    const toNdcY = (cy: number) => -((cy - rect.top) / rect.height) * 2 + 1;
    const nx0 = Math.min(toNdcX(x0), toNdcX(x1));
    const nx1 = Math.max(toNdcX(x0), toNdcX(x1));
    const ny0 = Math.min(toNdcY(y0), toNdcY(y1));
    const ny1 = Math.max(toNdcY(y0), toNdcY(y1));
    return this.collectProjectedPoints(
      (ndcX, ndcY) => ndcX >= nx0 && ndcX <= nx1 && ndcY >= ny0 && ndcY <= ny1,
    );
  }

  private collectPointsInScreenPolygon(
    polygon: readonly ScreenPoint[],
  ): { positions: Float32Array; indices: number[] } | null {
    const positions = this.getPointPositions();
    if (!positions) return null;
    if (polygon.length < 3) return null;
    const rect = this.container.getBoundingClientRect();
    const toNdcX = (cx: number) => ((cx - rect.left) / rect.width) * 2 - 1;
    const toNdcY = (cy: number) => -((cy - rect.top) / rect.height) * 2 + 1;
    const ndcPolygon = polygon.map((p) => ({ x: toNdcX(p.x), y: toNdcY(p.y) }));
    return this.collectProjectedPoints((ndcX, ndcY) =>
      isPointInPolygon({ x: ndcX, y: ndcY }, ndcPolygon),
    );
  }

  private collectProjectedPoints(
    containsNdc: (ndcX: number, ndcY: number) => boolean,
  ): { positions: Float32Array; indices: number[] } | null {
    const positions = this.getPointPositions();
    if (!positions) return null;
    this.camera.updateMatrixWorld();
    const vp = new THREE.Matrix4().multiplyMatrices(
      this.camera.projectionMatrix,
      this.camera.matrixWorldInverse,
    );
    const v = new THREE.Vector4();
    const out: number[] = [];
    const indices: number[] = [];
    const n = Math.floor(positions.length / 3);
    for (let i = 0; i < n; i++) {
      const px = positions[i * 3];
      const py = positions[i * 3 + 1];
      const pz = positions[i * 3 + 2];
      v.set(px, py, pz, 1).applyMatrix4(vp);
      if (v.w <= 0) continue;
      const ndcX = v.x / v.w;
      const ndcY = v.y / v.w;
      if (containsNdc(ndcX, ndcY)) {
        out.push(px, py, pz);
        indices.push(i * this.pointIndexStride);
      }
    }
    return out.length > 0 ? { positions: new Float32Array(out), indices } : null;
  }

  selectPointsInScreenRect(x0: number, y0: number, x1: number, y1: number): Float32Array | null {
    return this.collectPointsInScreenRect(x0, y0, x1, y1)?.positions ?? null;
  }

  selectPointMaskInScreenRect(
    x0: number,
    y0: number,
    x1: number,
    y1: number,
  ): PointMaskSelection | null {
    const selected = this.collectPointsInScreenRect(x0, y0, x1, y1);
    if (!selected) return null;
    return {
      pointIndices: selected.indices,
      decimateStride: this.pointIndexStride,
      sourcePointCount: this.sourcePointCount,
    };
  }

  selectPointMaskInScreenPolygon(polygon: readonly ScreenPoint[]): PointMaskSelection | null {
    const selected = this.collectPointsInScreenPolygon(polygon);
    if (!selected) return null;
    return {
      pointIndices: selected.indices,
      decimateStride: this.pointIndexStride,
      sourcePointCount: this.sourcePointCount,
    };
  }

  setBoxSelecting(active: boolean) {
    this.controls.enabled = !active;
  }

  getPointsGeometry(): THREE.BufferGeometry | null {
    return this.pointGeometry;
  }

  setTriViewLayout(layout: TriViewClientLayout | null): void {
    if (!this.triViewPass.setLayout(layout)) return;
    this.triViewLayout = layout;
    this.syncTriViewCanvasLayer();
    this.invalidateAll("tri-layout");
  }

  setTriViewElevated(elevated: boolean): void {
    if (this.disposed) return;
    if (elevated === this.triViewElevated) return;
    this.triViewElevated = elevated;
    this.syncTriViewCanvasLayer();
    this.invalidateAll("tri-layer");
  }

  private syncTriViewCanvasLayer(): void {
    const canvas = this.renderer.domElement;
    if (this.renderSurface !== this.container || !this.triViewElevated || !this.triViewLayout) {
      canvas.style.removeProperty("z-index");
      canvas.style.removeProperty("clip-path");
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const clipPath = clientRectToCanvasClipPath(this.triViewLayout.panel, {
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
    });
    if (!clipPath) {
      canvas.style.removeProperty("z-index");
      canvas.style.removeProperty("clip-path");
      return;
    }
    canvas.style.zIndex = "var(--sc-z-local-overlay)";
    canvas.style.clipPath = clipPath;
  }

  setTriViewBox(box: Psr | null): void {
    if (!this.triViewPass.setBox(box)) return;
    this.prewarmTriView();
    this.invalidateTri();
  }

  private prewarmTriView(): void {
    const completion = this.triViewPass.prewarm(this.renderer);
    if (!completion) return;
    void completion.then(() => {
      if (!this.disposed) this.invalidateTri();
    });
  }

  setTriViewCameraRef(cameraRef: Psr | null): void {
    if (this.triViewPass.setCameraRef(cameraRef)) this.invalidateTri();
  }

  setTriViewZoomByView(zoomByView: Record<TriView, number>): void {
    if (this.triViewPass.setZoomByView(zoomByView)) this.invalidateTri();
  }

  setTriViewActive(active: boolean): void {
    if (!this.triViewPass.setActive(active)) return;
    this.invalidateAll("tri-active");
  }

  setPointSize(size: number) {
    if (size === this.pointSize) return;
    this.pointSize = size;
    this.triViewPass.setPointSize(size);
    if (this.webGpuPointLayer) {
      this.webGpuPointLayer.setPointSize(size);
    } else if (this.points) {
      ((this.points as THREE.Points).material as THREE.PointsMaterial).size = size;
    }
    if (this.pointRaycastObject) {
      (this.pointRaycastObject.material as THREE.PointsMaterial).size = size;
    }
    for (const neighbor of this.neighborPoints) {
      if (neighbor.material) neighbor.material.size = size * 0.8;
      neighbor.webGpuLayer?.setPointSize(size * 0.8);
    }
    this.invalidateMain();
    this.invalidateTri();
  }

  setGridVisible(visible: boolean) {
    if (visible === this.grid.visible) return;
    this.grid.visible = visible;
    this.invalidateMain();
  }

  setQualityGroundPlane(z: number | null): void {
    const visible = z != null && Number.isFinite(z);
    this.qualityGroundGrid.visible = visible;
    if (visible) this.qualityGroundGrid.position.z = z!;
    this.invalidateMain("quality-ground-plane");
  }

  setMeasurementPaths(paths: readonly SceneMeasurementPath[]): void {
    this.clearMeasurementLayer();
    for (const path of paths) {
      if (path.positions.length === 0) continue;
      const color = path.active ? 0xf59e0b : 0x38bdf8;
      const points = path.positions.map((position) => new THREE.Vector3(...position));

      const markerGeometry = new THREE.BufferGeometry().setFromPoints(points);
      const markerMaterial = new THREE.PointsMaterial({
        color,
        depthTest: false,
        depthWrite: false,
        size: path.active ? 0.2 : 0.16,
        sizeAttenuation: true,
      });
      const markers = new THREE.Points(markerGeometry, markerMaterial);
      markers.name = `measurement-markers:${path.id}`;
      markers.renderOrder = 40;
      this.measurementLayer.add(markers);

      if (points.length < 2) continue;
      const lineGeometry = new THREE.BufferGeometry().setFromPoints(points);
      const lineMaterial = new THREE.LineBasicMaterial({
        color,
        depthTest: false,
        depthWrite: false,
        transparent: true,
        opacity: path.active ? 1 : 0.9,
      });
      const line = new THREE.Line(lineGeometry, lineMaterial);
      line.name = `measurement-line:${path.id}`;
      line.renderOrder = 39;
      this.measurementLayer.add(line);
    }
    this.invalidateMain("measurement-paths");
  }

  private clearMeasurementLayer(): void {
    for (const child of [...this.measurementLayer.children]) {
      this.measurementLayer.remove(child);
      const renderable = child as THREE.Object3D & {
        geometry?: THREE.BufferGeometry;
        material?: THREE.Material | THREE.Material[];
      };
      renderable.geometry?.dispose();
      const materials = Array.isArray(renderable.material)
        ? renderable.material
        : renderable.material
          ? [renderable.material]
          : [];
      materials.forEach((material) => material.dispose());
    }
  }

  setAxisGizmoVisible(visible: boolean) {
    if (visible === this.axisGizmoVisible) return;
    this.axisGizmoVisible = visible;
    this.axisGroup.visible = visible;
    this.invalidateMain();
  }

  setCameraDamping(dampingFactor: number) {
    this.controls.dampingFactor = dampingFactor;
  }

  setViewChangeHandler(handler: ((view: PointCloudViewState) => void) | null) {
    this.onViewChange = handler;
  }

  getViewState(): PointCloudViewState {
    return {
      position: this.camera.position.toArray() as [number, number, number],
      target: this.controls.target.toArray() as [number, number, number],
      up: this.camera.up.toArray() as [number, number, number],
      mode: this.orbitMode,
    };
  }

  applyViewState(view: PointCloudViewState | null | undefined): boolean {
    if (!view || (view.mode !== "orbit" && view.mode !== "bev")) return false;
    const values = [...view.position, ...view.target, ...view.up];
    if (values.length !== 9 || values.some((v) => !Number.isFinite(v))) return false;
    const viewChangeHandler = this.onViewChange;
    const dampingEnabled = this.controls.enableDamping;
    this.onViewChange = null;
    this.controls.enableDamping = false;
    this.controls.update();
    this.camera.position.fromArray(view.position);
    this.controls.target.fromArray(view.target);
    this.camera.up.fromArray(view.up);
    this.setOrbitMouseMode(view.mode);
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.controls.enableDamping = dampingEnabled;
    this.onViewChange = viewChangeHandler;
    this.onViewChange?.(this.getViewState());
    return true;
  }

  getPointPositions(): Float32Array | null {
    const attr = this.pointGeometry?.getAttribute("position") as THREE.BufferAttribute | undefined;
    if (!attr || this.renderedPointCount === 0) return null;
    const positions = attr.array as Float32Array;
    return positions.subarray(0, this.renderedPointCount * 3);
  }

  getBaseColors(): Float32Array | null {
    return this.baseColors;
  }

  clearPointCloud() {
    this.qualityGroundGrid.visible = false;
    if (this.webGpuPointLayer && this.points) {
      this.points.visible = false;
      this.webGpuPointLayer.setPointCount(0);
      this.renderedPointCount = 0;
    } else {
      this.removePoints();
    }
    this.triViewPass.setGeometry(this.pointGeometry);
    this.baseColors = null;
    this.groundZ = 0;
    this.invalidateMain();
    this.invalidateTri();
  }

  retirePointCloud() {
    this.qualityGroundGrid.visible = false;
    this.setCameraTextureColorization(null);
    this.removePoints();
    this.triViewPass.setGeometry(null);
    this.baseColors = null;
    this.groundZ = 0;
    this.invalidateMain();
    this.invalidateTri();
  }

  setPointCloudVisible(visible: boolean) {
    if (this.points?.visible === visible) return;
    if (this.points) this.points.visible = visible;
    this.triViewPass.setVisible(visible);
    this.invalidateMain();
    this.invalidateTri();
  }

  setPointColors(colors: Float32Array | null) {
    const geom = this.pointGeometry;
    if (!geom) return;
    const target = colors ?? this.baseColors;
    if (!target) return;
    const attr = geom.getAttribute("color") as THREE.BufferAttribute;
    (attr.array as Float32Array).set(target);
    attr.needsUpdate = true;
    this.invalidateMain();
    this.invalidateTri();
  }

  setCameraTextureColorization(samples: readonly GpuCameraTextureSample[] | null) {
    this.webGpuPointLayer?.setCameraColorization(samples);
    this.triViewPass.setCameraTextureColorization(samples);
    this.invalidateMain("camera-texture");
    this.invalidateTri("camera-texture");
  }

  setCameraTextureColorAdjust(adjust: ColorAdjust) {
    this.webGpuPointLayer?.setColorAdjust(adjust);
    this.triViewPass.setCameraTextureColorAdjust(adjust);
    this.invalidateMain("camera-color-adjust");
    this.invalidateTri("camera-color-adjust");
  }

  highlightPointMask(indices: readonly number[] | null) {
    if (this.webGpuPointLayer) {
      this.webGpuPointLayer.setSelection(indices, this.pointIndexStride);
      this.invalidateMain();
      this.invalidateTri();
      return;
    }
    const geom = this.pointGeometry;
    if (!geom) return;
    const base = this.baseColors;
    if (!base) return;
    const attr = geom.getAttribute("color") as THREE.BufferAttribute;
    const colors = attr.array as Float32Array;
    colors.set(base);
    if (indices && indices.length > 0) {
      const selected = new Set(indices);
      const count = Math.floor(colors.length / 3);
      for (let i = 0; i < count; i += 1) {
        if (!selected.has(i * this.pointIndexStride)) continue;
        colors[i * 3] = 1;
        colors[i * 3 + 1] = 0.12;
        colors[i * 3 + 2] = 0.12;
      }
    }
    attr.needsUpdate = true;
    this.invalidateMain();
    this.invalidateTri();
  }

  setRenderSurface(surface: HTMLElement | null): void {
    const next = surface ?? this.container;
    if (this.renderSurface === next) return;
    this.renderSurface = next;
    next.appendChild(this.renderer.domElement);
    this.resize();
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (w > 0 && h > 0 && this.camera.aspect !== w / h) {
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    }
    const width = this.renderSurface.clientWidth;
    const height = this.renderSurface.clientHeight;
    if (width > 0 && height > 0 && (width !== this.surfaceWidth || height !== this.surfaceHeight)) {
      this.surfaceWidth = width;
      this.surfaceHeight = height;
      this.renderer.setSize(width, height);
    }
    this.syncTriViewCanvasLayer();
    this.invalidateAll("resize");
  }

  private removePoints() {
    if (!this.points) return;
    this.scene.remove(this.points);
    this.triViewPass.setGeometry(null);
    const hadWebGpuLayer = this.webGpuPointLayer !== null;
    this.webGpuPointLayer?.dispose();
    this.webGpuPointLayer = null;
    this.pointRaycastObject?.material.dispose();
    this.pointRaycastObject = null;
    this.pointGeometry?.dispose();
    this.pointGeometry = null;
    if (!hadWebGpuLayer && (this.points as THREE.Points).material) {
      ((this.points as THREE.Points).material as THREE.Material).dispose();
    }
    this.points = null;
    this.pointIndexStride = 1;
    this.sourcePointCount = 0;
    this.renderedPointCount = 0;
    this.invalidateMain();
  }

  setBoxes(boxes: SceneBox[]) {
    const signature = JSON.stringify(
      boxes.map(({ id, center, size, rotation, color, selected, label }) => [
        id,
        center,
        size,
        rotation,
        color,
        selected,
        label ?? null,
      ]),
    );
    if (signature === this.boxSignature) return;
    this.boxSignature = signature;
    const next = new Set(boxes.map((b) => b.id));
    for (const [id, group] of this.boxGroups) {
      if (!next.has(id)) {
        if (this.transform.object === group) this.transform.detach();
        this.boxLayer.remove(group);
        this.disposeBoxGroup(group);
        this.boxGroups.delete(id);
      }
    }
    for (const b of boxes) {
      let group = this.boxGroups.get(b.id);
      if (!group) {
        group = this.createBoxGroup(b.id);
        this.boxGroups.set(b.id, group);
        this.boxLayer.add(group);
      }
      this.updateBoxGroup(group, b);
    }
    this.syncBoxLabels(boxes, next);
    this.invalidateMain("boxes");
  }

  private syncBoxLabels(boxes: SceneBox[], next: Set<string>) {
    for (const [id, sprite] of this.boxLabels) {
      const stillHasLabel = next.has(id) && boxes.some((b) => b.id === id && b.label);
      if (!stillHasLabel) {
        this.labelLayer.remove(sprite);
        this.disposeLabelSprite(sprite);
        this.boxLabels.delete(id);
      }
    }
    for (const b of boxes) {
      if (!b.label) continue;
      let sprite = this.boxLabels.get(b.id);
      if (!sprite) {
        sprite = this.createLabelSprite(b.label);
        this.boxLabels.set(b.id, sprite);
        this.labelLayer.add(sprite);
      } else if (sprite.userData.text !== b.label) {
        this.drawLabelTexture(sprite, b.label);
      }
      const euler = new THREE.Euler(b.rotation[0], b.rotation[1], b.rotation[2], "XYZ");
      const top = new THREE.Vector3(0, 0, b.size[2] / 2 + 0.3).applyEuler(euler);
      sprite.position.set(b.center[0] + top.x, b.center[1] + top.y, b.center[2] + top.z);
    }
  }

  private createLabelSprite(text: string): THREE.Sprite {
    const sprite = new THREE.Sprite(
      new THREE.SpriteMaterial({ transparent: true, depthTest: false, depthWrite: false }),
    );
    sprite.renderOrder = 4;
    this.drawLabelTexture(sprite, text);
    return sprite;
  }

  private drawLabelTexture(sprite: THREE.Sprite, text: string) {
    const fontPx = 44;
    const padX = 18;
    const padY = 12;
    const measure = document.createElement("canvas").getContext("2d");
    if (measure) measure.font = `600 ${fontPx}px sans-serif`;
    const textW = measure ? measure.measureText(text).width : text.length * fontPx * 0.6;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.ceil(textW + padX * 2));
    canvas.height = Math.ceil(fontPx + padY * 2);
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.font = `600 ${fontPx}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, canvas.width / 2, canvas.height / 2);
    }
    const texture = new THREE.CanvasTexture(canvas);
    texture.needsUpdate = true;
    const mat = sprite.material as THREE.SpriteMaterial;
    mat.map?.dispose();
    mat.map = texture;
    mat.needsUpdate = true;
    const worldH = 0.55;
    sprite.scale.set(worldH * (canvas.width / canvas.height), worldH, 1);
    sprite.userData.text = text;
  }

  private disposeLabelSprite(sprite: THREE.Sprite) {
    const mat = sprite.material as THREE.SpriteMaterial;
    mat.map?.dispose();
    mat.dispose();
  }

  setReferenceBoxes(boxes: ReferenceBox[]) {
    const signature = JSON.stringify(
      boxes.map(({ id, center, size, rotation, color, dim }) => [
        id,
        center,
        size,
        rotation,
        color,
        dim ?? false,
      ]),
    );
    if (signature === this.referenceBoxSignature) return;
    this.referenceBoxSignature = signature;
    for (const seg of this.referenceBoxes) {
      this.referenceLayer.remove(seg);
      (seg.material as THREE.Material).dispose();
    }
    this.referenceBoxes = [];
    for (const b of boxes) {
      const mat = new THREE.LineDashedMaterial({
        color: new THREE.Color(b.color).multiplyScalar(0.5),
        transparent: true,
        opacity: b.dim ? 0.22 : 0.5,
        depthTest: false,
        dashSize: 0.3,
        gapSize: 0.15,
      });
      const seg = new THREE.LineSegments(this.unitEdges, mat);
      seg.computeLineDistances();
      seg.renderOrder = 1;
      seg.position.set(b.center[0], b.center[1], b.center[2]);
      seg.quaternion.setFromEuler(
        new THREE.Euler(b.rotation[0], b.rotation[1], b.rotation[2], "XYZ"),
      );
      seg.scale.set(b.size[0], b.size[1], b.size[2]);
      this.referenceLayer.add(seg);
      this.referenceBoxes.push(seg);
    }
    this.invalidateMain("reference-boxes");
  }

  setNeighborPoints(
    frames: {
      positions: Float32Array;
      matrix: THREE.Matrix4;
      dir: "past" | "future";
      distance: number;
    }[],
  ) {
    if (frames.length === 0 && this.neighborPoints.length === 0) return;
    for (const pointLayer of this.neighborPoints) this.disposeNeighborPointLayer(pointLayer);
    this.neighborPoints = [];
    for (const f of frames) {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.BufferAttribute(f.positions, 3));
      const color = f.dir === "future" ? NEIGHBOR_FUTURE_COLOR : NEIGHBOR_PAST_COLOR;
      let material: THREE.PointsMaterial | null = null;
      let webGpuLayer: WebGpuPointCloudLayer | null = null;
      let object: THREE.Object3D;
      if (this.rendererStatus.actualBackend === "legacy-webgl2") {
        material = new THREE.PointsMaterial({
          size: this.pointSize * 0.8,
          color,
          transparent: true,
          opacity: neighborOpacity(f.distance),
          sizeAttenuation: true,
          depthWrite: false,
        });
        object = new THREE.Points(geom, material);
      } else {
        webGpuLayer = createWebGpuPointCloudLayer(geom, {
          pointSize: this.pointSize * 0.8,
          sizeAttenuation: true,
          color,
          opacity: neighborOpacity(f.distance),
          depthWrite: false,
        });
        object = webGpuLayer.object;
      }
      object.matrixAutoUpdate = false;
      object.matrix.copy(f.matrix);
      object.renderOrder = 0;
      this.neighborLayer.add(object);
      this.neighborPoints.push({ object, geometry: geom, material, webGpuLayer });
    }
    this.invalidateMain("neighbor-points");
  }

  private disposeNeighborPointLayer(pointLayer: NeighborPointLayer) {
    this.neighborLayer.remove(pointLayer.object);
    pointLayer.geometry.dispose();
    pointLayer.material?.dispose();
    pointLayer.webGpuLayer?.dispose();
  }

  private createBoxGroup(id: string): THREE.Group {
    const group = new THREE.Group();
    group.userData.boxId = id;
    const fill = new THREE.Mesh(
      this.unitBox,
      new THREE.MeshBasicMaterial({
        transparent: true,
        opacity: 0.06,
        depthWrite: false,
        depthTest: false,
      }),
    );
    fill.renderOrder = 2;
    fill.userData.boxId = id;
    const edges = new THREE.LineSegments(
      this.unitEdges,
      new THREE.LineBasicMaterial({ transparent: true, depthTest: false }),
    );
    edges.renderOrder = 3;
    group.add(fill);
    group.add(edges);
    return group;
  }

  private updateBoxGroup(group: THREE.Group, b: SceneBox) {
    group.position.set(b.center[0], b.center[1], b.center[2]);
    group.quaternion.setFromEuler(
      new THREE.Euler(b.rotation[0], b.rotation[1], b.rotation[2], "XYZ"),
    );
    group.scale.set(b.size[0], b.size[1], b.size[2]);
    const fill = group.children[0] as THREE.Mesh;
    const edges = group.children[1] as THREE.LineSegments;
    const fillMat = fill.material as THREE.MeshBasicMaterial;
    const edgeMat = edges.material as THREE.LineBasicMaterial;
    fillMat.color.set(b.color);
    fillMat.opacity = b.selected ? 0.2 : 0.06;
    edgeMat.color.set(b.selected ? SELECTED_EDGE_COLOR : b.color);
  }

  private disposeBoxGroup(group: THREE.Group) {
    for (const child of group.children) {
      const mat = (child as THREE.Mesh | THREE.LineSegments).material;
      if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
      else (mat as THREE.Material).dispose();
    }
  }

  pickBox(clientX: number, clientY: number): string | null {
    const rect = this.container.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.boxLayer.updateMatrixWorld(true);
    this.raycaster.setFromCamera(ndc, this.camera);
    const meshes: THREE.Object3D[] = [];
    for (const group of this.boxGroups.values()) meshes.push(group.children[0]);
    const hit = this.raycaster.intersectObjects(meshes, false)[0];
    const id = hit?.object.userData.boxId;
    return typeof id === "string" ? id : null;
  }

  pickPoint(clientX: number, clientY: number): PointCloudPick | null {
    const rect = this.container.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.raycaster.setFromCamera(ndc, this.camera);
    const pointPickTarget =
      this.pointRaycastObject ?? (this.points instanceof THREE.Points ? this.points : null);
    if (!pointPickTarget) return null;

    const previousThreshold = this.raycaster.params.Points?.threshold ?? 1;
    this.raycaster.params.Points = {
      ...(this.raycaster.params.Points ?? {}),
      threshold: 0.3,
    };
    let hit: THREE.Intersection | undefined;
    try {
      hit = this.raycaster.intersectObject(pointPickTarget, false)[0];
    } finally {
      this.raycaster.params.Points = {
        ...(this.raycaster.params.Points ?? {}),
        threshold: previousThreshold,
      };
    }
    if (
      !hit ||
      hit.index == null ||
      !Number.isInteger(hit.index) ||
      hit.index < 0 ||
      hit.index >= this.renderedPointCount
    ) {
      return null;
    }
    const positionAttribute = pointPickTarget.geometry.getAttribute(
      "position",
    ) as THREE.BufferAttribute;
    const pointPosition = new THREE.Vector3().fromBufferAttribute(positionAttribute, hit.index);
    pointPickTarget.updateMatrixWorld(true);
    pointPosition.applyMatrix4(pointPickTarget.matrixWorld);
    return {
      pointIndex: hit.index * this.pointIndexStride,
      position: [pointPosition.x, pointPosition.y, pointPosition.z],
    };
  }

  focusBox(id: string): boolean {
    const group = this.boxGroups.get(id);
    if (!group) return false;
    const framed = framePerspectiveBox({
      boxCenter: group.position.toArray() as [number, number, number],
      boxSize: [Math.abs(group.scale.x), Math.abs(group.scale.y), Math.abs(group.scale.z)],
      cameraPosition: this.camera.position.toArray() as [number, number, number],
      cameraTarget: this.controls.target.toArray() as [number, number, number],
      fallbackDirection: [-this.forward.x * 2.2, -this.forward.y * 2.2, 1.2],
      verticalFovDeg: this.camera.fov,
      aspect: this.camera.aspect,
      fullFrameFar: this.viewRadius * 50,
    });
    this.controls.target.fromArray(framed.target);
    this.camera.position.fromArray(framed.position);
    this.camera.near = framed.near;
    this.camera.far = framed.far;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    return true;
  }

  placeOnGround(clientX: number, clientY: number): [number, number, number] | null {
    const rect = this.container.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.raycaster.setFromCamera(ndc, this.camera);

    const pointHit = this.pickPoint(clientX, clientY);
    if (pointHit) return pointHit.position;

    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), -this.groundZ);
    const hit = new THREE.Vector3();
    if (!this.raycaster.ray.intersectPlane(plane, hit)) return null;
    return [hit.x, hit.y, this.groundZ];
  }

  setTransformHandler(cb: ((id: string, psr: BoxPsr, commit: boolean) => void) | null) {
    this.onTransformChange = cb;
  }

  attachTransform(id: string) {
    const group = this.boxGroups.get(id);
    if (group && this.transform.object === group) return;
    if (!group && !this.transform.object) return;
    if (group) this.transform.attach(group);
    else this.transform.detach();
    this.invalidateMain();
  }

  detachTransform() {
    if (!this.transform.object) return;
    this.transform.detach();
    this.invalidateMain();
  }

  setTransformMode(mode: TransformMode) {
    this.transform.setMode(mode);
    const rotateOnlyZ = mode === "rotate";
    this.transform.showX = !rotateOnlyZ;
    this.transform.showY = !rotateOnlyZ;
    this.transform.showZ = true;
    this.invalidateMain();
  }

  shouldIgnoreClick(): boolean {
    if (this.suppressClickAfterDrag) {
      this.suppressClickAfterDrag = false;
      return true;
    }
    return false;
  }

  isTransformDragging(): boolean {
    return this.transformDragging;
  }

  private emitTransform(commit: boolean) {
    const obj = this.transform.object;
    const id = obj?.userData.boxId;
    if (!obj || typeof id !== "string" || !this.onTransformChange) return;
    const e = new THREE.Euler().setFromQuaternion(obj.quaternion, "XYZ");
    const s = obj.scale;
    this.onTransformChange(
      id,
      {
        center: [obj.position.x, obj.position.y, obj.position.z],
        size: [
          Math.max(Math.abs(s.x), 0.05),
          Math.max(Math.abs(s.y), 0.05),
          Math.max(Math.abs(s.z), 0.05),
        ],
        rotation: [e.x, e.y, e.z],
      },
      commit,
    );
  }

  dispose() {
    this.disposed = true;
    const debugContainer = this.container as HTMLElement & { __pointCloudScene?: PointCloudScene };
    const ownsDebugContainer = debugContainer.__pointCloudScene === this;
    if (ownsDebugContainer) delete debugContainer.__pointCloudScene;
    if (POINT_CLOUD_TEST_PROBES_ENABLED && ownsDebugContainer) {
      delete this.container.dataset.pointcloudRendererCount;
      delete this.container.dataset.pointcloudSubmitCount;
      delete this.container.dataset.pointcloudMainPassCount;
      delete this.container.dataset.pointcloudTriPassCount;
      delete this.container.dataset.pointcloudLastSubmitAt;
      delete this.container.dataset.pointcloudTriActiveRenderAt;
      delete this.container.dataset.pointcloudLastInvalidateReason;
      delete this.container.dataset.pointcloudLastInvalidateAt;
    }
    this.renderScheduler.dispose();
    this.removePoints();
    this.triViewPass.dispose();
    for (const group of this.boxGroups.values()) this.disposeBoxGroup(group);
    this.boxGroups.clear();
    for (const sprite of this.boxLabels.values()) {
      this.labelLayer.remove(sprite);
      this.disposeLabelSprite(sprite);
    }
    this.boxLabels.clear();
    for (const seg of this.referenceBoxes) {
      this.referenceLayer.remove(seg);
      (seg.material as THREE.Material).dispose();
    }
    this.referenceBoxes = [];
    this.clearMeasurementLayer();
    for (const pointLayer of this.neighborPoints) this.disposeNeighborPointLayer(pointLayer);
    this.neighborPoints = [];
    this.unitEdges.dispose();
    this.unitBox.dispose();
    this.qualityGroundGrid.geometry.dispose();
    const qualityGroundMaterial = this.qualityGroundGrid.material;
    if (Array.isArray(qualityGroundMaterial)) {
      qualityGroundMaterial.forEach((material) => material.dispose());
    } else {
      qualityGroundMaterial.dispose();
    }
    this.transform.detach();
    this.scene.remove(this.transform.getHelper());
    this.transform.dispose();
    this.disposeAxisGizmo();
    this.controls.dispose();
    disposePointCloudRenderer(this.renderer);
    this.renderer.domElement.remove();
  }

  private disposeAxisGizmo() {
    this.axisScene.traverse((obj) => {
      const withGeometry = obj as THREE.Object3D & { geometry?: THREE.BufferGeometry };
      withGeometry.geometry?.dispose();
      const material = (
        obj as THREE.Object3D & {
          material?: THREE.Material | THREE.Material[];
        }
      ).material;
      const disposeMaterial = (mat: THREE.Material) => {
        const withMap = mat as THREE.Material & { map?: THREE.Texture | null };
        withMap.map?.dispose();
        mat.dispose();
      };
      if (Array.isArray(material)) material.forEach(disposeMaterial);
      else material?.dispose();
    });
  }
}
