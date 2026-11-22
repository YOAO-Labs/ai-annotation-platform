import { originalOwnerKey } from "@/api/originals";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";
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
  viewportRef: RefObject<HTMLDivElement | null>;
  renderSurface?: HTMLElement | null;
  layoutKey?: string | number;
  getVisibleRegions?: PointCloudVisibleRegions;
  sceneRef: MutableRefObject<PointCloudScene | null>;
  pcdDecimate: number;
  rendererMode?: PointCloudRendererMode;
  pointSize: number;
  showGrid: boolean;
  showAxisGizmo: boolean;
  cameraDamping: number;
  persistCameraView: boolean;
  pointCloudUrl: string | undefined;
  deferPointCloudDisplay?: boolean;
  continuityKey: string | null;
  axisConvention: LidarAxisConvention;
  boxes: SceneBox[];
  measurementPaths?: SceneMeasurementPath[];
  selectedId: string | null;
  selectedPsrEditable: boolean;
  pointcloudCamera: PointcloudCameraState | null;
  onWorkbenchLayoutChange: (patch: WorkbenchLayoutPatch) => void;
  onViewModeChange: (mode: PointCloudViewState["mode"]) => void;
  onTransformCommit: (id: string, psr: BoxPsr) => void;
  onTransformPreview: (id: string, psr: BoxPsr) => void;
}

interface UsePointCloudSceneResult {
  stats: PointCloudStats | null;
  loadError: string | null;
  rendererError: string | null;
  isLoading: boolean;
  loadedPointCloudUrl: string | null;
  rendererStatus: PointCloudRendererStatus | null;
  retryLoad: () => void;
}

const RUNTIME_VIEW_TRANSFER_TTL_MS = 10_000;
const EMPTY_MEASUREMENT_PATHS: SceneMeasurementPath[] = [];
let pendingRuntimeViewTransfer: {
  continuityKey: string;
  owner: string;
  view: PointCloudViewState;
  expiresAt: number;
} | null = null;

function storeRuntimeViewTransfer(continuityKey: string, view: PointCloudViewState, owner: string) {
  if (owner !== originalOwnerKey()) return;
  pendingRuntimeViewTransfer = {
    continuityKey,
    owner,
    view,
    expiresAt: Date.now() + RUNTIME_VIEW_TRANSFER_TTL_MS,
  };
}

function takeRuntimeViewTransfer(
  continuityKey: string | null,
  owner: string,
): PointCloudViewState | null {
  const pending = pendingRuntimeViewTransfer;
  pendingRuntimeViewTransfer = null;
  if (!pending || continuityKey === null) return null;
  if (
    pending.owner !== owner ||
    pending.continuityKey !== continuityKey ||
    pending.expiresAt < Date.now()
  )
    return null;
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

  const originalOwner = useOriginalAssetOwner();
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
  const loadedOwnerRef = useRef<string | null>(null);
  const renderSurfaceRef = useRef(renderSurface);
  renderSurfaceRef.current = renderSurface;
  const visibleRegionsRef = useRef(getVisibleRegions);
  visibleRegionsRef.current = getVisibleRegions;

  const scheduleCameraViewSave = useCallback(
    (view: PointCloudViewState) => {
      if (suspendCameraSaveRef.current || originalOwner !== originalOwnerKey()) return;
      const loadedContinuityKey = loadedContinuityKeyRef.current;
      if (loadedContinuityKey !== null)
        storeRuntimeViewTransfer(loadedContinuityKey, view, originalOwner);
      if (!persistCameraViewRef.current) return;
      pendingCameraViewRef.current = view;
      if (cameraSaveRafRef.current !== null) return;
      cameraSaveRafRef.current = window.requestAnimationFrame(() => {
        if (originalOwner !== originalOwnerKey()) return;
        cameraSaveRafRef.current = null;
        const next = pendingCameraViewRef.current;
        pendingCameraViewRef.current = null;
        if (!next || !persistCameraViewRef.current) return;
        onWorkbenchLayoutChangeRef.current({ pointcloudCamera: next });
      });
    },
    [originalOwner],
  );

  const scheduleCameraViewSaveRef = useRef(scheduleCameraViewSave);
  scheduleCameraViewSaveRef.current = scheduleCameraViewSave;

  useLayoutEffect(() => {
    if (cameraSaveRafRef.current !== null) {
      window.cancelAnimationFrame(cameraSaveRafRef.current);
      cameraSaveRafRef.current = null;
    }
    pendingCameraViewRef.current = null;
    loadedContinuityKeyRef.current = null;
    loadedOwnerRef.current = null;
    suspendCameraSaveRef.current = true;
    loadSequenceRef.current++;
    sceneRef.current?.retirePointCloud();
    sceneRef.current?.setViewChangeHandler(scheduleCameraViewSave);
    cancelActivePointCloudFrameLoad();
    setLoadState({ stats: null, loadedPointCloudUrl: null, isLoading: !!pointCloudUrl });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [originalOwner, sceneRef, scheduleCameraViewSave]);
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
        created.setViewChangeHandler(scheduleCameraViewSaveRef.current);
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
      const loadedOwner = loadedOwnerRef.current;
      if (
        scene &&
        loadedContinuityKey !== null &&
        loadedOwner !== null &&
        !suspendCameraSaveRef.current
      ) {
        storeRuntimeViewTransfer(loadedContinuityKey, scene.getViewState(), loadedOwner);
      }
      ro?.disconnect();
      scene?.dispose();
      if (sceneRef.current === scene) sceneRef.current = null;
    };
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
      continuityKey !== null &&
      loadedContinuityKeyRef.current === continuityKey &&
      loadedOwnerRef.current === originalOwner
        ? scene.getViewState()
        : takeRuntimeViewTransfer(continuityKey, originalOwner);
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
        loadedOwnerRef.current = originalOwner;
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
        storeRuntimeViewTransfer(continuityKey, scene.getViewState(), originalOwner);
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

  useEffect(() => {
    sceneRef.current?.setBoxes(boxes);
  }, [boxes, rendererReadyVersion, sceneRef]);

  useEffect(() => {
    sceneRef.current?.setMeasurementPaths(measurementPaths);
  }, [measurementPaths, rendererReadyVersion, loadState.loadedPointCloudUrl, sceneRef]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (selectedId && selectedPsrEditable) scene.attachTransform(selectedId);
    else scene.detachTransform();
  }, [boxes, selectedId, selectedPsrEditable, rendererReadyVersion, sceneRef]);

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
