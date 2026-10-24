import { originalOwnerKey } from "@/api/originals";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";
/**
 * v0.17.x · 巨石拆分 Epic 收尾 —— 从 ThreeDWorkbench 抽出的 Three.js 场景生命周期 hook。
 *
 * 封装 `PointCloudScene` 的 实例化 / 销毁 / 偏好同步 / 点云加载 / 框图层 / gizmo 挂载 /
 * W-E-R 模式键 / 相机视图存档(rAF 防抖)。与 ThreeDWorkbench 的唯一耦合点 ——
 * gizmo 拖拽中回写本地预览、结束后 PATCH —— 走 `onTransformPreview` / `onTransformCommit`
 * 回调参数注入，form / mutate / history 仍由壳组件持有，边界干净。
 *
 * 守护:点云 E2E 护栏网(smoke 加载 / gizmo W 拖拽→PATCH / B 放置 / cx 编辑 / Q 贴合)。
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MutableRefObject,
  type RefObject,
} from "react";
import { isWorkbenchInteractionBlocked } from "../../state/workbenchInteractionGuards";

import type { PointcloudCameraState } from "@/api/auth";
import type { WorkbenchLayoutPatch } from "@/pages/Workbench/state/useWorkbenchConfig";
import type { LidarAxisConvention } from "@/types";

import {
  PointCloudScene,
  type BoxPsr,
  type PointCloudStats,
  type PointCloudViewState,
  type SceneMeasurementPath,
  type SceneBox,
} from "./PointCloudScene";
import type {
  PointCloudRendererMode,
  PointCloudRendererStatus,
} from "./rendering/pointCloudRenderer";
import type { PointCloudVisibleRegions } from "./PointCloudTriViewPass";
import { cancelActivePointCloudFrameLoad } from "./pointCloudAssetCache";
import { markPointCloudPaint } from "./pointCloudTiming";
import { publishPointCloudResourceTrace } from "@/utils/pointCloudNavigationDiagnostics";

interface UsePointCloudSceneParams {
  /** 渲染容器(壳组件持有的 DOM ref)。 */
  viewportRef: RefObject<HTMLDivElement | null>;
  /** Optional workspace-wide canvas host; input and projection stay on viewportRef. */
  renderSurface?: HTMLElement | null;
  /** Changes after Dockview has measured and clipped moving/hidden groups. */
  layoutKey?: string | number;
  getVisibleRegions?: PointCloudVisibleRegions;
  /** 场景实例 ref —— 由壳组件持有(稳定),本 hook 负责其生命周期写入/清理;
   *  壳层各交互 handler(pickBox / placeOnGround / getPointPositions …)共用同一 ref。 */
  sceneRef: MutableRefObject<PointCloudScene | null>;
  /** 性能档位抽稀阈值。 */
  pcdDecimate: number;
  /** 工作台打开时冻结的 renderer 选择；设置切换后需刷新。 */
  rendererMode?: PointCloudRendererMode;
  pointSize: number;
  showGrid: boolean;
  showAxisGizmo: boolean;
  /** OrbitControls 阻尼系数(数值,非开关)。 */
  cameraDamping: number;
  /** 是否把相机视角落库(跨帧/跨会话记忆)。 */
  persistCameraView: boolean;
  /** manifest 的点云 URL(presigned);缺省时不加载。 */
  pointCloudUrl: string | undefined;
  /** 相机上色开启时，新点云在 RGB 完成前不显示中间高度色。 */
  deferPointCloudDisplay?: boolean;
  /** scene_id；无 scene 的单帧任务传 taskId，防止无关任务复用运行时视角。 */
  continuityKey: string | null;
  axisConvention: LidarAxisConvention;
  /** 标注框 → 渲染输入(选中态 / 颜色 / 草稿覆盖已在壳层算好)。 */
  boxes: SceneBox[];
  /** 会话态测量路径；只同步到主 scene 的辅助层。 */
  measurementPaths?: SceneMeasurementPath[];
  selectedId: string | null;
  /** 选中且可编辑时才挂 gizmo / 接 W-E-R。 */
  selectedPsrEditable: boolean;
  pointcloudCamera: PointcloudCameraState | null;
  onWorkbenchLayoutChange: (patch: WorkbenchLayoutPatch) => void;
  /** 载帧与视角恢复完成后，同步壳层的相机模式按钮状态。 */
  onViewModeChange: (mode: PointCloudViewState["mode"]) => void;
  /** gizmo 拖拽结束:回写表单 + PATCH 持久化(壳层注入,闭合 form / mutate / history)。 */
  onTransformCommit: (id: string, psr: BoxPsr) => void;
  /** gizmo 拖拽中:只更新壳层本地 PSR，供三视图和相机投影实时预览。 */
  onTransformPreview: (id: string, psr: BoxPsr) => void;
}

interface UsePointCloudSceneResult {
  /** 载帧统计(渲染点数 / 抽稀);null = 未加载。其变化亦作"换帧"信号被壳层多处消费。 */
  stats: PointCloudStats | null;
  loadError: string | null;
  rendererError: string | null;
  isLoading: boolean;
  /** stats 所属的精确 URL，用于防止新 manifest 搭配旧点缓冲。 */
  loadedPointCloudUrl: string | null;
  rendererStatus: PointCloudRendererStatus | null;
  retryLoad: () => void;
}

const RUNTIME_VIEW_TRANSFER_TTL_MS = 10_000;
const EMPTY_MEASUREMENT_PATHS: SceneMeasurementPath[] = [];
let pendingRuntimeViewTransfer: {
  continuityKey: string;
  view: PointCloudViewState;
  expiresAt: number;
} | null = null;

function storeRuntimeViewTransfer(continuityKey: string, view: PointCloudViewState) {
  pendingRuntimeViewTransfer = {
    continuityKey,
    view,
    expiresAt: Date.now() + RUNTIME_VIEW_TRANSFER_TTL_MS,
  };
}

function takeRuntimeViewTransfer(continuityKey: string | null): PointCloudViewState | null {
  const pending = pendingRuntimeViewTransfer;
  pendingRuntimeViewTransfer = null;
  if (!pending || continuityKey === null) return null;
  if (pending.continuityKey !== continuityKey || pending.expiresAt < Date.now()) return null;
  return pending.view;
}

export function usePointCloudScene(params: UsePointCloudSceneParams): UsePointCloudSceneResult {
  const {
    viewportRef,
    renderSurface = null,
    layoutKey,
    getVisibleRegions,
    sceneRef,
    pcdDecimate,
    rendererMode = "legacy",
    pointSize,
    showGrid,
    showAxisGizmo,
    cameraDamping,
    persistCameraView,
    pointCloudUrl,
    deferPointCloudDisplay = false,
    continuityKey,
    axisConvention,
    boxes,
    measurementPaths = EMPTY_MEASUREMENT_PATHS,
    selectedId,
    selectedPsrEditable,
    pointcloudCamera,
    onWorkbenchLayoutChange,
    onViewModeChange,
    onTransformCommit,
    onTransformPreview,
  } = params;

  const [loadState, setLoadState] = useState<{
    stats: PointCloudStats | null;
    loadedPointCloudUrl: string | null;
    isLoading: boolean;
  }>({ stats: null, loadedPointCloudUrl: null, isLoading: false });
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rendererError, setRendererError] = useState<string | null>(null);
  const [rendererStatus, setRendererStatus] = useState<PointCloudRendererStatus | null>(null);
  const [rendererReadyVersion, setRendererReadyVersion] = useState(0);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [rendererAttempt, setRendererAttempt] = useState(0);
  const retryLoad = useCallback(() => {
    if (sceneRef.current) setLoadAttempt((value) => value + 1);
    else setRendererAttempt((value) => value + 1);
  }, [sceneRef]);
  const [rendererCircuitReason, setRendererCircuitReason] = useState<string | null>(null);
  const deferPointCloudDisplayRef = useRef(deferPointCloudDisplay);
  deferPointCloudDisplayRef.current = deferPointCloudDisplay;

  const persistCameraViewRef = useRef(persistCameraView);
  persistCameraViewRef.current = persistCameraView;
  const pointcloudCameraRef = useRef(pointcloudCamera);
  pointcloudCameraRef.current = pointcloudCamera;
  const onWorkbenchLayoutChangeRef = useRef(onWorkbenchLayoutChange);
  onWorkbenchLayoutChangeRef.current = onWorkbenchLayoutChange;
  const onViewModeChangeRef = useRef(onViewModeChange);
  onViewModeChangeRef.current = onViewModeChange;
  const onTransformCommitRef = useRef(onTransformCommit);
  onTransformCommitRef.current = onTransformCommit;
  const onTransformPreviewRef = useRef(onTransformPreview);
  onTransformPreviewRef.current = onTransformPreview;
  const cameraSaveRafRef = useRef<number | null>(null);
  const pendingCameraViewRef = useRef<PointCloudViewState | null>(null);
  const suspendCameraSaveRef = useRef(false);
  const loadSequenceRef = useRef(0);
  const loadedContinuityKeyRef = useRef<string | null>(null);
  const renderSurfaceRef = useRef(renderSurface);
  renderSurfaceRef.current = renderSurface;
  const visibleRegionsRef = useRef(getVisibleRegions);
  visibleRegionsRef.current = getVisibleRegions;

  const scheduleCameraViewSave = useCallback((view: PointCloudViewState) => {
    if (suspendCameraSaveRef.current) return;
    const loadedContinuityKey = loadedContinuityKeyRef.current;
    if (loadedContinuityKey !== null) storeRuntimeViewTransfer(loadedContinuityKey, view);
    if (!persistCameraViewRef.current) return;
    pendingCameraViewRef.current = view;
    if (cameraSaveRafRef.current !== null) return;
    cameraSaveRafRef.current = window.requestAnimationFrame(() => {
      cameraSaveRafRef.current = null;
      const next = pendingCameraViewRef.current;
      pendingCameraViewRef.current = null;
      if (!next || !persistCameraViewRef.current) return;
      onWorkbenchLayoutChangeRef.current({ pointcloudCamera: next });
    });
  }, []);

  // 实例化 / 销毁 Scene(随容器挂载一次)。
  const originalOwner = useOriginalAssetOwner();
  useLayoutEffect(() => {
    sceneRef.current?.clearPointCloud();
    cancelActivePointCloudFrameLoad();
  }, [originalOwner, sceneRef]);
  useEffect(() => {
    const container = viewportRef.current;
    if (!container) return;
    let cancelled = false;
    let scene: PointCloudScene | null = null;
    let ro: ResizeObserver | null = null;
    setRendererStatus(null);
    setRendererError(null);
    setLoadState({ stats: null, loadedPointCloudUrl: null, isLoading: !!pointCloudUrl });
    const effectiveMode = rendererCircuitReason ? "legacy" : rendererMode;
    void PointCloudScene.create(container, {
      renderSurface: renderSurfaceRef.current,
      getVisibleRegions: (element) =>
        visibleRegionsRef.current?.(element) ?? [element.getBoundingClientRect()],
      decimateThreshold: pcdDecimate,
      rendererMode: effectiveMode,
      onDeviceLost: (reason) => {
        if (!cancelled && rendererMode === "webgpu-experimental") {
          setRendererCircuitReason(`device-lost: ${reason}`);
        }
      },
    })
      .then((created) => {
        if (cancelled) {
          created.dispose();
          return;
        }
        scene = created;
        sceneRef.current = created;
        created.setPointSize(pointSize);
        created.setGridVisible(showGrid);
        created.setAxisGizmoVisible(showAxisGizmo);
        created.setCameraDamping(cameraDamping);
        created.setViewChangeHandler(scheduleCameraViewSave);
        created.setTransformHandler((id, psr, commit) => {
          if (commit) onTransformCommitRef.current(id, psr);
          else onTransformPreviewRef.current(id, psr);
        });
        ro = new ResizeObserver(() => created.resize());
        ro.observe(container);
        const status = created.getRendererStatus();
        setRendererStatus(
          rendererCircuitReason
            ? { ...status, requestedMode: rendererMode, fallbackReason: rendererCircuitReason }
            : status,
        );
        setRendererReadyVersion((value) => value + 1);
      })
      .catch((error) => {
        if (cancelled) return;
        setLoadState({ stats: null, loadedPointCloudUrl: null, isLoading: false });
        setRendererError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
      if (cameraSaveRafRef.current !== null) {
        window.cancelAnimationFrame(cameraSaveRafRef.current);
        cameraSaveRafRef.current = null;
      }
      const loadedContinuityKey = loadedContinuityKeyRef.current;
      if (scene && loadedContinuityKey !== null && !suspendCameraSaveRef.current) {
        storeRuntimeViewTransfer(loadedContinuityKey, scene.getViewState());
      }
      ro?.disconnect();
      scene?.dispose();
      if (sceneRef.current === scene) sceneRef.current = null;
    };
    // rendererMode 在工作台打开时冻结；只有 page circuit 会触发同页重建。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rendererCircuitReason, rendererMode, rendererAttempt]);

  useLayoutEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    scene.setRenderSurface(renderSurface);
    const observer = new ResizeObserver(() => scene.resize());
    if (renderSurface) observer.observe(renderSurface);
    return () => observer.disconnect();
  }, [renderSurface, rendererReadyVersion, sceneRef]);

  useLayoutEffect(() => {
    sceneRef.current?.resize();
  }, [layoutKey, getVisibleRegions, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setPointSize(pointSize);
  }, [pointSize, loadState.stats, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setGridVisible(showGrid);
  }, [showGrid, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setAxisGizmoVisible(showAxisGizmo);
  }, [showAxisGizmo, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setCameraDamping(cameraDamping);
  }, [cameraDamping, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setDecimateThreshold(pcdDecimate);
  }, [pcdDecimate, rendererReadyVersion, sceneRef]);

  // manifest 到位后加载点云。
  // v0.13.11 · 传入 axisConvention,scene 内部加载完 PCD 立即把 positions 旋到 ISO 系。
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) {
      if (pointCloudUrl) {
        publishPointCloudResourceTrace(pointCloudUrl, {
          type: "waiting-renderer",
          status: "pending",
          pending: true,
        });
      }
      return;
    }
    const loadSequence = ++loadSequenceRef.current;
    setLoadError(null);
    setLoadState((previous) =>
      pointCloudUrl
        ? { ...previous, isLoading: true }
        : { stats: null, loadedPointCloudUrl: null, isLoading: false },
    );
    if (!pointCloudUrl) {
      scene.clearPointCloud();
      suspendCameraSaveRef.current = false;
      return;
    }
    publishPointCloudResourceTrace(pointCloudUrl, {
      type: "load-start",
      status: "pending",
      pending: true,
    });
    const runtimeView =
      continuityKey !== null && loadedContinuityKeyRef.current === continuityKey
        ? scene.getViewState()
        : takeRuntimeViewTransfer(continuityKey);
    let cancelled = false;
    suspendCameraSaveRef.current = true;
    scene
      .loadPcd(pointCloudUrl, axisConvention, {
        shouldCommit: () =>
          !cancelled &&
          originalOwner === originalOwnerKey() &&
          loadSequence === loadSequenceRef.current,
        visible: !deferPointCloudDisplayRef.current,
      })
      .then((s) => {
        if (
          cancelled ||
          originalOwner !== originalOwnerKey() ||
          loadSequence !== loadSequenceRef.current
        ) {
          publishPointCloudResourceTrace(pointCloudUrl, {
            type: "load-stale-result",
            status: cancelled ? "cancelled" : "superseded",
            pending: false,
          });
          return;
        }
        if (runtimeView) {
          scene.applyViewState(runtimeView);
        } else if (persistCameraViewRef.current) {
          scene.applyViewState(pointcloudCameraRef.current);
        }
        loadedContinuityKeyRef.current = continuityKey;
        onViewModeChangeRef.current(scene.getViewState().mode);
        setLoadState({ stats: s, loadedPointCloudUrl: pointCloudUrl, isLoading: false });
        publishPointCloudResourceTrace(pointCloudUrl, {
          type: "load-committed",
          status: "success",
          pending: false,
        });
        markPointCloudPaint(
          "geometry-ready",
          pointCloudUrl,
          () => !cancelled && loadSequence === loadSequenceRef.current,
        );
        suspendCameraSaveRef.current = false;
      })
      .catch((e) => {
        if (
          cancelled ||
          originalOwner !== originalOwnerKey() ||
          loadSequence !== loadSequenceRef.current
        ) {
          publishPointCloudResourceTrace(pointCloudUrl, {
            type: "load-rejected-after-cancel",
            status: cancelled ? "cancelled" : "superseded",
            pending: false,
          });
          return;
        }
        suspendCameraSaveRef.current = false;
        scene.clearPointCloud();
        setLoadState({ stats: null, loadedPointCloudUrl: null, isLoading: false });
        setLoadError(e instanceof Error ? e.message : String(e));
        publishPointCloudResourceTrace(pointCloudUrl, {
          type: "load-error",
          status: e instanceof Error ? e.name : "unknown-error",
          pending: false,
        });
      });
    return () => {
      cancelled = true;
      publishPointCloudResourceTrace(pointCloudUrl, {
        type: "load-cancel-requested",
        status: "cancelled",
        pending: true,
      });
      cancelActivePointCloudFrameLoad();
      if (
        continuityKey !== null &&
        loadedContinuityKeyRef.current === continuityKey &&
        !suspendCameraSaveRef.current
      ) {
        storeRuntimeViewTransfer(continuityKey, scene.getViewState());
      }
    };
  }, [
    originalOwner,
    pointCloudUrl,
    continuityKey,
    axisConvention,
    rendererReadyVersion,
    sceneRef,
    loadAttempt,
  ]);

  // 同步 3D 框图层(标注 / 选中变化)。scene 在挂载 effect 里先建,本 effect 后跑。
  useEffect(() => {
    sceneRef.current?.setBoxes(boxes);
  }, [boxes, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setMeasurementPaths(measurementPaths);
  }, [measurementPaths, rendererReadyVersion, loadState.loadedPointCloudUrl, sceneRef]);

  // 选中框时挂变换 gizmo,取消选中时脱离；同一次 render 中 setBoxes effect 先执行。
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (selectedId && selectedPsrEditable) scene.attachTransform(selectedId);
    else scene.detachTransform();
  }, [boxes, selectedId, selectedPsrEditable, rendererReadyVersion, sceneRef]);

  // W/E/R 切 gizmo 模式(仅选中且可编辑时;焦点在输入框时不拦截)。
  useEffect(() => {
    if (!selectedId || !selectedPsrEditable) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      const mode =
        e.key === "w" || e.key === "W"
          ? "translate"
          : e.key === "e" || e.key === "E"
            ? "rotate"
            : e.key === "r" || e.key === "R"
              ? "scale"
              : null;
      if (mode) sceneRef.current?.setTransformMode(mode);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, selectedPsrEditable, sceneRef]);

  return {
    stats: loadState.stats,
    loadError,
    rendererError,
    isLoading: loadState.isLoading,
    loadedPointCloudUrl: loadState.loadedPointCloudUrl,
    rendererStatus,
    retryLoad,
  };
}
