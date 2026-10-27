/**
 * v0.13.2 · 裸 Three.js 点云场景(命令式薄封装,不用 react-three-fiber)。
 *
 * 职责:管 Legacy WebGL / 实验 WebGPU renderer、PerspectiveCamera / Scene / OrbitControls 生命周期,
 * 加载 PCD、按高度上色、大点云抽稀、resize、dispose。React 组件只持有一个实例
 * 并在 effect 里 mount/unmount,交互逻辑全在这里(命令式编辑器更顺,见 epic §14.10.4)。
 */
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

// 超过此点数按步长降采样渲染(大点云性能地基;真正 LOD/分块留后续切片)。
const DEFAULT_DECIMATE_THRESHOLD = 500_000;
const WEBGPU_MIN_POINT_CAPACITY = 65_536;
const POINT_CLOUD_TEST_PROBES_ENABLED = import.meta.env.DEV || import.meta.env.MODE === "e2e";

function webGpuPointCapacity(pointCount: number): number {
  let capacity = WEBGPU_MIN_POINT_CAPACITY;
  while (capacity < pointCount) capacity *= 2;
  return capacity;
}

// v0.15.18 · 邻帧点云叠加弱化色,与当前帧的高度色带 / 相机上色强区分。
// 前/后帧分色(过去冷蓝 / 未来暖橙),让动态目标拖影读起来是"运动方向"而非乱噪。
const NEIGHBOR_PAST_COLOR = 0x4a90d9; // 过去帧:冷蓝
const NEIGHBOR_FUTURE_COLOR = 0xd98a4a; // 未来帧:暖橙
// 时序淡出:帧距越远越淡(distance=1 最实),拖影随距离自然衰减。
function neighborOpacity(distance: number): number {
  return Math.max(0.15, 0.5 - (Math.max(1, distance) - 1) * 0.12);
}

interface NeighborPointLayer {
  object: THREE.Object3D;
  geometry: THREE.BufferGeometry;
  material: THREE.PointsMaterial | null;
  webGpuLayer: WebGpuPointCloudLayer | null;
}

// 选中框高亮色(线框)。未选中用类别色。
const SELECTED_EDGE_COLOR = 0xffd54a;
const TRANSFORM_SIZE_MIN = 0.35;
const TRANSFORM_SIZE_MAX = 1.15;

export interface PointCloudStats {
  totalPoints: number;
  renderedPoints: number;
  decimated: boolean;
  decimateStride: number;
}

/** v0.13.3 · 渲染层用的 3D 框输入(PSR + 类别色 + 选中态),由 React 从标注派生。 */
export interface SceneBox {
  id: string;
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  /** 类别色(hex 字符串,如 "#4f8cff");选中时线框另用高亮色。 */
  color: string;
  selected: boolean;
  /**
   * 框顶悬浮文本标签(类别名/轨迹号/属性,已在 React 侧按 labelContent + 可见性组装好)。
   * 空 / undefined = 不显示标签。渲染层只负责把字符串画成 billboard sprite。
   */
  label?: string;
}

/** v0.14.1 · 邻帧参考框(只读叠加层): PSR + 类别色, 不可选不可拖。 */
export interface ReferenceBox {
  id: string;
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  color: string;
  /** v0.15.17 · scope=all 下非选中 group 的框弱化显示(更低透明度)。 */
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

/** v0.13.3 · TransformControls 拖拽结束时回传的 PSR(center/size/rotation)。 */
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
  /** 异步解析完成后再复核，阻止过期帧覆盖新帧。 */
  shouldCommit?: () => boolean;
  /** 相机上色开启时先隐藏高度色，待 RGB 就绪后原子显示。 */
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
  // v0.13.6 · 载帧时存的原色(高度色带),相机上色关闭时还原。
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

  // v0.13.3 · 3D 框图层:每框一个 Group(线框 LineSegments + 半透明拾取 Mesh),
  // 用 boxToMatrix4 设矩阵。共享单位几何(边长 1),材质按框单独建(颜色不同)。
  private boxLayer = new THREE.Group();
  private boxGroups = new Map<string, THREE.Group>();
  private boxSignature = "";
  private readonly unitEdges = new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1));
  private readonly unitBox = new THREE.BoxGeometry(1, 1, 1);
  private readonly raycaster = new THREE.Raycaster();

  // 框标签图层:悬浮在框顶的文字 billboard(CanvasTexture → Sprite)。独立于 boxLayer,
  // 因为 box group 用非均匀 scale + 绕 Z 旋转,把 sprite 挂进去会因 shear 让文字变形;
  // 这里按世界坐标独立定位、不缩放。每个有 label 的框一条,按框 id 复用。
  private labelLayer = new THREE.Group();
  private boxLabels = new Map<string, THREE.Sprite>();

  // v0.14.1 · 邻帧参考框图层:半透明 dashed 线框, 不参与 raycast(不加入 boxGroups,
  // pickBox 只遍历 boxGroups), 仅作时序连续性参考。切 selectedGroupId / overlay K 时整层重建。
  private referenceLayer = new THREE.Group();
  private referenceBoxes: THREE.LineSegments[] = [];
  private referenceBoxSignature = "";

  // 会话态测量辅助层：只渲染主视图，不参与框拾取、标注、导出或三视图编辑。
  private measurementLayer = new THREE.Group();

  // v0.15.18 · 邻帧点云叠加图层:各邻帧点云经 ego 补偿(对象矩阵)对齐到当前帧 ego 系,
  // 弱化单色 + 低透明,与当前帧点区分。切帧 / 关开关时整层重建并 dispose。
  private neighborLayer = new THREE.Group();
  private neighborPoints: NeighborPointLayer[] = [];

  // v0.13.3 · 鲁棒取景中心/半径(mean ± 2.5σ,见 setRobustFrame)。
  private readonly viewCenter = new THREE.Vector3();
  private viewRadius = 10;

  // v0.13.7 · resetView 默认视向的水平「前方」(= front 相机光轴水平投影)。
  // 默认 +Y(历史行为:相机蹲 -Y 看 +Y);由 setViewForward 跟随实际车头改写。
  private readonly forward = new THREE.Vector3(0, 1, 0);

  // v0.13.3 · 估计的地面高度 z(低分位,见 estimateGroundZ),放置新框时落在此平面上。
  private groundZ = 0;

  // 主视图左下角方向辅助器:同一个 renderer 的小 viewport,跟随主相机旋转。
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

  // v0.13.3 · 选中框拖拽编辑(平移/yaw/缩放)。gizmo 挂 getHelper() 到场景。
  private readonly transform: TransformControls;
  private onTransformChange: ((id: string, psr: BoxPsr, commit: boolean) => void) | null = null;
  private transformDragging = false;
  private transformChangedDuringDrag = false;
  // 拖拽结束会触发一次 click,不应改变选中 —— 用此标记吞掉那次 click。
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
    // The renderer fills its surface; the main viewport remains the input host.
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
    this.camera.up.set(0, 0, 1); // 点云 z 朝上(自动驾驶惯例)

    this.controls = new OrbitControls(this.camera, container);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.1;
    this.setOrbitMouseMode("orbit");
    this.controls.addEventListener("change", () => {
      this.onViewChange?.(this.getViewState());
      this.invalidateMain("orbit-controls");
    });

    // 网格地平面参考(xy 平面)。
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

    // 变换 gizmo:在框 local 空间编辑(缩放/旋转沿框自身轴)。
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
      this.controls.enabled = !dragging; // 拖 gizmo 时禁用 orbit,避免相机乱转
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
    // WebGPURenderer（含 WebGL2 fallback）的 clear/scissor 状态由 backend 延迟提交；只画
    // 正交 pass 时清三视图区域可能先清掉完整 swapchain。任何可见 pass 变化都先恢复主 Scene，
    // 再叠三视图，避免缩放三视图后主画布黑屏，仍由 scheduler 合并为同一个 RAF。
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
      // Clear the shared surface once. WebGPU attachment clears are not restricted by scissor.
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
          // Three scales attenuated points by the complete canvas height, not this viewport.
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

  /**
   * 加载 PCD 并渲染,返回统计;失败 throw。
   *
   * v0.13.11 · convention 用于把 src 系下的 lidar 点云就地旋转到 ISO 8855
   * (+X 前 / +Y 左 / +Z 上),下游 (色带 / robust frame / groundZ / autofit /
   * cameraAnchor / projectPoints) 全部在 ISO 系下工作。默认 iso_8855 = identity,
   * 与历史行为完全一致。
   */
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
    const f = this.forward; // 水平单位向量(车头方向)
    this.controls.target.copy(c);
    this.camera.up.set(0, 0, 1); // v0.13.9 · 还原 up(bevView 会改成水平 forward)
    // 蹲在车头反方向、抬高,看向中心 ⇒ 视线 = 车头方向(与 front 相机一致)。
    // forward 默认 (0,1,0) 时退化为历史的 (c.x, c.y - 2.2r, ...)。
    this.camera.position.set(c.x - f.x * r * 2.2, c.y - f.y * r * 2.2, c.z + r * 1.2);
    this.camera.near = Math.max(r / 100, 0.1);
    this.camera.far = r * 50;
    this.camera.updateProjectionMatrix();
    this.setOrbitMouseMode("orbit");
    this.controls.update();
  }

  /**
   * v0.13.7 · 设默认视向的水平「前方」(= front 相机光轴水平投影),使 resetView 与
   * front 相机朝向一致(健壮于任意 lidar 系前向约定)。近零向量忽略(保持上次/默认 +Y);
   * 不立即重排,下次 frameView / resetView 生效。
   */
  setViewForward(x: number, y: number) {
    if (Math.hypot(x, y) < 1e-3) return;
    this.forward.set(x, y, 0).normalize();
  }

  resetView() {
    this.frameView();
  }

  /**
   * v0.13.9 · 俯视(BEV)复位: 相机摆到稠密区正上方俯看 -Z, 车头(forward)朝屏幕上方。
   * 看 -Z 时 up 不能与视线共线 → 取水平的 forward 作 up(切回 resetView 时 frameView 还原 (0,0,1))。
   * 便于在地面平面拖框选 footprint。仍是透视相机, 不引入正交模式。
   */
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

  /**
   * v0.13.9 · 框选选点: 返回投影落在屏幕矩形(两对角 client px)内、且在相机前方的点 world 坐标
   * (Float32Array, len = 3·K)。用屏幕投影选真实点而非投地面平面 → 对物体高度/视角零视差
   * (SUSTechPOINTS 「框选 + 点云拟合」范式)。无点云 / 选不到点 → 返回 null。
   *
   * 实现: vp = projection · viewMatrixInverse; 对每点取齐次裁剪坐标, w ≤ 0 (相机后方) 丢弃,
   * 否则透视除得 NDC, 落在矩形 [nx0,nx1]×[ny0,ny1] 内即选中。
   */
  private collectPointsInScreenRect(
    x0: number,
    y0: number,
    x1: number,
    y1: number,
  ): { positions: Float32Array; indices: number[] } | null {
    const rect = this.container.getBoundingClientRect();
    const toNdcX = (cx: number) => ((cx - rect.left) / rect.width) * 2 - 1;
    const toNdcY = (cy: number) => -((cy - rect.top) / rect.height) * 2 + 1;
    // client y 越大 NDC y 越小 → 取 min/max 归一化矩形。
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
      if (v.w <= 0) continue; // 相机后方
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

  /** v0.13.9 · 框选拖拽期禁用 OrbitControls(同 gizmo 拖拽), 避免拖框时相机乱转。 */
  setBoxSelecting(active: boolean) {
    this.controls.enabled = !active;
  }

  /** 暴露当前点云 BufferGeometry，供开发诊断确认实例化 attribute 合同。 */
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
    // OrbitControls 在阻尼启用时保留 sphericalDelta / panOffset。直接应用跨帧
    // 相机会在下一次 update 又叠加旧动量，产生毫米级漂移。先在无阻尼模式冲刷
    // 内部残量，再写入目标视角；期间不向外暴露瞬时中间态。
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

  /** v0.13.6 · 当前点坐标 (N*3, lidar/world 系, 与标定同系); 供相机上色逐点投影。 */
  getPointPositions(): Float32Array | null {
    const attr = this.pointGeometry?.getAttribute("position") as THREE.BufferAttribute | undefined;
    if (!attr || this.renderedPointCount === 0) return null;
    const positions = attr.array as Float32Array;
    return positions.subarray(0, this.renderedPointCount * 3);
  }

  /** v0.13.6 · 载帧时的原色 (高度色带); 上色时无相机覆盖的点回退到它。 */
  getBaseColors(): Float32Array | null {
    return this.baseColors;
  }

  /** 在无目标点云或载入失败时移除当前点缓冲；正常换帧由 loadPcd 原子替换。 */
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

  /** Release every original point buffer when its authenticated owner retires. */
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

  /**
   * v0.13.6 · 设点云颜色。colors=相机上色结果 (N*3); null=还原原色 (高度色带)。
   * 原地写回既有 color buffer (长度一致), 触发 GPU 更新。三视图复用同一 geometry 自动跟随。
   */
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

  /** Moving the surface never recreates the renderer, scene, geometry or interaction host. */
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

  /**
   * v0.13.3 · 同步 3D 框图层到给定集合(diff 增删改)。共享单位几何;
   * 每框材质单独建(颜色不同),移除时 dispose 材质(几何只在 scene dispose 时清)。
   */
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

  /**
   * 同步框标签图层:按框 id 增删 sprite,文本变化才重画纹理,位置放框顶(随框旋转)。
   * label 为空 / undefined 的框不显示标签(拆除已有 sprite)。
   */
  private syncBoxLabels(boxes: SceneBox[], next: Set<string>) {
    // 拆除:框已删除,或该框不再需要标签。
    for (const [id, sprite] of this.boxLabels) {
      const stillHasLabel = next.has(id) && boxes.some((b) => b.id === id && b.label);
      if (!stillHasLabel) {
        this.labelLayer.remove(sprite);
        this.disposeLabelSprite(sprite);
        this.boxLabels.delete(id);
      }
    }
    // 建 / 更新。
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
      // 位置:框顶面中心(局部 +Z 半高)随框旋转,再上抬一点留白。
      const euler = new THREE.Euler(b.rotation[0], b.rotation[1], b.rotation[2], "XYZ");
      const top = new THREE.Vector3(0, 0, b.size[2] / 2 + 0.3).applyEuler(euler);
      sprite.position.set(b.center[0] + top.x, b.center[1] + top.y, b.center[2] + top.z);
    }
  }

  /** 建一个空的标签 sprite(材质 depthTest 关,画在框线之上),文本由 drawLabelTexture 填。 */
  private createLabelSprite(text: string): THREE.Sprite {
    const sprite = new THREE.Sprite(
      new THREE.SpriteMaterial({ transparent: true, depthTest: false, depthWrite: false }),
    );
    sprite.renderOrder = 4; // 在框线(3)之上
    this.drawLabelTexture(sprite, text);
    return sprite;
  }

  /** 把文本画进 CanvasTexture 贴到 sprite;按文本宽度定 canvas 尺寸,世界高度固定、宽度按纵横比。 */
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
      // 半透明底衬,保证亮 / 暗点云上都可读。
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
    // 世界尺寸:固定高度(米),宽度按 canvas 纵横比,避免文字拉伸。
    const worldH = 0.55;
    sprite.scale.set(worldH * (canvas.width / canvas.height), worldH, 1);
    sprite.userData.text = text;
  }

  private disposeLabelSprite(sprite: THREE.Sprite) {
    const mat = sprite.material as THREE.SpriteMaterial;
    mat.map?.dispose();
    mat.dispose();
  }

  /**
   * v0.14.1 · 同步邻帧参考框图层(整层清空重建)。参考框半透明 dashed 线框, 颜色取
   * 主框类别色但描边更暗, 不参与 raycast(不加入 boxGroups)。切 selectedGroupId /
   * overlay K 时由 React 重新调一次。
   */
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
        // v0.15.17 · scope=all 非选中 group 的框更淡,突出当前对象。
        opacity: b.dim ? 0.22 : 0.5,
        depthTest: false,
        dashSize: 0.3,
        gapSize: 0.15,
      });
      const seg = new THREE.LineSegments(this.unitEdges, mat);
      seg.computeLineDistances(); // dashed 必需
      seg.renderOrder = 1; // 在实框(2/3)之下
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

  /**
   * v0.15.18 · 邻帧点云叠加。每个 frame 的 positions 是该邻帧 ISO ego 系点;matrix 是
   * inv(T_cur)@T_nbr(frameRelMatrix),作为对象矩阵直接把整片点云对齐到当前帧 ego 系
   * (刚体变换,GPU 端做,无逐点 CPU 开销)。静止背景重合加密,动态目标留拖影。
   * v0.15.18 · 视觉缓解动态拖影:前/后帧分色(过去冷蓝 / 未来暖橙)+ 按帧距时序淡出,
   * 拖影读起来是运动方向而非乱噪。整层重建,旧 geometry/material 全部 dispose。
   */
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
      object.renderOrder = 0; // 在当前帧点(默认)与框之下
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
    group.userData.boxId = id; // 供 TransformControls 拖拽结束回查
    // 用 position/quaternion/scale 表示框(matrixAutoUpdate 默认 true),让 TransformControls
    // 能直接驱动。depthTest:false + renderOrder:框始终画在点云之上。否则细线框会被点云遮挡 /
    // 淹没,远视角下几乎不可见(标注 overlay 的惯例做法)。fill 兼作射线拾取目标 + 选中淡填充。
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
    fill.userData.boxId = id; // 供 pickBox 反查
    const edges = new THREE.LineSegments(
      this.unitEdges,
      new THREE.LineBasicMaterial({ transparent: true, depthTest: false }),
    );
    edges.renderOrder = 3;
    group.add(fill); // children[0] = 拾取 mesh
    group.add(edges); // children[1] = 线框
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

  /** 屏幕坐标射线拾取最近的框,返回其 id;未命中返回 null。 */
  pickBox(clientX: number, clientY: number): string | null {
    const rect = this.container.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.boxLayer.updateMatrixWorld(true); // 确保拾取前矩阵最新
    this.raycaster.setFromCamera(ndc, this.camera);
    const meshes: THREE.Object3D[] = [];
    for (const group of this.boxGroups.values()) meshes.push(group.children[0]);
    const hit = this.raycaster.intersectObjects(meshes, false)[0];
    const id = hit?.object.userData.boxId;
    return typeof id === "string" ? id : null;
  }

  /** 屏幕坐标严格命中最近的渲染点；未命中不回落地面或自由空间。 */
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

  /** 保持当前观察方向 / up / 模式，把相机安全取景到指定框。 */
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

  /**
   * v0.13.3 · 屏幕坐标射线 → 世界落点 [x,y,z],供放置新框(透视拖拽不准,故先点落点 +
   * 默认尺寸,再用数值面板 / gizmo / Q 一键贴合精修)。射线极端时返回 null。
   *
   * v0.13.8.1 · **优先打点云**(SUSTechPOINTS / xtreme1 通行做法):
   *   1. 射线与点云相交 → 用最近命中点的 (x,y,z) 作落点,框底贴该点。
   *   2. 未命中(点击空地)→ fallback 到水平面相交,用中位数 groundZ。
   * 解决 v0.13.3 「总用 groundZ」的两个体感问题:
   *   - 斜视角 + lidar 缺自车正下方点 → 1% 分位 groundZ 比"视觉地面"低,框总埋地下;
   *   - 想标车顶物 / 屋顶物 → 总强行贴 groundZ,要手动调 cz。
   * 点云命中容差用 0.3m(默认 1m 太松,易抓到背景远点;0.3m 跟点云密度匹配)。
   */
  placeOnGround(clientX: number, clientY: number): [number, number, number] | null {
    const rect = this.container.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.raycaster.setFromCamera(ndc, this.camera);

    const pointHit = this.pickPoint(clientX, clientY);
    if (pointHit) return pointHit.position;

    // Fallback:射线与 z=groundZ 水平面相交(空地点击 / 未载点云)。
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), -this.groundZ);
    const hit = new THREE.Vector3();
    if (!this.raycaster.ray.intersectPlane(plane, hit)) return null;
    return [hit.x, hit.y, this.groundZ];
  }

  /** React 注册变换回调；拖动中回传预览，松手后回传提交。 */
  setTransformHandler(cb: ((id: string, psr: BoxPsr, commit: boolean) => void) | null) {
    this.onTransformChange = cb;
  }

  /** 把变换 gizmo 挂到指定框;找不到则脱离。 */
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

  /** 切换 gizmo 模式;旋转仅绕 Z(yaw,7-DoF),平移/缩放允许各轴。 */
  setTransformMode(mode: TransformMode) {
    this.transform.setMode(mode);
    const rotateOnlyZ = mode === "rotate";
    this.transform.showX = !rotateOnlyZ;
    this.transform.showY = !rotateOnlyZ;
    this.transform.showZ = true;
    this.invalidateMain();
  }

  /** 拖拽刚结束触发的 click 应被吞掉(否则会误改选中);返回并清标记。 */
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
        // 缩放 gizmo 可能拖出负 scale(翻转);尺寸必须为正,取绝对值并设下限 0.05m。
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
    // 参考框共用 this.unitEdges 几何(下面统一 dispose),仅各自持有 LineDashedMaterial,
    // 与 setReferenceBoxes 的清理口径一致:只 dispose 材质。
    for (const seg of this.referenceBoxes) {
      this.referenceLayer.remove(seg);
      (seg.material as THREE.Material).dispose();
    }
    this.referenceBoxes = [];
    this.clearMeasurementLayer();
    // v0.15.18 · 邻帧点云图层:各自持有 geometry + material,全部 dispose。
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
    // renderer.dispose() 只释放渲染缓存/着色器,不丢弃底层 WebGL context(靠 GC 回收,
    // 时机不定)。dev 下 StrictMode 双调用 + 反复 HMR 会让旧 context 堆积到浏览器上限
    // (Chrome ~16),后续 new WebGLRenderer 报 "Error creating WebGL context"。
    // forceContextLoss() 主动触发 context loss,让浏览器立即回收。
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
