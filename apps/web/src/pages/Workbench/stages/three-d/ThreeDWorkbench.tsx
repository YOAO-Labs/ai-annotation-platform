import { cancelPointCloudFramePrefetch } from "./pointCloudAssetCache";
import { originalOwnerKey } from "@/api/originals";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";
import { CopyableError } from "@/components/ui/CopyableError";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { viewportRegionClipPath } from "../../layout/workbenchViewportRegions";
import { createPortal } from "react-dom";
import { useWorkbench3DLayout } from "../../layout/Workbench3DLayoutContext";
import { CameraDockPanel } from "./CameraDockPanel";
import { isWorkbenchInteractionBlocked } from "../../state/workbenchInteractionGuards";

import type {
  CameraPanelState,
  PointcloudCameraState,
  TriViewFloatState,
  WorkbenchCommonPreferences,
  WorkbenchPointcloudPreferences,
} from "@/api/auth";
import type { AnnotationPayload, AnnotationUpdatePayload } from "@/api/tasks";
import {
  useAnnotations,
  useCreateAnnotation,
  useDeleteAnnotation,
  useTask,
  useUpdateAnnotation,
} from "@/hooks/useTasks";
import { useProject } from "@/hooks/useProjects";
import { useProjectAccess } from "@/hooks/useProjectAccess";
import { useFrameNeighbors } from "@/hooks/useFrameNeighbors";
import { useNeighborAnnotations } from "@/hooks/useNeighborAnnotations";
import { useNeighborPointClouds } from "@/hooks/useNeighborPointClouds";
import { useSceneTrajectory } from "@/hooks/useSceneTrajectory";
import { alignPsrToFrame, frameRelMatrix } from "./geometry/egoAlign";
import { cullPointsInBoxes } from "./geometry/cullDynamicPoints";
import {
  alignNeighborPointsPerObject,
  type AlignNeighborBox,
  type AlignPsr,
} from "./geometry/perObjectAlign";
import { useToastStore } from "@/components/ui/Toast";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/Tooltip";
import { useAuthStore } from "@/stores/authStore";
import { classColorForCanvas, displayClassName } from "@/pages/Workbench/stage/colors";
import { buildTrackLabelText, shouldShowLabel } from "@/pages/Workbench/stage/annotationVisual";
import type { ThreeDTool } from "@/pages/Workbench/state/useWorkbenchState";
import type {
  WorkbenchConfigPatch,
  WorkbenchLayoutPatch,
} from "@/pages/Workbench/state/useWorkbenchConfig";
import type { PointMaskGeometry, SensorCalibration } from "@/types";
import type { PointCloudQualityConfig, PointCloudQualityIssue } from "@/api/pointCloudQuality";
import { usePointCloudQualityIssues } from "@/hooks/usePointCloudQuality";
import { useCameraAnnotationMembers } from "@/hooks/useCameraAnnotationMembers";

import { AttributeForm } from "../../shell/AttributeForm";
import type { FloatingPanelBounds } from "../../shell/useDragMove";
import { WORKBENCH_PET_SIZE, type WorkbenchPetDock } from "../../shell/pet/WorkbenchPet";
import { usePointCloudManifest } from "./usePointCloudManifest";
import {
  type BoxPsr,
  type PointCloudScene,
  type PointMaskSelection,
  type SceneBox,
  type SceneMeasurementPath,
  type ReferenceBox,
} from "./PointCloudScene";
import { usePointCloudScene } from "./usePointCloudScene";
import { ContextMenu } from "@/components/ui/ContextMenu";
import { Icon } from "@/components/ui/Icon";
import { useCanvasContextMenu } from "../../stage/useCanvasContextMenu";
import { ClassPickerPopover } from "../../shell/ClassPickerPopover";
import {
  buildThreeDBoxContextMenuItems,
  buildThreeDEmptyContextMenuItems,
} from "./threeDContextMenu";
import { FramePicker, type FramePickerMode } from "./FramePicker";
import CameraProjectionView from "./CameraProjectionView";
import FloatingCameraPanel from "./FloatingCameraPanel";
import TriViewPanel from "./TriViewPanel";
import { PointCloudQualityPanel } from "./PointCloudQualityPanel";
import { SensorCalibrationSheet } from "./SensorCalibrationSheet";
import type { TriSelected } from "./TriOrthoView";
import { TRI_ZOOM_DEFAULT, clampTriZoom, type Psr, type TriView } from "./geometry/triview";
import { psrToCorners } from "./geometry/box3d";
import { cameraAnchor, type Anchor } from "./geometry/cameraAnchor";
import {
  adjustColors,
  isNeutralAdjust,
  type CameraSample,
  type ColorAdjust,
} from "./geometry/colorize";
import { colorizePointsAsync } from "./geometry/pointcloudCompute";
import { retainPointCloudComputeSession } from "./geometry/pointCloudComputeSession";
import {
  prepareCameraTextureResources,
  type CameraTextureResources,
} from "./rendering/cameraTextureResources";
import { projectPoints } from "./geometry/projection";
import type { ScreenPoint } from "./geometry/pointInPolygon";
import {
  formatMeasurementMeters,
  MAX_MEASUREMENT_ANCHORS,
  MAX_MEASUREMENT_PATHS,
  summarizeMeasurement,
  type MeasurementAnchor,
  type MeasurementPath,
} from "./geometry/measurement";
import { fitSize, fitBottom, fitYaw, fitSizeAndBottom, psrFromPoints } from "./geometry/autofit";
import { centralRay, fitCameraSeedBox, type SeedRect } from "./geometry/frustum";
import {
  applyConventionToPsr,
  applyConventionToExtrinsic,
  type LidarAxisConvention,
  unapplyConventionToPsr,
} from "./geometry/axisConvention";
import { box3dAttributeSchema } from "./geometry/box3dAttributes";
import { pasteOffsetPayload, serializeBox3D, type ClipboardBox3D } from "./geometry/box3dClipboard";
import { useThreeDHistory } from "./useThreeDHistory";
import {
  buildPointcloudLegacyMigration,
  finishPointcloudLegacyMigration,
  isCrossFrameOverlayK,
  isPointMaskSelectMode,
} from "./pointcloudPreferenceStorage";
import { usePsrFloatingPanel } from "./usePsrFloatingPanel";
import { usePsrPatchPipeline } from "./usePsrPatchPipeline";
import { useCameraPanels } from "./useCameraPanels";
import { resolveWorkbenchPerformanceTier } from "../../state/performanceTier";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { SceneTimeline } from "./SceneTimeline";
import { markPointCloudPaint } from "./pointCloudTiming";
import {
  pointCloudRendererModeFromExperiment,
  readPointCloudWebGpuExperiment,
  type PointCloudRendererStatus,
} from "./rendering/pointCloudRenderer";

const ROOT = "flex flex-col size-full min-h-0 bg-transparent";
const VIEWPORT_WRAP = "relative flex-1 min-h-0";
const VIEWPORT = "absolute inset-0";
const PLACING = "cursor-crosshair";
const CREATION_BLOCKED = "cursor-wait";
const BOX_SELECT_RECT =
  "absolute left-[var(--rect-l)] top-[var(--rect-t)] w-[var(--rect-w)] h-[var(--rect-h)] z-local-3 pointer-events-none border border-brand bg-brand/10 opacity-50";
const POINT_MASK_PATH_PREVIEW = "absolute inset-0 z-local-3 size-full pointer-events-none";
const CONTROLS =
  "absolute top-3 left-3 z-local-4 flex max-w-[calc(100%-24px)] flex-col items-start gap-1.5 pointer-events-none";
const CONTROL_BAR =
  "flex h-8 items-center gap-0.5 max-w-full px-0.5 rounded-md bg-card border border-border pointer-events-auto";
const BTN =
  "appearance-none px-2.5 py-1 rounded-sm border border-border bg-background text-foreground cursor-pointer text-sm hover:border-brand hover:text-brand disabled:text-muted-foreground/65 disabled:cursor-not-allowed disabled:opacity-65";
const BTN_ACTIVE = "!border-brand !bg-brand/10 !text-brand";
const SIZE_CTL = "flex items-center gap-1.5 text-xs text-muted-foreground";
const SELECT_CTL =
  "appearance-none min-w-[84px] px-1.5 py-1 rounded-sm border border-border bg-background text-foreground text-xs";
const FIT_GROUP =
  "grid grid-cols-2 items-center gap-1.5 py-1.5 border-y border-border [&_button]:w-full [&_button]:px-1.5 [&_button]:py-1 [&_button]:text-xs";
const STATUS_BAR =
  "absolute bottom-3 left-3.5 flex flex-wrap gap-2 max-w-[min(420px,calc(100%-28px))] px-2.5 py-1 rounded-sm bg-card border border-border text-xs text-muted-foreground";
const MEASUREMENT_PANEL =
  "absolute top-3 right-3 z-local-4 flex w-[288px] max-h-[calc(100%-72px)] flex-col gap-2 overflow-auto rounded-md border border-border bg-card p-2.5 text-xs text-foreground shadow-sm";
const MEASUREMENT_ROW =
  "flex items-start justify-between gap-2 rounded-sm border border-border p-2";
const MEASUREMENT_VALUE_GRID = "grid grid-cols-2 gap-x-3 gap-y-1 text-muted-foreground";
const MEASUREMENT_ACTIONS = "flex shrink-0 items-center gap-1";
const ERR = "text-status-danger";
const MISMATCH_BANNER =
  "absolute top-[calc(var(--top-toolbar-height)+24px)] left-3 z-local-4 flex flex-wrap items-center gap-2 max-w-[min(640px,calc(100%-24px))] px-2.5 py-1.5 text-status-caution text-xs bg-card border border-amber-600 dark:border-amber-400 rounded-md shadow-sm";
const EDIT_PANEL_SURFACE =
  "w-[210px] flex flex-col gap-1.5 p-2.5 rounded-md bg-card border shadow-sm text-xs text-foreground";
const EDIT_PANEL_FLOATING =
  "absolute top-3 right-3 translate-x-[var(--psr-dx)] translate-y-[var(--psr-dy)] border-border";
const EDIT_PANEL_PET_LINKED =
  "fixed left-[var(--psr-pet-x)] top-[var(--psr-pet-y)] z-overlay-high -translate-x-1/2 -translate-y-full border-brand shadow-md";
const EDIT_PANEL_DRAGGING = "select-none";
const EDIT_HEADER = "flex flex-col gap-1 cursor-grab";
const DRAG_HINT = "shrink-0 text-muted-foreground";
const ICON_BTN =
  "shrink-0 inline-flex items-center justify-center px-1 py-0.5 rounded-sm border border-border bg-background text-muted-foreground cursor-pointer hover:border-brand hover:text-brand";
const EDIT_SUMMARY = "text-xs text-muted-foreground";
const EDIT_BODY = "flex flex-col gap-1.5";
const EDIT_TITLE = "flex items-center justify-between gap-1.5 text-sm font-semibold";
const CLASS_SELECT =
  "appearance-none flex-1 min-w-0 px-1.5 py-0.5 rounded-sm border border-border bg-background text-foreground text-xs";
const LOCK_BTN =
  "appearance-none shrink-0 px-2 py-0.5 rounded-sm border border-border bg-background text-muted-foreground cursor-pointer text-xs hover:border-brand hover:text-brand";
const LOCK_BTN_ON = "!border-brand !text-brand";
const EDIT_GROUP_LABEL = "mt-1 text-muted-foreground";
const EDIT_GROUP_LABEL_ROW = "flex items-center justify-between gap-1.5";
const RESET_BTN =
  "appearance-none shrink-0 px-2 py-px rounded-sm border border-border bg-background text-muted-foreground cursor-pointer text-xs hover:border-brand hover:text-brand";
const EDIT_ROW =
  "flex gap-1.5 [&_input]:flex-1 [&_input]:min-w-0 [&_input]:px-1.5 [&_input]:py-1 [&_input]:rounded-sm [&_input]:border [&_input]:border-border [&_input]:bg-background [&_input]:text-foreground [&_input]:text-xs";
const DELETE_BTN =
  "appearance-none mt-1.5 px-2.5 py-1 rounded-sm border border-rose-600 dark:border-rose-400 bg-transparent text-status-danger cursor-pointer text-xs hover:bg-rose-600 dark:hover:bg-rose-400 hover:text-white dark:hover:text-white";
const QUALITY_PANEL_WIDTH = 360;
const CAMERA_LAYER = "absolute inset-0 z-local-3 pointer-events-none";
const CAM_GROUP =
  "absolute flex gap-2.5 max-h-[calc(100%-var(--top-toolbar-height)-48px)] overflow-visible pointer-events-none [&>*]:pointer-events-auto";
const CAM_MODAL =
  "fixed left-[var(--camera-edit-left)] top-[var(--camera-edit-top)] w-[var(--camera-edit-width)] h-[var(--camera-edit-height)] z-workbench-modal flex items-center justify-center bg-black/70";
const CAM_MODAL_BODY =
  "relative w-fit max-w-[calc(100%-24px)] p-3 rounded-md border border-border bg-card shadow-sm";
const CAM_MODAL_CLOSE =
  "absolute top-4 right-4 z-local-1 appearance-none px-2.5 py-1 rounded-sm border border-border bg-background text-foreground cursor-pointer text-xs hover:border-brand hover:text-brand";
const CAM_MODAL_TOOL_GROUP = "absolute top-4 left-4 z-local-1 flex items-center gap-2";
const CAM_MODAL_SEED =
  "appearance-none px-2.5 py-1 rounded-sm border border-border bg-background text-foreground cursor-pointer text-xs hover:border-brand hover:text-brand";
const CAM_MODAL_SEED_ACTIVE = "!border-brand !bg-brand/10 !text-brand";
const CAM_MODAL_MEMBER_BAR =
  "absolute bottom-9 left-4 right-4 z-local-1 flex flex-wrap items-center justify-center gap-2 pointer-events-none";
const CAM_MODAL_MEMBER_CARD =
  "flex flex-wrap items-center gap-2 rounded-md border border-border bg-card/95 px-2.5 py-1.5 text-xs text-foreground shadow-sm pointer-events-auto";
const CAM_MODAL_SWITCH =
  "absolute top-1/2 z-local-1 w-9 h-12 -translate-y-1/2 rounded-md border border-border bg-background text-foreground cursor-pointer text-control-xl leading-none hover:border-brand hover:text-brand";
const CAM_MODAL_PREV = "left-4";
const CAM_MODAL_NEXT = "right-4";
import {
  ANCHOR_CLASS,
  CAMERA_STACK_VISIBLE,
  IDENTITY_MATRIX,
  LIDAR_TOOL_UNIT,
  POINT_MASK_TOOL_UNIT,
  PSR_GROUPS,
  SEED_FALLBACK_RANGE_M,
  boxGeometryFromPsr,
  frontCameraForward,
  enlargedCameraNavigationDirection,
  geometryConvention,
  isPsrFieldBad,
  loadCameraSample,
  psrToForm,
  resolveBox3dDefaultSize,
  sortedIndices,
  type PsrField,
} from "./ThreeDWorkbench.helpers";

const DEFAULT_TRI_ZOOM_BY_VIEW: Record<TriView, number> = {
  top: TRI_ZOOM_DEFAULT,
  side: TRI_ZOOM_DEFAULT,
  front: TRI_ZOOM_DEFAULT,
};

function rendererBackendLabel(status: PointCloudRendererStatus | null): string {
  if (!status) return "Renderer 初始化…";
  if (status.actualBackend === "webgpu") return "WebGPU";
  if (status.actualBackend === "webgl2-fallback") return "WebGL2 fallback";
  return "Legacy WebGL2";
}

interface ThreeDWorkbenchProps {
  taskId: string | null;
  readOnly?: boolean;
  playbackActive?: boolean;
  onPlaybackActiveChange?: (active: boolean) => void;
  playbackBlockedReason?: string | null;
  selectedId: string | null;
  selectedIds: string[];
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  activeClass: string;
  threeDTool: ThreeDTool;
  onSetThreeDTool: (t: ThreeDTool) => void;
  onCrossFramePropagate: (direction: "next" | "prev") => void;
  onCrossFramePropagateBatch: (direction: "next" | "prev") => void;
  onCrossFramePropagateToTask: (targetTaskId: string, targetFrameIndex: number) => void;
  onCrossFrameInterpolate: (trackId: string, toTaskId: string) => void;
  onNavigateSceneFrame: (targetTaskId: string) => Promise<boolean>;
  rightSidebarOpen: boolean;
  rightSidebarWidth: number;
  triViewFloat: TriViewFloatState;
  cameraPanels: Record<string, CameraPanelState>;
  pointcloudCamera: PointcloudCameraState | null;
  onWorkbenchLayoutChange: (patch: WorkbenchLayoutPatch) => void;
  workbenchCommon: WorkbenchCommonPreferences;
  workbenchPointcloud: WorkbenchPointcloudPreferences;
  workbenchConfigLoaded: boolean;
  onWorkbenchConfigChange: (patch: WorkbenchConfigPatch) => void;
  onWorkbenchConfigUpdate: (patch: WorkbenchConfigPatch) => Promise<void>;
  box3dDefaultSize?: [number, number, number] | null;
  petDock?: WorkbenchPetDock;
}

export function ThreeDWorkbench({
  taskId,
  readOnly: taskReadOnly = false,
  playbackActive = false,
  onPlaybackActiveChange,
  playbackBlockedReason: shellPlaybackBlockedReason,
  selectedId,
  selectedIds,
  onSelectBox,
  activeClass,
  threeDTool,
  onSetThreeDTool,
  onCrossFramePropagate,
  onCrossFramePropagateBatch,
  onCrossFramePropagateToTask,
  onCrossFrameInterpolate,
  onNavigateSceneFrame,
  cameraPanels,
  pointcloudCamera,
  onWorkbenchLayoutChange,
  workbenchCommon,
  workbenchPointcloud,
  workbenchConfigLoaded,
  onWorkbenchConfigChange,
  onWorkbenchConfigUpdate,
  box3dDefaultSize,
  petDock,
}: ThreeDWorkbenchProps) {
  const readOnly = taskReadOnly || playbackActive;
  const writeAllowedRef = useRef(!readOnly);
  writeAllowedRef.current = !readOnly;
  useEffect(() => {
    if (!readOnly) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target?.closest(
          'input, textarea, select, [contenteditable="true"], [role="dialog"], [role="menu"], [role="tab"], [data-workbench-layout-control], [data-scene-timeline]',
        )
      )
        return;
      const key = event.key.toLowerCase();
      const editing =
        event.ctrlKey || event.metaKey
          ? ["z", "y", "v", "d", "enter"].includes(key)
          : [
              "b",
              "p",
              "w",
              "e",
              "r",
              "q",
              "l",
              "h",
              "enter",
              "delete",
              "backspace",
              "arrowleft",
              "arrowright",
              "arrowup",
              "arrowdown",
            ].includes(key);
      if (!editing) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (playbackActive) onPlaybackActiveChange?.(false);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onPlaybackActiveChange, playbackActive, readOnly]);
  const dockLayout = useWorkbench3DLayout();
  const [cameraResetKey, setCameraResetKey] = useState(0);
  const originalOwner = useOriginalAssetOwner();
  useEffect(() => {
    const release = retainPointCloudComputeSession();
    return () => {
      cancelPointCloudFramePrefetch();
      release();
    };
  }, [originalOwner]);
  const manifestQuery = usePointCloudManifest(taskId, true);
  const { data: manifest, isLoading, error } = manifestQuery;
  const retainedManifestRef = useRef({ owner: originalOwner, manifest });
  if (retainedManifestRef.current.owner !== originalOwner)
    retainedManifestRef.current = { owner: originalOwner, manifest: undefined };
  if (manifest) retainedManifestRef.current = { owner: originalOwner, manifest };
  const displayManifest = manifest ?? retainedManifestRef.current.manifest;
  const pushToast = useToastStore((st) => st.push);
  const userId = useAuthStore((s) => s.user?.id ?? null);
  const userRole = useAuthStore((s) => s.user?.role ?? null);
  const axisConvention: LidarAxisConvention = manifest?.axis_convention ?? "iso_8855";
  useEffect(() => {
    if (manifest?.scene_id) {
      console.debug("[3D] scene info", {
        scene_id: manifest.scene_id,
        scene_name: manifest.scene_name,
        frame_index: manifest.frame_index,
        scene_total_frames: manifest.scene_total_frames,
      });
    }
  }, [
    manifest?.scene_id,
    manifest?.scene_name,
    manifest?.frame_index,
    manifest?.scene_total_frames,
  ]);
  const axisConventionRef = useRef<LidarAxisConvention>(axisConvention);
  axisConventionRef.current = axisConvention;
  const viewportWrapRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const controlsRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<PointCloudScene | null>(null);
  const [rendererMode] = useState(() =>
    pointCloudRendererModeFromExperiment(readPointCloudWebGpuExperiment()),
  );
  const performanceConfig = useMemo(
    () => resolveWorkbenchPerformanceTier(workbenchCommon.performanceTier),
    [workbenchCommon.performanceTier],
  );
  const defaultBoxSize = useMemo(
    () => resolveBox3dDefaultSize(box3dDefaultSize),
    [box3dDefaultSize],
  );
  const [triFloatBounds, setTriFloatBounds] = useState<FloatingPanelBounds | null>(null);
  const pointSize = workbenchPointcloud.pointSize;
  const colorizeOn = workbenchPointcloud.colorizeWithCamera;
  const colorAdjust: ColorAdjust = useMemo(
    () => ({
      contrast: workbenchPointcloud.colorizeContrast,
      brightness: workbenchPointcloud.colorizeBrightness,
      gamma: workbenchPointcloud.colorizeGamma,
    }),
    [
      workbenchPointcloud.colorizeBrightness,
      workbenchPointcloud.colorizeContrast,
      workbenchPointcloud.colorizeGamma,
    ],
  );
  const depthOn = workbenchPointcloud.showDepthHint;
  const pointMaskSelectMode = isPointMaskSelectMode(workbenchPointcloud.pointMaskSelectMode)
    ? workbenchPointcloud.pointMaskSelectMode
    : "rect";
  const overlayEnabled =
    workbenchCommon.crossFrameOverlayEnabled ?? workbenchCommon.crossFrameOverlayK > 0;
  const overlayFrameK = isCrossFrameOverlayK(workbenchCommon.crossFrameOverlayK)
    ? Math.max(workbenchCommon.crossFrameOverlayK, 1)
    : 1;
  const overlayK = overlayEnabled ? overlayFrameK : 0;
  const overlayScope = workbenchCommon.crossFrameOverlayScope === "all" ? "all" : "selected";
  const pointOverlayOn = workbenchPointcloud.neighborPointOverlay;
  const pointOverlayFrameK =
    workbenchPointcloud.neighborPointOverlayK === 2 ||
    workbenchPointcloud.neighborPointOverlayK === 3
      ? workbenchPointcloud.neighborPointOverlayK
      : 1;
  const pointOverlayK = pointOverlayOn ? pointOverlayFrameK : 0;
  const neighborPointCull: "keep" | "cull" | "align" =
    workbenchPointcloud.neighborPointCull === "cull"
      ? "cull"
      : workbenchPointcloud.neighborPointCull === "align"
        ? "align"
        : "keep";
  const [neighborCulledCount, setNeighborCulledCount] = useState(0);
  const [neighborMovedCount, setNeighborMovedCount] = useState(0);
  const [pointCloudViewMode, setPointCloudViewMode] = useState<"orbit" | "bev">("orbit");
  const [colorizing, setColorizing] = useState(false);
  const [, setCameraTextureResources] = useState<CameraTextureResources | null>(null);
  const colorizedRawRef = useRef<Float32Array | null>(null);
  const adjustedColorBufferRef = useRef<Float32Array | null>(null);
  const [enlargedRole, setEnlargedRole] = useState<string | null>(null);
  const [calibrationSheetOpen, setCalibrationSheetOpen] = useState(false);
  const [seedMode, setSeedMode] = useState(false);
  const [manualBboxMode, setManualBboxMode] = useState(false);
  const [boxCreationSaving, setBoxCreationSaving] = useState(false);
  const [boxCreationIssue, setBoxCreationIssue] = useState<string | null>(null);
  const boxCreationInFlightRef = useRef(false);
  const boxCreationRevisionRef = useRef(0);
  const taskIdRef = useRef(taskId);
  taskIdRef.current = taskId;
  const [pointMaskPolygonPoints, setPointMaskPolygonPoints] = useState<ScreenPoint[]>([]);
  const [pointMaskCursor, setPointMaskCursor] = useState<ScreenPoint | null>(null);
  const finishPointMaskPolygonRef = useRef<(subtract: boolean) => void>(() => undefined);
  const [measurementDraft, setMeasurementDraft] = useState<MeasurementAnchor[]>([]);
  const [completedMeasurements, setCompletedMeasurements] = useState<MeasurementPath[]>([]);
  const measurementIdRef = useRef(0);

  const annotationsQuery = useAnnotations(taskId ?? undefined);
  const { data: annotations } = annotationsQuery;
  const updateAnnotation = useUpdateAnnotation(taskId ?? undefined);
  const deleteAnnotation = useDeleteAnnotation(taskId ?? undefined);
  const createAnnotation = useCreateAnnotation(taskId ?? undefined);
  const history = useThreeDHistory(taskId, {
    createAnnotation,
    deleteAnnotation,
    updateAnnotation,
  });
  const clipboardRef = useRef<ClipboardBox3D | null>(null);
  const pasteCountRef = useRef(0);
  const annotationsRef = useRef<typeof annotations>(annotations);
  annotationsRef.current = annotations;
  const commitHistoryRef = useRef(history.commit);
  commitHistoryRef.current = history.commit;

  const { data: task } = useTask(taskId ?? "");
  const { data: project } = useProject(task?.project_id ?? "");
  const projectAccess = useProjectAccess(task?.project_id ?? undefined);
  const qualityAllowed = projectAccess.isManager || projectAccess.hasCapability("review.write");
  const qualityCanScanScene = userRole === "super_admin" || project?.owner_id === userId;
  const canManageCalibration = userRole === "super_admin" || project?.owner_id === userId;
  const [qualityPanelOpen, setQualityPanelOpen] = useState(false);
  const qualitySafeFloatBounds = useMemo(() => {
    if (!triFloatBounds || !qualityPanelOpen) return triFloatBounds;
    const margin = triFloatBounds.margin ?? 0;
    return {
      ...triFloatBounds,
      right: Math.max(
        triFloatBounds.left + 200 + margin * 2,
        triFloatBounds.right - QUALITY_PANEL_WIDTH,
      ),
    };
  }, [qualityPanelOpen, triFloatBounds]);
  const qualityIssuesQuery = usePointCloudQualityIssues({
    projectId: qualityAllowed ? (task?.project_id ?? "") : "",
    sceneId: manifest?.scene_id ?? undefined,
    status: "open",
  });
  const qualityMarkers = useMemo(() => {
    const rank = { blocker: 0, warning: 1, info: 2 } as const;
    const markers: Record<number, "blocker" | "warning" | "info"> = {};
    for (const issue of qualityIssuesQuery.data?.items ?? []) {
      if (issue.frame_start == null) continue;
      const end = issue.frame_end ?? issue.frame_start;
      for (let frame = issue.frame_start; frame <= end; frame += 1) {
        const current = markers[frame];
        if (!current || rank[issue.severity] < rank[current]) markers[frame] = issue.severity;
      }
    }
    return markers;
  }, [qualityIssuesQuery.data?.items]);
  const toolBindings = project?.tool_bindings;
  const hasToolBindings = !!toolBindings && Object.keys(toolBindings).length > 0;
  const boxClasses = useMemo(() => {
    const binding = toolBindings?.[LIDAR_TOOL_UNIT];
    if (hasToolBindings && !binding?.enabled) return [];
    return (binding?.classes ?? []).map((c) => c.name);
  }, [hasToolBindings, toolBindings]);
  const pointMaskOwnClasses = useMemo(() => {
    const binding = toolBindings?.[POINT_MASK_TOOL_UNIT];
    if (hasToolBindings && !binding?.enabled) return [];
    return (binding?.classes ?? []).map((c) => c.name);
  }, [hasToolBindings, toolBindings]);
  const pointMaskClasses = pointMaskOwnClasses.length > 0 ? pointMaskOwnClasses : boxClasses;
  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const canPlaceBox = !readOnly && boxClasses.length > 0;
  const canPlacePointMask = !readOnly && pointMaskClasses.length > 0;
  const placing = threeDTool === "box" && canPlaceBox;
  const pointMasking = threeDTool === "point-mask" && canPlacePointMask;
  const measuring = threeDTool === "measure";
  const pointMaskPolygonMode = pointMasking && pointMaskSelectMode === "polygon";
  const pointMaskDragMode = pointMasking && pointMaskSelectMode !== "polygon";
  const drawingSelection = placing || pointMasking;
  const boxPlaceClass =
    activeClass && boxClasses.includes(activeClass) ? activeClass : (boxClasses[0] ?? null);
  const pointMaskPlaceClass =
    activeClass && pointMaskClasses.includes(activeClass)
      ? activeClass
      : (pointMaskClasses[0] ?? null);
  const {
    autoCollapseCameras,
    handleCameraPanelPosition,
    handleCameraPanelCollapsed,
    handleResetCameraPanels,
  } = useCameraPanels({ cameraPanels, onWorkbenchLayoutChange, viewportWrapRef });
  const registerLayoutActions = dockLayout?.registerActions;
  const cameraPresentation = dockLayout?.cameraPresentation ?? "floating";
  useEffect(() => {
    registerLayoutActions?.({
      resetCameras: () => {
        if (cameraPresentation === "floating") handleResetCameraPanels();
        else setCameraResetKey((value) => value + 1);
      },
    });
    return () => registerLayoutActions?.(null);
  }, [cameraPresentation, handleResetCameraPanels, registerLayoutActions]);

  useLayoutEffect(() => {
    const controls = controlsRef.current;
    const viewport = viewportWrapRef.current;
    if (!controls || !viewport) return;
    const setToolbarHeight = (height: number) => {
      viewport.style.setProperty("--top-toolbar-height", `${Math.round(height)}px`);
    };
    setToolbarHeight(controls.getBoundingClientRect().height);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      setToolbarHeight(entry.contentRect.height);
    });
    observer.observe(controls);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const viewport = viewportWrapRef.current;
    if (!viewport) return;
    const syncBounds = () => {
      const rect = viewport.getBoundingClientRect();
      const next: FloatingPanelBounds = {
        left: rect.left,
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
        margin: 12,
      };
      setTriFloatBounds((prev) =>
        prev &&
        prev.left === next.left &&
        prev.top === next.top &&
        prev.right === next.right &&
        prev.bottom === next.bottom &&
        prev.margin === next.margin
          ? prev
          : next,
      );
    };
    syncBounds();
    window.addEventListener("resize", syncBounds);
    if (typeof ResizeObserver === "undefined") {
      return () => window.removeEventListener("resize", syncBounds);
    }
    const observer = new ResizeObserver(syncBounds);
    observer.observe(viewport);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", syncBounds);
    };
  }, [dockLayout?.layoutKey]);

  useEffect(() => {
    if (!workbenchConfigLoaded || !userId || typeof window === "undefined") return;
    let cancelled = false;
    let migration;
    try {
      migration = buildPointcloudLegacyMigration(userId, window.localStorage);
    } catch {
      return;
    }
    if (!migration) return;
    if (!migration.patch) {
      finishPointcloudLegacyMigration(migration, window.localStorage);
      return;
    }
    void onWorkbenchConfigUpdate(migration.patch)
      .then(() => {
        if (!cancelled) finishPointcloudLegacyMigration(migration, window.localStorage);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [workbenchConfigLoaded, userId, onWorkbenchConfigUpdate]);

  useEffect(() => {
    if (pointMasking) return;
    setPointMaskPolygonPoints([]);
    setPointMaskCursor(null);
  }, [pointMasking]);

  const [form, setForm] = useState<Record<PsrField, string> | null>(null);

  const [draftPsr, setDraftPsr] = useState<{
    id: string;
    psr: Psr;
    source: "gizmo" | "triview";
  } | null>(null);
  const sceneDraftPsr = draftPsr?.source === "triview" ? draftPsr : null;

  const sceneBoxes = useMemo<SceneBox[]>(() => {
    const trackContent = workbenchCommon.labelContent.track;
    const labelVisibility = workbenchCommon.labelVisibility;
    const trackNumbers = new Map<string, number>();
    Array.from(
      new Set(
        (annotations ?? [])
          .filter((a) => !a.is_hidden && a.geometry?.type === "box_3d" && a.track_id)
          .map((a) => a.track_id as string),
      ),
    )
      .sort((x, y) => x.localeCompare(y))
      .forEach((tid, i) => trackNumbers.set(tid, i + 1));

    const list: SceneBox[] = [];
    for (const a of annotations ?? []) {
      if (a.is_hidden) continue;
      const g = a.geometry as {
        type?: string;
        center?: number[];
        size?: number[];
        rotation?: number[];
      };
      if (g?.type !== "box_3d" || !g.center || !g.size || !g.rotation) continue;
      const dp = sceneDraftPsr && sceneDraftPsr.id === a.id ? sceneDraftPsr.psr : null;
      const selected = selectedIdSet.has(a.id);
      const label = shouldShowLabel(selected, labelVisibility)
        ? buildTrackLabelText(
            {
              className: displayClassName(a.class_name),
              trackNumber: a.track_id ? trackNumbers.get(a.track_id) : undefined,
              attributes: (a as { attributes?: Record<string, unknown> | null }).attributes ?? null,
            },
            trackContent,
          )
        : undefined;
      list.push({
        id: a.id,
        center: dp
          ? [dp.center[0], dp.center[1], dp.center[2]]
          : (g.center as [number, number, number]),
        size: dp
          ? [Math.abs(dp.size[0]), Math.abs(dp.size[1]), Math.abs(dp.size[2])]
          : (g.size.map((v) => Math.abs(v)) as [number, number, number]),
        rotation: dp
          ? [dp.rotation[0], dp.rotation[1], dp.rotation[2]]
          : (g.rotation as [number, number, number]),
        color: classColorForCanvas(a.class_name),
        selected,
        label,
      });
    }
    return list;
  }, [
    annotations,
    selectedIdSet,
    sceneDraftPsr,
    workbenchCommon.labelContent.track,
    workbenchCommon.labelVisibility,
  ]);

  const boxes = useMemo<SceneBox[]>(() => {
    if (!draftPsr || draftPsr.source !== "gizmo") return sceneBoxes;
    return sceneBoxes.map((box) =>
      box.id === draftPsr.id
        ? {
            ...box,
            center: [draftPsr.psr.center[0], draftPsr.psr.center[1], draftPsr.psr.center[2]],
            size: [
              Math.abs(draftPsr.psr.size[0]),
              Math.abs(draftPsr.psr.size[1]),
              Math.abs(draftPsr.psr.size[2]),
            ],
            rotation: [
              draftPsr.psr.rotation[0],
              draftPsr.psr.rotation[1],
              draftPsr.psr.rotation[2],
            ],
          }
        : box,
    );
  }, [draftPsr, sceneBoxes]);

  const selectedBox = boxes.find((b) => b.id === selectedId) ?? null;
  const selectedAnn = (annotations ?? []).find((a) => a.id === selectedId) ?? null;
  const cameraMembers = useCameraAnnotationMembers(
    taskId,
    selectedAnn?.scene_track_id ?? null,
    selectedAnn?.version ?? null,
    enlargedRole,
  );
  const pendingTimelineSelectionRef = useRef<{
    taskId: string;
    annotationId: string | null;
  } | null>(null);
  const pendingQualityLocatorRef = useRef<PointCloudQualityIssue | null>(null);
  const handleTimelineNavigate = useCallback(
    async (targetTaskId: string, annotationId: string | null) => {
      pendingTimelineSelectionRef.current = { taskId: targetTaskId, annotationId };
      const allowed = await onNavigateSceneFrame(targetTaskId);
      if (!allowed) pendingTimelineSelectionRef.current = null;
      return allowed;
    },
    [onNavigateSceneFrame],
  );
  const applyQualityLocatorView = useCallback(
    (issue: PointCloudQualityIssue) => {
      const groundZ = issue.metric.ground_z;
      sceneRef.current?.setQualityGroundPlane(
        issue.locator.auxiliary_layers.includes("ground") && typeof groundZ === "number"
          ? groundZ
          : null,
      );
      if (issue.locator.auxiliary_layers.includes("neighbor_frames")) {
        onWorkbenchConfigChange({
          common: { crossFrameOverlayEnabled: true, crossFrameOverlayK: 1 },
        });
      }
      if (issue.locator.camera) setEnlargedRole(issue.locator.camera);
    },
    [onWorkbenchConfigChange],
  );
  const handleLocateQualityIssue = useCallback(
    (issue: PointCloudQualityIssue) => {
      if (!issue.locator.task_id) {
        pushToast({ msg: "该问题缺少可访问的任务定位器", kind: "warning" });
        return;
      }
      if (issue.locator.task_id === taskId) {
        applyQualityLocatorView(issue);
        onSelectBox(issue.locator.annotation_id);
        return;
      }
      pendingQualityLocatorRef.current = issue;
      void handleTimelineNavigate(issue.locator.task_id, issue.locator.annotation_id).then(
        (allowed) => {
          if (!allowed && pendingQualityLocatorRef.current?.id === issue.id) {
            pendingQualityLocatorRef.current = null;
          }
        },
      );
    },
    [applyQualityLocatorView, handleTimelineNavigate, onSelectBox, pushToast, taskId],
  );
  useEffect(() => {
    const pending = pendingTimelineSelectionRef.current;
    if (!pending || pending.taskId !== taskId || !annotations) return;
    pendingTimelineSelectionRef.current = null;
    if (
      pending.annotationId &&
      annotations.some((annotation) => annotation.id === pending.annotationId)
    ) {
      onSelectBox(pending.annotationId);
    }
    const qualityIssue = pendingQualityLocatorRef.current;
    if (qualityIssue?.locator.task_id === taskId) {
      pendingQualityLocatorRef.current = null;
      applyQualityLocatorView(qualityIssue);
    }
  }, [annotations, applyQualityLocatorView, onSelectBox, taskId]);
  const selectedBoxIds = useMemo(
    () => selectedIds.filter((id) => boxes.some((b) => b.id === id)),
    [selectedIds, boxes],
  );
  const multiBoxSelected = selectedBoxIds.length > 1;
  const boxAttributeSchema = useMemo(() => box3dAttributeSchema(toolBindings), [toolBindings]);
  const selectedAnnRef = useRef(selectedAnn);
  selectedAnnRef.current = selectedAnn;
  const selectedClass = selectedAnn?.class_name ?? null;
  const pointMasks = useMemo(
    () => (annotations ?? []).filter((a) => !a.is_hidden && a.geometry?.type === "point_mask_3d"),
    [annotations],
  );
  const selectedPointMask =
    !selectedAnn?.is_hidden && selectedAnn?.geometry?.type === "point_mask_3d"
      ? selectedAnn.geometry
      : null;
  const conventionMismatches = useMemo(() => {
    const mismatches = [];
    for (const ann of annotations ?? []) {
      const g = ann.geometry;
      if (!g || (g.type !== "box_3d" && g.type !== "point_mask_3d")) continue;
      const created = (g as { convention_at_create?: LidarAxisConvention | null })
        .convention_at_create;
      if (created && created !== axisConvention) mismatches.push({ ann, convention: created });
    }
    return mismatches;
  }, [annotations, axisConvention]);
  const selectedConventionMismatch =
    selectedAnn?.geometry?.type === "box_3d" &&
    selectedAnn.geometry.convention_at_create &&
    selectedAnn.geometry.convention_at_create !== axisConvention
      ? selectedAnn.geometry.convention_at_create
      : null;
  const selectedLocked = !!selectedAnn?.is_locked;
  const selectedHidden = !!selectedAnn?.is_hidden;
  const selectedEditable = !readOnly && !selectedLocked;
  const selectedPsrEditable = selectedEditable && !multiBoxSelected;
  const selectedPointMaskEditable = selectedEditable && !!selectedPointMask && !multiBoxSelected;

  const handleTransformPreview = useCallback((id: string, psr: BoxPsr) => {
    if (!writeAllowedRef.current) return;
    setForm(psrToForm(psr));
    setDraftPsr({ id, psr, source: "gizmo" });
  }, []);

  const handleTransformCommit = useCallback((id: string, psr: BoxPsr) => {
    if (!writeAllowedRef.current) return;
    setDraftPsr(null);
    setForm(psrToForm(psr));
    const ann = annotationsRef.current?.find((a) => a.id === id);
    const geometry = boxGeometryFromPsr(
      psr,
      geometryConvention(ann?.geometry, axisConventionRef.current),
    );
    if (ann?.geometry?.type === "box_3d") {
      void commitHistoryRef.current([
        {
          kind: "update",
          annotationId: id,
          before: { geometry: ann.geometry },
          after: { geometry },
        },
      ]);
    }
  }, []);

  const measurementScenePaths = useMemo<SceneMeasurementPath[]>(() => {
    const completed = completedMeasurements
      .filter((measurement) => measurement.visible)
      .map((measurement) => ({
        id: measurement.id,
        positions: measurement.anchors.map((anchor) => anchor.position),
        active: false,
      }));
    if (measurementDraft.length === 0) return completed;
    return [
      ...completed,
      {
        id: "measurement-draft",
        positions: measurementDraft.map((anchor) => anchor.position),
        active: true,
      },
    ];
  }, [completedMeasurements, measurementDraft]);

  const {
    stats,
    loadError,
    rendererError,
    isLoading: pointCloudLoading,
    loadedPointCloudUrl,
    rendererStatus,
    retryLoad,
  } = usePointCloudScene({
    renderSurface: dockLayout?.renderSurface,
    layoutKey: dockLayout?.layoutKey,
    getVisibleRegions: dockLayout?.getVisibleRegions,
    viewportRef,
    sceneRef,
    pcdDecimate: performanceConfig.pcdDecimate,
    rendererMode,
    pointSize,
    showGrid: workbenchPointcloud.showGrid,
    showAxisGizmo: workbenchPointcloud.showAxisGizmo,
    cameraDamping: workbenchPointcloud.cameraDamping,
    persistCameraView: workbenchPointcloud.persistCameraView,
    pointCloudUrl: displayManifest?.point_cloud_url ?? undefined,
    deferPointCloudDisplay: false,
    continuityKey: displayManifest?.scene_id ?? displayManifest?.task_id ?? taskId,
    axisConvention: displayManifest?.axis_convention ?? axisConvention,
    boxes: sceneBoxes,
    measurementPaths: measurementScenePaths,
    selectedId,
    selectedPsrEditable,
    pointcloudCamera,
    onWorkbenchLayoutChange,
    onViewModeChange: setPointCloudViewMode,
    onTransformPreview: handleTransformPreview,
    onTransformCommit: handleTransformCommit,
  });

  useEffect(() => {
    if (readOnly) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.repeat) return;
      if (!e.shiftKey || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      e.preventDefault();
      e.stopPropagation();
      const dir = e.key === "ArrowRight" ? "next" : "prev";
      if (e.ctrlKey || e.metaKey) {
        void onCrossFramePropagateBatch(dir);
        return;
      }
      if (!selectedId) {
        pushToast({ msg: "请先选中一个 3D 框", kind: "" });
        return;
      }
      const selType = (selectedAnnRef.current?.geometry as { type?: string } | undefined)?.type;
      if (selType !== "box_3d") {
        pushToast({ msg: "跨帧延续仅支持 3D 框", kind: "" });
        return;
      }
      void onCrossFramePropagate(dir);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [readOnly, selectedId, onCrossFramePropagate, onCrossFramePropagateBatch, pushToast]);

  const measurementDraftSummary = useMemo(
    () => summarizeMeasurement(measurementDraft),
    [measurementDraft],
  );
  const completeMeasurement = useCallback(() => {
    if (measurementDraft.length < 2) {
      pushToast({ msg: "至少选择两个点才能完成测量", kind: "" });
      return;
    }
    if (completedMeasurements.length >= MAX_MEASUREMENT_PATHS) {
      pushToast({ msg: `当前任务最多保留 ${MAX_MEASUREMENT_PATHS} 条测量`, kind: "" });
      return;
    }
    measurementIdRef.current += 1;
    setCompletedMeasurements((previous) => [
      ...previous,
      {
        id: `measurement-${measurementIdRef.current}`,
        anchors: measurementDraft,
        visible: true,
      },
    ]);
    setMeasurementDraft([]);
  }, [completedMeasurements.length, measurementDraft, pushToast]);
  const toggleMeasurementVisible = useCallback((measurementId: string) => {
    setCompletedMeasurements((previous) =>
      previous.map((measurement) =>
        measurement.id === measurementId
          ? { ...measurement, visible: !measurement.visible }
          : measurement,
      ),
    );
  }, []);
  const deleteMeasurement = useCallback((measurementId: string) => {
    setCompletedMeasurements((previous) =>
      previous.filter((measurement) => measurement.id !== measurementId),
    );
  }, []);

  useEffect(() => {
    boxCreationRevisionRef.current += 1;
    boxCreationInFlightRef.current = false;
    setBoxCreationSaving(false);
    setBoxCreationIssue(null);
    setSeedMode(false);
    setMeasurementDraft([]);
    setCompletedMeasurements([]);
    measurementIdRef.current = 0;
    onSetThreeDTool("select");
    setPointCloudViewMode("orbit");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  const previousThreeDToolRef = useRef(threeDTool);
  useEffect(() => {
    const previous = previousThreeDToolRef.current;
    previousThreeDToolRef.current = threeDTool;
    if (threeDTool === "box" && previous !== "box") {
      onSelectBox(null);
      setBoxCreationIssue(null);
    } else if (
      threeDTool === "point-mask" &&
      previous !== "point-mask" &&
      selectedAnn?.geometry?.type !== "point_mask_3d"
    ) {
      onSelectBox(null);
    } else if (threeDTool === "measure" && previous !== "measure") {
      onSelectBox(null);
    }
    if (previous === "measure" && threeDTool !== "measure") {
      setMeasurementDraft([]);
    }
  }, [threeDTool, selectedAnn?.geometry?.type, onSelectBox]);

  useEffect(() => {
    if (!readOnly && boxClasses.length > 0) return;
    if (threeDTool !== "box" && !seedMode) return;
    boxCreationRevisionRef.current += 1;
    boxCreationInFlightRef.current = false;
    setBoxCreationSaving(false);
    setBoxCreationIssue(null);
    setSeedMode(false);
    if (threeDTool === "box") onSetThreeDTool("select");
  }, [boxClasses.length, onSetThreeDTool, readOnly, seedMode, threeDTool]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      if (e.key === "Enter" && measuring) {
        e.preventDefault();
        completeMeasurement();
      } else if (e.key === "Enter" && pointMaskPolygonMode) {
        e.preventDefault();
        finishPointMaskPolygonRef.current(e.altKey);
      } else if (e.key === "Escape" && measuring && measurementDraft.length > 0) {
        setMeasurementDraft([]);
      } else if (e.key === "Escape" || e.key === "v" || e.key === "V") {
        setPointMaskPolygonPoints([]);
        setPointMaskCursor(null);
        setMeasurementDraft([]);
        setSeedMode(false);
        setBoxCreationIssue(null);
        onSetThreeDTool("select");
      } else if ((e.key === "b" || e.key === "B") && canPlaceBox) onSetThreeDTool("box");
      else if ((e.key === "p" || e.key === "P") && canPlacePointMask) onSetThreeDTool("point-mask");
      else if (e.key === "m" || e.key === "M") onSetThreeDTool("measure");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    canPlaceBox,
    canPlacePointMask,
    completeMeasurement,
    measurementDraft.length,
    measuring,
    onSetThreeDTool,
    pointMaskPolygonMode,
  ]);

  useEffect(() => {
    setForm(selectedBox ? psrToForm(selectedBox) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  useEffect(() => {
    if (!history.busy || draftPsr) return;
    setForm(selectedBox ? psrToForm(selectedBox) : null);
  }, [draftPsr, history.busy, selectedBox]);

  const { schedulePatch, hasPendingPatch, hasInvalidDraft, isSaving, saveError } =
    usePsrPatchPipeline({
      form,
      selectedId,
      selectedAnn,
      axisConvention,
      updateAnnotation,
      history,
    });

  const handleField = useCallback(
    (k: PsrField, value: string) => {
      setForm((prev) => {
        if (!prev) return prev;
        const next = { ...prev, [k]: value };
        schedulePatch(next);
        return next;
      });
    },
    [schedulePatch],
  );

  const handleFieldBlur = useCallback(
    (k: PsrField) => {
      if (!selectedBox) return;
      setForm((prev) => {
        if (!prev) return prev;
        if (!isPsrFieldBad(k, prev[k])) return prev;
        const next = { ...prev, [k]: psrToForm(selectedBox)[k] };
        schedulePatch(next);
        return next;
      });
    },
    [schedulePatch, selectedBox],
  );

  const updateAnnotationWithHistory = useCallback(
    (annotationId: string, before: AnnotationUpdatePayload, after: AnnotationUpdatePayload) => {
      if (!writeAllowedRef.current) return;
      if (JSON.stringify(before) === JSON.stringify(after)) return;
      void history.commit([{ kind: "update", annotationId, before, after }]);
    },
    [history.commit],
  );

  const editableSelectedBoxAnnotations = useCallback(() => {
    const ids = selectedBoxIds.length > 0 ? selectedBoxIds : selectedId ? [selectedId] : [];
    return (annotationsRef.current ?? []).filter(
      (a) => ids.includes(a.id) && a.geometry?.type === "box_3d" && !a.is_locked,
    );
  }, [selectedBoxIds, selectedId]);

  const handleDeleteSelected = useCallback(() => {
    if (readOnly) return;
    const targets = editableSelectedBoxAnnotations();
    if (targets.length === 0) return;
    void history.commit(targets.map((annotation) => ({ kind: "delete", annotation })));
    onSelectBox(null);
  }, [readOnly, editableSelectedBoxAnnotations, history.commit, onSelectBox]);

  const handleChangeClass = useCallback(
    (cls: string) => {
      if (readOnly || !cls) return;
      if (selectedPointMask && selectedId && selectedAnn && !selectedAnn.is_locked) {
        updateAnnotationWithHistory(
          selectedId,
          { class_name: selectedAnn.class_name },
          { class_name: cls },
        );
        return;
      }
      const targets = editableSelectedBoxAnnotations();
      const commands = targets
        .filter((ann) => ann.class_name !== cls)
        .map((ann) => ({
          kind: "update" as const,
          annotationId: ann.id,
          before: { class_name: ann.class_name },
          after: { class_name: cls },
        }));
      void history.commit(commands);
    },
    [
      readOnly,
      selectedPointMask,
      selectedId,
      selectedAnn,
      updateAnnotationWithHistory,
      editableSelectedBoxAnnotations,
      history.commit,
    ],
  );

  const handleChangeAttributes = useCallback(
    (next: Record<string, unknown>) => {
      if (!selectedId || !selectedAnn || readOnly || selectedAnn.is_locked || multiBoxSelected)
        return;
      updateAnnotationWithHistory(
        selectedId,
        { attributes: selectedAnn.attributes ?? {} },
        { attributes: next },
      );
    },
    [selectedId, selectedAnn, readOnly, multiBoxSelected, updateAnnotationWithHistory],
  );

  const handleResetRotation = useCallback(() => {
    if (!selectedId || !selectedBox) return;
    setForm((prev) => (prev ? { ...prev, yaw: "0", pitch: "0", roll: "0" } : prev));
    const geometry = boxGeometryFromPsr(
      {
        center: selectedBox.center,
        size: selectedBox.size,
        rotation: [0, 0, 0],
      },
      geometryConvention(selectedAnn?.geometry, axisConvention),
    );
    updateAnnotationWithHistory(
      selectedId,
      selectedAnn?.geometry?.type === "box_3d" ? { geometry: selectedAnn.geometry } : {},
      { geometry },
    );
  }, [selectedId, selectedBox, selectedAnn?.geometry, axisConvention, updateAnnotationWithHistory]);

  const applyFit = useCallback(
    (transform: (positions: Float32Array, psr: Psr) => Psr) => {
      if (!selectedId || !selectedBox || !selectedPsrEditable) return;
      const positions = sceneRef.current?.getPointPositions();
      if (!positions) return;
      const current: Psr = {
        center: selectedBox.center,
        size: selectedBox.size,
        rotation: selectedBox.rotation,
      };
      const next = transform(positions, current);
      setForm(psrToForm(next));
      const geometry = boxGeometryFromPsr(
        next,
        geometryConvention(selectedAnn?.geometry, axisConvention),
      );
      updateAnnotationWithHistory(
        selectedId,
        selectedAnn?.geometry?.type === "box_3d" ? { geometry: selectedAnn.geometry } : {},
        { geometry },
      );
    },
    [
      selectedId,
      selectedBox,
      selectedPsrEditable,
      selectedAnn?.geometry,
      axisConvention,
      updateAnnotationWithHistory,
    ],
  );
  const handleFitSize = useCallback(() => applyFit(fitSize), [applyFit]);
  const handleFitBottom = useCallback(() => applyFit(fitBottom), [applyFit]);
  const handleFitYaw = useCallback(() => applyFit(fitYaw), [applyFit]);
  const handleFitDefault = useCallback(() => applyFit(fitSizeAndBottom), [applyFit]);

  useEffect(() => {
    if (!selectedId || !selectedPsrEditable) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.key !== "q" && e.key !== "Q") return;
      if (e.ctrlKey || e.metaKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      if (e.shiftKey) handleFitSize();
      else if (e.altKey) handleFitBottom();
      else handleFitDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, selectedPsrEditable, handleFitSize, handleFitBottom, handleFitDefault]);

  useEffect(() => {
    if (readOnly || selectedBoxIds.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.key !== "Delete" && e.key !== "Backspace") return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      handleDeleteSelected();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [readOnly, selectedBoxIds.length, handleDeleteSelected]);

  const handleToggleLock = useCallback(() => {
    if (!writeAllowedRef.current) return;
    if (!selectedId) return;
    updateAnnotation.mutate({
      annotationId: selectedId,
      payload: { is_locked: !selectedLocked },
    });
  }, [selectedId, selectedLocked, updateAnnotation]);

  const handleToggleHidden = useCallback(() => {
    if (!writeAllowedRef.current) return;
    if (!selectedId) return;
    updateAnnotation.mutate({
      annotationId: selectedId,
      payload: { is_hidden: !selectedHidden },
    });
  }, [selectedId, selectedHidden, updateAnnotation]);

  const submitBoxCreation = useCallback(
    (payload: AnnotationPayload) => {
      if (!writeAllowedRef.current) return;
      if (boxCreationInFlightRef.current) return;
      const submittedTaskId = taskId;
      const revision = ++boxCreationRevisionRef.current;
      boxCreationInFlightRef.current = true;
      setBoxCreationIssue(null);
      setBoxCreationSaving(true);
      createAnnotation.mutate(payload, {
        onSuccess: (created) => {
          if (
            revision !== boxCreationRevisionRef.current ||
            submittedTaskId !== taskIdRef.current
          ) {
            return;
          }
          history.push({ kind: "create", annotationId: created.id, payload });
          onSelectBox(created.id);
        },
        onError: () => {
          if (revision !== boxCreationRevisionRef.current) return;
          setBoxCreationIssue("保存失败，请重试");
        },
        onSettled: () => {
          if (revision !== boxCreationRevisionRef.current) return;
          boxCreationInFlightRef.current = false;
          setBoxCreationSaving(false);
        },
      });
    },
    [createAnnotation, history, onSelectBox, taskId],
  );

  const handlePlace = useCallback(
    (clientX: number, clientY: number) => {
      const scene = sceneRef.current;
      if (!scene || !boxPlaceClass || boxCreationInFlightRef.current) return;
      const ground = scene.placeOnGround(clientX, clientY);
      if (!ground) {
        setBoxCreationIssue("未找到可放置位置，请换个位置重试");
        return;
      }
      const [l, w, h] = defaultBoxSize;
      const geometry = boxGeometryFromPsr(
        {
          center: [ground[0], ground[1], ground[2] + h / 2],
          size: [l, w, h],
          rotation: [0, 0, 0],
        },
        axisConvention,
      );
      const payload: AnnotationPayload = {
        annotation_type: "box_3d",
        tool_unit_id: LIDAR_TOOL_UNIT,
        class_name: boxPlaceClass,
        geometry,
      };
      submitBoxCreation(payload);
    },
    [axisConvention, boxPlaceClass, defaultBoxSize, submitBoxCreation],
  );

  const handleBoxSelect = useCallback(
    (a: { x: number; y: number }, b: { x: number; y: number }) => {
      const scene = sceneRef.current;
      if (!scene || !boxPlaceClass || boxCreationInFlightRef.current) return;
      const selected = scene.selectPointsInScreenRect(a.x, a.y, b.x, b.y);
      if (!selected) {
        setBoxCreationIssue("框内无点，请调整范围后重试");
        return;
      }
      const psr = psrFromPoints(selected);
      const geometry = boxGeometryFromPsr(psr, axisConvention);
      const payload: AnnotationPayload = {
        annotation_type: "box_3d",
        tool_unit_id: LIDAR_TOOL_UNIT,
        class_name: boxPlaceClass,
        geometry,
      };
      submitBoxCreation(payload);
    },
    [boxPlaceClass, axisConvention, submitBoxCreation],
  );

  const handleSeedBox = useCallback(
    (rect: SeedRect, calibration: SensorCalibration) => {
      const scene = sceneRef.current;
      if (!scene || !boxPlaceClass || boxCreationInFlightRef.current) return;
      const positions = scene.getPointPositions();
      if (!positions) {
        setBoxCreationIssue("点云尚未就绪，请稍后重试");
        return;
      }
      let psr = fitCameraSeedBox(positions, rect, calibration);
      if (!psr) {
        const ray = centralRay(rect, calibration);
        const center: [number, number, number] = [
          ray.origin[0] + ray.direction[0] * SEED_FALLBACK_RANGE_M,
          ray.origin[1] + ray.direction[1] * SEED_FALLBACK_RANGE_M,
          ray.origin[2] + ray.direction[2] * SEED_FALLBACK_RANGE_M,
        ];
        psr = { center, size: defaultBoxSize, rotation: [0, 0, 0] };
        pushToast({ msg: "视锥内缺少足够连续的点云回波，已按默认尺寸放置，请微调", kind: "" });
      }
      const geometry = boxGeometryFromPsr(psr, axisConvention);
      const payload: AnnotationPayload = {
        annotation_type: "box_3d",
        tool_unit_id: LIDAR_TOOL_UNIT,
        class_name: boxPlaceClass,
        geometry,
      };
      submitBoxCreation(payload);
    },
    [boxPlaceClass, axisConvention, defaultBoxSize, pushToast, submitBoxCreation],
  );

  const applyPointMaskSelection = useCallback(
    (selected: PointMaskSelection | null, subtract: boolean) => {
      if (!writeAllowedRef.current) return;
      if (!selected) return;
      const scene = sceneRef.current;
      if (selectedPointMaskEditable && selectedId && selectedPointMask) {
        const next = new Set(selectedPointMask.point_indices);
        if (subtract) {
          for (const index of selected.pointIndices) next.delete(index);
        } else {
          for (const index of selected.pointIndices) next.add(index);
        }
        const nextIndices = sortedIndices(next);
        const geometry: PointMaskGeometry = {
          ...selectedPointMask,
          point_indices: nextIndices,
        };
        scene?.highlightPointMask(nextIndices);
        updateAnnotationWithHistory(selectedId, { geometry: selectedPointMask }, { geometry });
        return;
      }
      if (!pointMaskPlaceClass) return;
      const geometry: PointMaskGeometry = {
        type: "point_mask_3d",
        point_indices: selected.pointIndices,
        convention_at_create: axisConvention,
        decimate_stride: selected.decimateStride,
        source_point_count: selected.sourcePointCount,
      };
      createAnnotation.mutate(
        {
          annotation_type: "point_mask_3d",
          tool_unit_id: POINT_MASK_TOOL_UNIT,
          class_name: pointMaskPlaceClass,
          geometry,
        },
        { onSuccess: (created) => onSelectBox(created.id) },
      );
      onSetThreeDTool("select");
    },
    [
      selectedPointMaskEditable,
      selectedId,
      selectedPointMask,
      updateAnnotationWithHistory,
      pointMaskPlaceClass,
      axisConvention,
      createAnnotation,
      onSelectBox,
      onSetThreeDTool,
    ],
  );

  const handlePointMaskRectSelect = useCallback(
    (a: ScreenPoint, b: ScreenPoint, subtract: boolean) => {
      const scene = sceneRef.current;
      if (!scene) return;
      applyPointMaskSelection(scene.selectPointMaskInScreenRect(a.x, a.y, b.x, b.y), subtract);
    },
    [applyPointMaskSelection],
  );

  const handlePointMaskPolygonSelect = useCallback(
    (polygon: readonly ScreenPoint[], subtract: boolean) => {
      const scene = sceneRef.current;
      if (!scene || polygon.length < 3) return;
      applyPointMaskSelection(scene.selectPointMaskInScreenPolygon(polygon), subtract);
    },
    [applyPointMaskSelection],
  );

  const finishPointMaskPolygon = useCallback(
    (subtract: boolean) => {
      if (pointMaskPolygonPoints.length < 3) return;
      handlePointMaskPolygonSelect(pointMaskPolygonPoints, subtract);
      setPointMaskPolygonPoints([]);
      setPointMaskCursor(null);
    },
    [handlePointMaskPolygonSelect, pointMaskPolygonPoints],
  );
  finishPointMaskPolygonRef.current = finishPointMaskPolygon;

  const createPastedBox = useCallback(
    (clip: ClipboardBox3D) => {
      if (readOnly) return;
      pasteCountRef.current += 1;
      const offset = 2 * pasteCountRef.current;
      const payload = pasteOffsetPayload(clip, [offset, offset, 0]);
      createAnnotation.mutate(payload, {
        onSuccess: (created) => {
          history.push({ kind: "create", annotationId: created.id, payload });
          onSelectBox(created.id);
        },
      });
    },
    [readOnly, createAnnotation, history, onSelectBox],
  );

  const copySelected = useCallback(() => {
    const clip = serializeBox3D(selectedAnn);
    if (!clip) return;
    clipboardRef.current = clip;
    pasteCountRef.current = 0;
  }, [selectedAnn]);

  const pasteClipboard = useCallback(() => {
    if (!clipboardRef.current) return;
    createPastedBox(clipboardRef.current);
  }, [createPastedBox]);

  const duplicateSelected = useCallback(() => {
    const clip = serializeBox3D(selectedAnn);
    if (!clip) return;
    clipboardRef.current = clip;
    pasteCountRef.current = 0;
    createPastedBox(clip);
  }, [selectedAnn, createPastedBox]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (!(e.ctrlKey || e.metaKey)) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      const key = e.key.toLowerCase();
      if (!["z", "y", "c", "v", "d"].includes(key)) return;
      if (key !== "c" && !writeAllowedRef.current) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (key === "z" && !e.shiftKey) {
        void history.undo();
      } else if (key === "y" || (key === "z" && e.shiftKey)) {
        void history.redo();
      } else if (key === "c") {
        copySelected();
      } else if (key === "v") {
        pasteClipboard();
      } else if (key === "d") {
        duplicateSelected();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [history, copySelected, pasteClipboard, duplicateSelected]);

  const pointerDownRef = useRef<{ x: number; y: number } | null>(null);
  const selectionBeforeDoubleClickRef = useRef<string[]>([]);
  const DRAG_CLICK_TOL = 4;
  const boxSelectStartRef = useRef<{ x: number; y: number } | null>(null);
  const lassoPointsRef = useRef<ScreenPoint[]>([]);
  const [isBoxSelecting, setIsBoxSelecting] = useState(false);
  const [previewRect, setPreviewRect] = useState<{
    l: number;
    t: number;
    w: number;
    h: number;
  } | null>(null);
  const [previewPath, setPreviewPath] = useState<ScreenPoint[]>([]);

  const handleViewportMouseDown = (e: React.MouseEvent) => {
    if (e.target !== viewportRef.current && !(e.target instanceof HTMLCanvasElement)) return;
    pointerDownRef.current = { x: e.clientX, y: e.clientY };
    if (e.button === 0 && e.detail === 1 && threeDTool === "select") {
      selectionBeforeDoubleClickRef.current = [...selectedIds];
    }
    if (placing && boxCreationSaving) {
      e.preventDefault();
      return;
    }
    if (placing || pointMaskDragMode) {
      if (placing) setBoxCreationIssue(null);
      sceneRef.current?.setBoxSelecting(true);
      boxSelectStartRef.current = { x: e.clientX, y: e.clientY };
      lassoPointsRef.current =
        pointMaskSelectMode === "lasso" ? [{ x: e.clientX, y: e.clientY }] : [];
      setIsBoxSelecting(true);
    }
  };

  useEffect(() => {
    if (!isBoxSelecting) return;
    const onMove = (e: MouseEvent) => {
      const start = boxSelectStartRef.current;
      const wrap = viewportRef.current;
      if (!start || !wrap) return;
      const r = wrap.getBoundingClientRect();
      if (pointMasking && pointMaskSelectMode === "lasso") {
        const points = lassoPointsRef.current;
        const last = points[points.length - 1] ?? start;
        if (Math.hypot(e.clientX - last.x, e.clientY - last.y) >= 3) {
          points.push({ x: e.clientX, y: e.clientY });
          setPreviewPath(points.map((p) => ({ x: p.x - r.left, y: p.y - r.top })));
        }
        return;
      }
      setPreviewRect({
        l: Math.min(start.x, e.clientX) - r.left,
        t: Math.min(start.y, e.clientY) - r.top,
        w: Math.abs(e.clientX - start.x),
        h: Math.abs(e.clientY - start.y),
      });
    };
    const onUp = (e: MouseEvent) => {
      const start = boxSelectStartRef.current;
      boxSelectStartRef.current = null;
      sceneRef.current?.setBoxSelecting(false);
      setIsBoxSelecting(false);
      setPreviewRect(null);
      setPreviewPath([]);
      if (!start) return;
      const dist = Math.hypot(e.clientX - start.x, e.clientY - start.y);
      if (dist <= DRAG_CLICK_TOL) {
        if (placing) handlePlace(e.clientX, e.clientY);
      } else if (placing) {
        handleBoxSelect(start, { x: e.clientX, y: e.clientY });
      } else if (pointMasking && pointMaskSelectMode === "lasso") {
        const points = lassoPointsRef.current;
        lassoPointsRef.current = [];
        handlePointMaskPolygonSelect(points, e.altKey);
      } else if (pointMasking) {
        handlePointMaskRectSelect(start, { x: e.clientX, y: e.clientY }, e.altKey);
      }
    };
    const cancel = () => {
      boxSelectStartRef.current = null;
      sceneRef.current?.setBoxSelecting(false);
      setIsBoxSelecting(false);
      setPreviewRect(null);
      setPreviewPath([]);
      lassoPointsRef.current = [];
    };
    const onCancelKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (e.key === "Escape") cancel();
    };
    const onContextMenu = (e: MouseEvent) => {
      e.preventDefault();
      cancel();
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("keydown", onCancelKey);
    window.addEventListener("contextmenu", onContextMenu);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      window.removeEventListener("keydown", onCancelKey);
      window.removeEventListener("contextmenu", onContextMenu);
    };
  }, [
    isBoxSelecting,
    placing,
    pointMasking,
    pointMaskSelectMode,
    handlePlace,
    handleBoxSelect,
    handlePointMaskRectSelect,
    handlePointMaskPolygonSelect,
  ]);

  const handleViewportMouseMove = (e: React.MouseEvent) => {
    if (!pointMaskPolygonMode || pointMaskPolygonPoints.length === 0) return;
    const r = viewportRef.current?.getBoundingClientRect();
    if (!r) return;
    setPointMaskCursor({ x: e.clientX - r.left, y: e.clientY - r.top });
  };

  const handleViewportDoubleClick = (e: React.MouseEvent) => {
    if (pointMaskPolygonMode) {
      e.preventDefault();
      finishPointMaskPolygon(e.altKey);
      return;
    }
    const scene = sceneRef.current;
    if (
      threeDTool !== "select" ||
      drawingSelection ||
      isBoxSelecting ||
      !scene ||
      scene.isTransformDragging()
    ) {
      return;
    }
    const hitId = scene.pickBox(e.clientX, e.clientY);
    if (!hitId) {
      const previous = selectionBeforeDoubleClickRef.current;
      if (previous.length > 0) {
        onSelectBox(previous[0]);
        for (const id of previous.slice(1)) onSelectBox(id, { shift: true });
      }
      return;
    }
    e.preventDefault();
    onSelectBox(hitId);
    scene.focusBox(hitId);
  };

  const handleViewportClick = (e: React.MouseEvent) => {
    if (playbackActive) {
      const down = pointerDownRef.current;
      pointerDownRef.current = null;
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) <= DRAG_CLICK_TOL) {
        onPlaybackActiveChange?.(false);
      }
      return;
    }
    if (measuring) {
      if (
        (e.target !== viewportRef.current && !(e.target instanceof HTMLCanvasElement)) ||
        e.detail !== 1
      )
        return;
      if (sceneRef.current?.shouldIgnoreClick()) return;
      const down = pointerDownRef.current;
      pointerDownRef.current = null;
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_CLICK_TOL) return;
      if (completedMeasurements.length >= MAX_MEASUREMENT_PATHS) {
        pushToast({ msg: `当前任务最多保留 ${MAX_MEASUREMENT_PATHS} 条测量`, kind: "" });
        return;
      }
      if (measurementDraft.length >= MAX_MEASUREMENT_ANCHORS) {
        pushToast({ msg: `单条测量最多包含 ${MAX_MEASUREMENT_ANCHORS} 个锚点`, kind: "" });
        return;
      }
      const hit = sceneRef.current?.pickPoint(e.clientX, e.clientY) ?? null;
      if (!hit) {
        pushToast({ msg: "未命中点云，请放大后重试", kind: "" });
        return;
      }
      if (measurementDraft[measurementDraft.length - 1]?.pointIndex === hit.pointIndex) {
        pushToast({ msg: "该点已经是当前路径的最后一个锚点", kind: "" });
        return;
      }
      setMeasurementDraft((previous) => [...previous, hit]);
      return;
    }
    if (pointMaskPolygonMode) {
      const down = pointerDownRef.current;
      pointerDownRef.current = null;
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_CLICK_TOL) return;
      setPointMaskPolygonPoints((prev) => [...prev, { x: e.clientX, y: e.clientY }]);
      return;
    }
    if (sceneRef.current?.shouldIgnoreClick()) return;
    if (drawingSelection) {
      pointerDownRef.current = null;
      return;
    }
    const down = pointerDownRef.current;
    pointerDownRef.current = null;
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_CLICK_TOL) return;
    onSelectBox(sceneRef.current?.pickBox(e.clientX, e.clientY) ?? null, { shift: e.shiftKey });
  };

  const contextMenu = useCanvasContextMenu();
  const [ctxTargetId, setCtxTargetId] = useState<string | null>(null);
  const [classPickerAnchor, setClassPickerAnchor] = useState<{ left: number; top: number } | null>(
    null,
  );
  const [framePicker, setFramePicker] = useState<{
    mode: FramePickerMode;
    anchor: { left: number; top: number };
  } | null>(null);

  const { psrPanel, psrDragging, onPsrHeaderPointerDown, togglePsrExpanded } = usePsrFloatingPanel(
    userId,
    petDock?.enabled ? petDock : null,
  );

  const closeContextMenu = () => {
    contextMenu.close();
    setCtxTargetId(null);
  };
  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    if (measuring) {
      pointerDownRef.current = null;
      closeContextMenu();
      return;
    }
    const down = pointerDownRef.current;
    pointerDownRef.current = null;
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_CLICK_TOL) {
      closeContextMenu();
      return;
    }
    if (drawingSelection || isBoxSelecting) {
      closeContextMenu();
      return;
    }
    const hitId = sceneRef.current?.pickBox(e.clientX, e.clientY) ?? null;
    setCtxTargetId(hitId);
    if (hitId && !selectedIdSet.has(hitId)) onSelectBox(hitId);
    contextMenu.openAt(e.clientX, e.clientY);
  };
  const contextMenuItems = useMemo(() => {
    if (!contextMenu.open) return [];
    const canPropagate = !!manifest?.scene_id;
    const hasClipboard = clipboardRef.current != null;
    if (ctxTargetId) {
      const ann = (annotations ?? []).find((a) => a.id === ctxTargetId);
      return buildThreeDBoxContextMenuItems({
        readOnly,
        locked: !!ann?.is_locked,
        hidden: !!ann?.is_hidden,
        hasClipboard,
        canPropagate,
        canInterpolate: ann?.track_id != null,
        onPropagateNext: () => onCrossFramePropagate("next"),
        onPropagatePrev: () => onCrossFramePropagate("prev"),
        onPropagateToFrame: () =>
          setFramePicker({
            mode: "propagate",
            anchor: { left: contextMenu.x, top: contextMenu.y },
          }),
        onInterpolate: () =>
          setFramePicker({
            mode: "interpolate",
            anchor: { left: contextMenu.x, top: contextMenu.y },
          }),
        onChangeClass: () => setClassPickerAnchor({ left: contextMenu.x, top: contextMenu.y }),
        onToggleLock: handleToggleLock,
        onToggleHidden: handleToggleHidden,
        onCopy: copySelected,
        onPaste: pasteClipboard,
        onDelete: handleDeleteSelected,
      });
    }
    return buildThreeDEmptyContextMenuItems({
      readOnly,
      hasClipboard,
      canPropagate,
      onPropagateBatchNext: () => onCrossFramePropagateBatch("next"),
      onPropagateBatchPrev: () => onCrossFramePropagateBatch("prev"),
      onPaste: pasteClipboard,
    });
  }, [
    contextMenu.open,
    contextMenu.x,
    contextMenu.y,
    ctxTargetId,
    manifest?.scene_id,
    annotations,
    readOnly,
    onCrossFramePropagate,
    onCrossFramePropagateBatch,
    handleToggleLock,
    handleToggleHidden,
    copySelected,
    pasteClipboard,
    handleDeleteSelected,
  ]);

  const handleResetView = useCallback(() => {
    sceneRef.current?.resetView();
    setPointCloudViewMode("orbit");
  }, []);
  const handleBevView = useCallback(() => {
    sceneRef.current?.bevView();
    setPointCloudViewMode("bev");
  }, []);

  const cameras = useMemo(() => {
    const raw = manifest?.cameras ?? [];
    return raw.map((c) =>
      c.calibration
        ? {
            ...c,
            calibration: {
              ...c.calibration,
              extrinsic: applyConventionToExtrinsic(
                c.calibration.extrinsic,
                axisConvention,
              ) as SensorCalibration["extrinsic"],
            },
          }
        : c,
    );
  }, [manifest?.cameras, axisConvention]);
  const cameraGroups = useMemo(() => {
    const groups = new Map<Anchor, typeof cameras>();
    for (const cam of cameras) {
      const anchor = cameraAnchor(cam.calibration, cam.role || cam.name);
      const arr = groups.get(anchor) ?? [];
      arr.push(cam);
      groups.set(anchor, arr);
    }
    return [...groups.entries()].map(
      ([anchor, cams]) =>
        [
          anchor,
          [...cams].sort((a, b) => (a.role || a.name).localeCompare(b.role || b.name)),
        ] as const,
    );
  }, [cameras]);
  const enlargedIndex = useMemo(
    () => cameras.findIndex((c) => c.role === enlargedRole),
    [cameras, enlargedRole],
  );
  const enlargedCam = useMemo(
    () => (enlargedIndex >= 0 ? cameras[enlargedIndex] : null),
    [cameras, enlargedIndex],
  );
  const enlargedCalibrationSource = useMemo(
    () => manifest?.cameras.find((camera) => camera.role === enlargedRole) ?? null,
    [enlargedRole, manifest?.cameras],
  );
  const enlargedCameraMember = useMemo(
    () =>
      cameraMembers.data?.items.find(
        (member) => member.is_active && member.camera_role === enlargedCam?.role,
      ) ?? null,
    [cameraMembers.data?.items, enlargedCam?.role],
  );
  useEffect(() => {
    setManualBboxMode(false);
  }, [enlargedCam?.role, selectedAnn?.scene_track_id, readOnly]);

  const handleManualBboxCommit = useCallback(
    async (bbox: { x: number; y: number; w: number; h: number }) => {
      if (!writeAllowedRef.current) return;
      if (
        !taskId ||
        !selectedAnn?.scene_track_id ||
        !enlargedCam?.calibration ||
        !enlargedCam.calibration_revision ||
        !enlargedCam.calibration_digest ||
        !cameraMembers.data?.track_revision
      ) {
        pushToast({ msg: "2D 成员上下文尚未就绪，请稍后重试", kind: "warning" });
        return;
      }
      try {
        if (enlargedCameraMember) {
          await cameraMembers.update.mutateAsync({
            memberId: enlargedCameraMember.id,
            payload: {
              bbox,
              expected_version: enlargedCameraMember.version,
              expected_track_revision: cameraMembers.data.track_revision,
              expected_calibration_revision: enlargedCam.calibration_revision,
              expected_calibration_digest: enlargedCam.calibration_digest,
            },
          });
        } else {
          await cameraMembers.create.mutateAsync({
            source_annotation_id: selectedAnn.id,
            camera_role: enlargedCam.role,
            bbox,
            visibility: "visible",
            expected_track_revision: cameraMembers.data.track_revision,
            expected_calibration_revision: enlargedCam.calibration_revision,
            expected_calibration_digest: enlargedCam.calibration_digest,
          });
        }
        setManualBboxMode(false);
        pushToast({
          msg: enlargedCameraMember ? "2D 成员已更新" : "2D 成员已创建",
          kind: "success",
        });
      } catch (error) {
        await cameraMembers.query.refetch();
        pushToast({
          msg: "2D 成员保存失败",
          sub: error instanceof Error ? error.message : "数据或标定已变化，请重试",
          kind: "error",
        });
      }
    },
    [cameraMembers, enlargedCam, enlargedCameraMember, pushToast, selectedAnn, taskId],
  );

  const handleManualVisibilityChange = useCallback(
    async (visibility: "visible" | "occluded" | "truncated" | "unknown") => {
      if (!writeAllowedRef.current) return;
      if (
        !enlargedCameraMember ||
        !enlargedCam?.calibration_revision ||
        !enlargedCam.calibration_digest ||
        !cameraMembers.data?.track_revision
      )
        return;
      try {
        await cameraMembers.update.mutateAsync({
          memberId: enlargedCameraMember.id,
          payload: {
            visibility,
            expected_version: enlargedCameraMember.version,
            expected_track_revision: cameraMembers.data.track_revision,
            expected_calibration_revision: enlargedCam.calibration_revision,
            expected_calibration_digest: enlargedCam.calibration_digest,
          },
        });
      } catch (error) {
        await cameraMembers.query.refetch();
        pushToast({
          msg: "可见性更新失败",
          sub: error instanceof Error ? error.message : "数据已变化，请重试",
          kind: "error",
        });
      }
    },
    [cameraMembers, enlargedCam, enlargedCameraMember, pushToast],
  );

  const handleDeleteManualBbox = useCallback(async () => {
    if (!writeAllowedRef.current) return;
    if (!enlargedCameraMember || !cameraMembers.data?.track_revision) return;
    const confirmed = await confirmDialog({
      tone: "danger",
      title: `删除 ${enlargedCam?.name ?? "当前相机"} 的 2D 成员？`,
      confirmLabel: "删除",
    });
    if (!confirmed) return;
    if (!writeAllowedRef.current) return;
    if (!enlargedCameraMember || !cameraMembers.data?.track_revision) return;
    try {
      await cameraMembers.remove.mutateAsync({
        memberId: enlargedCameraMember.id,
        payload: {
          expected_version: enlargedCameraMember.version,
          expected_track_revision: cameraMembers.data.track_revision,
        },
      });
      setManualBboxMode(false);
      pushToast({ msg: "2D 成员已删除", kind: "success" });
    } catch (error) {
      await cameraMembers.query.refetch();
      pushToast({
        msg: "2D 成员删除失败",
        sub: error instanceof Error ? error.message : "数据已变化，请重试",
        kind: "error",
      });
    }
  }, [cameraMembers, enlargedCam?.name, enlargedCameraMember, pushToast]);
  const triViewElevated = enlargedCam !== null;
  useLayoutEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    scene.setTriViewElevated(triViewElevated);
    return () => scene.setTriViewElevated(false);
  }, [rendererStatus?.actualBackend, triViewElevated]);
  const cycleEnlargedCamera = useCallback(
    (dir: -1 | 1) => {
      if (cameras.length === 0) return;
      setEnlargedRole((prev) => {
        const idx = cameras.findIndex((c) => c.role === prev);
        const base = idx >= 0 ? idx : dir > 0 ? -1 : 0;
        const next = (base + dir + cameras.length) % cameras.length;
        return cameras[next].role;
      });
    },
    [cameras],
  );
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const fwd = frontCameraForward(cameras);
    scene.setViewForward(fwd?.[0] ?? 0, fwd?.[1] ?? 1);
  }, [cameras]);
  useEffect(() => {
    if (!enlargedRole) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (calibrationSheetOpen) return;
      if (e.key === "Escape") {
        if (seedMode) setSeedMode(false);
        else setEnlargedRole(null);
      } else {
        const direction = enlargedCameraNavigationDirection(e);
        if (direction === null) return;
        e.preventDefault();
        cycleEnlargedCamera(direction);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [calibrationSheetOpen, cycleEnlargedCamera, enlargedRole, seedMode]);
  useEffect(() => {
    if (!enlargedRole) {
      setSeedMode(false);
      setCalibrationSheetOpen(false);
    }
  }, [enlargedRole]);
  const pointPositions = useMemo(
    () => (stats ? (sceneRef.current?.getPointPositions() ?? null) : null),
    [stats],
  );

  useEffect(() => {
    const scene = sceneRef.current;
    const pointCloudUrl = manifest?.point_cloud_url ?? null;
    const frameReady =
      !!scene && !!stats && !!pointCloudUrl && loadedPointCloudUrl === pointCloudUrl;
    if (!scene || !frameReady) {
      colorizedRawRef.current = null;
      adjustedColorBufferRef.current = null;
      setColorizing(false);
      setCameraTextureResources(null);
      return;
    }
    if (!colorizeOn) {
      colorizedRawRef.current = null;
      adjustedColorBufferRef.current = null;
      scene.setCameraTextureColorization(null);
      setCameraTextureResources(null);
      scene.setPointColors(null);
      scene.setPointCloudVisible(true);
      return;
    }
    const controller = new AbortController();
    setColorizing(true);
    let textureResources: CameraTextureResources | null = null;
    (async () => {
      try {
        const positions = scene.getPointPositions();
        const calibCams = cameras.filter((c) => c.calibration);
        if (!positions || calibCams.length === 0) return;
        const useTextureSampling =
          rendererStatus?.actualBackend !== "legacy-webgl2" && calibCams.length <= 6;
        if (useTextureSampling) {
          textureResources = await prepareCameraTextureResources(
            pointCloudUrl,
            positions,
            calibCams.map((camera) => ({
              imageUrl: camera.image_url,
              calibration: camera.calibration!,
            })),
            controller.signal,
          );
          if (controller.signal.aborted || originalOwner !== originalOwnerKey()) {
            textureResources.dispose();
            textureResources = null;
            return;
          }
          scene.setCameraTextureColorAdjust(colorAdjust);
          scene.setCameraTextureColorization(textureResources.samples);
          setCameraTextureResources(textureResources);
          return;
        }
        const samples = (
          await Promise.all(
            calibCams.map((c) => loadCameraSample(c.image_url, c.calibration!, controller.signal)),
          )
        ).filter((s): s is CameraSample => s !== null);
        if (
          controller.signal.aborted ||
          originalOwner !== originalOwnerKey() ||
          samples.length === 0
        )
          return;
        const colors = await colorizePointsAsync(positions, scene.getBaseColors(), samples, {
          signal: controller.signal,
        });
        if (!controller.signal.aborted && originalOwner === originalOwnerKey()) {
          colorizedRawRef.current = colors;
          adjustedColorBufferRef.current = null;
          scene.setPointColors(
            isNeutralAdjust(colorAdjust) ? colors : adjustColors(colors, colorAdjust),
          );
        }
      } catch (error) {
        if (!controller.signal.aborted && originalOwner === originalOwnerKey()) {
          console.warn("[pointcloud-colorize] failed; showing height colors", error);
          scene.setCameraTextureColorization(null);
          scene.setPointColors(null);
        }
      } finally {
        if (!controller.signal.aborted && originalOwner === originalOwnerKey()) {
          scene.setPointCloudVisible(true);
          setColorizing(false);
          markPointCloudPaint(
            "camera-color-ready",
            pointCloudUrl,
            () => !controller.signal.aborted && originalOwner === originalOwnerKey(),
          );
        }
      }
    })();
    return () => {
      controller.abort();
      scene.setCameraTextureColorization(null);
      const resources = textureResources;
      resources?.dispose();
      setCameraTextureResources((current) => (current === resources ? null : current));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    originalOwner,
    colorizeOn,
    cameras,
    loadedPointCloudUrl,
    manifest?.point_cloud_url,
    rendererStatus?.actualBackend,
    stats,
  ]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (scene && rendererStatus?.actualBackend !== "legacy-webgl2") {
      scene.setCameraTextureColorAdjust(colorAdjust);
      return;
    }
    const raw = colorizedRawRef.current;
    if (!scene || !colorizeOn || !raw) return;
    if (isNeutralAdjust(colorAdjust)) {
      scene.setPointColors(raw);
      return;
    }
    if (!adjustedColorBufferRef.current || adjustedColorBufferRef.current.length !== raw.length) {
      adjustedColorBufferRef.current = new Float32Array(raw.length);
    }
    scene.setPointColors(adjustColors(raw, colorAdjust, adjustedColorBufferRef.current));
  }, [colorAdjust, colorizeOn, rendererStatus?.actualBackend]);

  useEffect(() => {
    sceneRef.current?.highlightPointMask(selectedPointMask?.point_indices ?? null);
  }, [selectedPointMask?.point_indices, stats]);

  const [playbackTrackId, setPlaybackTrackId] = useState<string | null>(null);
  const selectedTrackId = selectedAnn?.is_hidden ? null : (selectedAnn?.track_id ?? null);
  useEffect(() => {
    if (!playbackActive || !playbackTrackId || !annotationsQuery.isSuccess) return;
    const member = annotations?.find((annotation) => annotation.track_id === playbackTrackId);
    if (member && member.id !== selectedId) onSelectBox(member.id);
  }, [
    annotations,
    annotationsQuery.isSuccess,
    onSelectBox,
    playbackActive,
    playbackTrackId,
    selectedId,
  ]);
  const highlightedIds = useMemo(() => {
    const s = new Set<string>();
    for (const id of selectedIds) s.add(id);
    if (selectedTrackId != null) {
      for (const a of annotations ?? []) {
        if (a.track_id === selectedTrackId) s.add(a.id);
      }
    }
    return s;
  }, [selectedIds, selectedTrackId, annotations]);

  const neighborsActive = overlayK > 0 || pointOverlayK > 0;
  const neighborsFetchK = Math.max(1, overlayK, pointOverlayK);
  const { data: neighborsData } = useFrameNeighbors(
    neighborsActive ? taskId : null,
    neighborsFetchK,
  );
  const neighborTaskIds = useMemo(() => {
    if (overlayK <= 0 || !neighborsData) return [];
    return [
      ...(neighborsData.prev ?? []).slice(0, overlayK),
      ...(neighborsData.next ?? []).slice(0, overlayK),
    ].map((n) => n.task_id);
  }, [overlayK, neighborsData]);
  const boxAnnotationOverlayEnabled =
    overlayK > 0 && (overlayScope === "all" || selectedTrackId != null);
  const overlayTrackId = overlayScope === "all" ? null : selectedTrackId;
  const { byTask: neighborAnnsByTask } = useNeighborAnnotations(
    overlayK > 0 ? taskId : null,
    overlayK,
    overlayTrackId,
    boxAnnotationOverlayEnabled,
  );
  const { poseByFrame } = useSceneTrajectory(
    neighborsActive ? (neighborsData?.scene_id ?? null) : null,
  );
  const neighborFrameByTask = useMemo<Map<string, number>>(() => {
    if (!neighborsData) return new Map();
    return new Map(
      [...(neighborsData.prev ?? []), ...(neighborsData.next ?? [])].map((n) => [
        n.task_id,
        n.frame_index,
      ]),
    );
  }, [neighborsData]);
  const referenceBoxes = useMemo<ReferenceBox[]>(() => {
    if (overlayK <= 0) return [];
    if (overlayScope === "selected" && selectedTrackId == null) return [];
    const curFrame = neighborsData?.frame_index ?? null;
    const out: ReferenceBox[] = [];
    for (const tid of neighborTaskIds) {
      for (const a of neighborAnnsByTask[tid] ?? []) {
        if (a.is_hidden) continue;
        const g = a.geometry as {
          type?: string;
          center?: number[];
          size?: number[];
          rotation?: number[];
        };
        if (g?.type !== "box_3d" || !g.center || !g.size || !g.rotation) continue;
        let center = g.center as [number, number, number];
        let rotation = g.rotation as [number, number, number];
        if (poseByFrame && curFrame != null) {
          const nbrFrame = neighborFrameByTask.get(tid);
          const aligned = alignPsrToFrame(
            { center, rotation },
            nbrFrame != null ? poseByFrame.get(nbrFrame) : undefined,
            poseByFrame.get(curFrame),
          );
          if (aligned) {
            center = aligned.center;
            rotation = aligned.rotation;
          }
        }
        const dim =
          overlayScope === "all" && selectedTrackId != null && a.track_id !== selectedTrackId;
        out.push({
          id: `${tid}:${a.id}`,
          center,
          size: g.size.map((v) => Math.abs(v)) as [number, number, number],
          rotation,
          color: classColorForCanvas(a.class_name),
          dim,
        });
      }
    }
    return out;
  }, [
    overlayK,
    overlayScope,
    selectedTrackId,
    neighborTaskIds,
    neighborAnnsByTask,
    neighborsData,
    poseByFrame,
    neighborFrameByTask,
  ]);
  useEffect(() => {
    sceneRef.current?.setReferenceBoxes(referenceBoxes);
  }, [referenceBoxes]);

  const pointOverlayActive = pointOverlayK > 0 && !!neighborsData?.scene_id && !!poseByFrame;
  const pointNeighbors = useMemo(() => {
    if (!pointOverlayActive || !neighborsData) return [];
    return [
      ...(neighborsData.prev ?? []).slice(0, pointOverlayK),
      ...(neighborsData.next ?? []).slice(0, pointOverlayK),
    ].map((n) => ({ taskId: n.task_id, frameIndex: n.frame_index }));
  }, [pointOverlayActive, neighborsData, pointOverlayK]);
  const alignActive = pointOverlayActive && neighborPointCull === "align";
  const { byTask: alignAnnsByTask } = useNeighborAnnotations(
    alignActive ? taskId : null,
    pointOverlayK,
    null,
    alignActive,
  );
  const currentBoxesByTrack = useMemo<Map<string, AlignPsr>>(() => {
    const m = new Map<string, AlignPsr>();
    if (!alignActive) return m;
    for (const a of annotations ?? []) {
      if (a.is_hidden) continue;
      if (a.track_id == null) continue;
      const g = a.geometry as {
        type?: string;
        center?: number[];
        size?: number[];
        rotation?: number[];
      };
      if (g?.type !== "box_3d" || !g.center || !g.size || !g.rotation) continue;
      m.set(a.track_id, {
        center: g.center as [number, number, number],
        size: g.size.map((v) => Math.abs(v)) as [number, number, number],
        rotation: g.rotation as [number, number, number],
      });
    }
    return m;
  }, [alignActive, annotations]);
  const neighborBoxesByTask = useMemo<Map<string, AlignNeighborBox[]>>(() => {
    const m = new Map<string, AlignNeighborBox[]>();
    if (!alignActive) return m;
    for (const [tid, anns] of Object.entries(alignAnnsByTask)) {
      const list: AlignNeighborBox[] = [];
      for (const a of anns) {
        if (a.is_hidden) continue;
        if (a.track_id == null) continue;
        const g = a.geometry as {
          type?: string;
          center?: number[];
          size?: number[];
          rotation?: number[];
        };
        if (g?.type !== "box_3d" || !g.center || !g.size || !g.rotation) continue;
        list.push({
          trackId: a.track_id,
          center: g.center as [number, number, number],
          size: g.size.map((v) => Math.abs(v)) as [number, number, number],
          rotation: g.rotation as [number, number, number],
        });
      }
      m.set(tid, list);
    }
    return m;
  }, [alignActive, alignAnnsByTask]);
  const neighborPointTarget = Math.min(80_000, Math.round(performanceConfig.pcdDecimate / 8));
  const { items: neighborPcds } = useNeighborPointClouds(
    pointNeighbors,
    axisConvention,
    neighborPointTarget,
    pointOverlayActive,
  );
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const curFrame = neighborsData?.frame_index ?? null;
    if (!pointOverlayActive || !poseByFrame || curFrame == null) {
      scene.setNeighborPoints([]);
      setNeighborCulledCount(0);
      setNeighborMovedCount(0);
      return;
    }
    const toPose = poseByFrame.get(curFrame);
    type NeighborFrame = {
      positions: Float32Array;
      matrix: NonNullable<ReturnType<typeof frameRelMatrix>>;
      dir: "past" | "future";
      distance: number;
    };
    const cullOn = neighborPointCull === "cull" && boxes.length > 0;
    const alignOn = neighborPointCull === "align" && currentBoxesByTrack.size > 0;
    let culledTotal = 0;
    let movedTotal = 0;
    const frames = neighborPcds
      .map((pcd): NeighborFrame | null => {
        const matrix = frameRelMatrix(poseByFrame.get(pcd.frameIndex), toPose);
        if (!matrix) return null;
        let positions = pcd.positions;
        let renderMatrix = matrix;
        if (alignOn) {
          const res = alignNeighborPointsPerObject(
            positions,
            matrix,
            neighborBoxesByTask.get(pcd.taskId) ?? [],
            currentBoxesByTrack,
          );
          positions = res.aligned;
          movedTotal += res.movedCount;
          renderMatrix = IDENTITY_MATRIX;
        } else if (cullOn) {
          const res = cullPointsInBoxes(positions, matrix, boxes);
          positions = res.kept;
          culledTotal += res.culledCount;
        }
        return {
          positions,
          matrix: renderMatrix,
          dir: pcd.frameIndex >= curFrame ? "future" : "past",
          distance: Math.abs(pcd.frameIndex - curFrame),
        };
      })
      .filter((f): f is NeighborFrame => f != null);
    scene.setNeighborPoints(frames);
    setNeighborCulledCount(cullOn ? culledTotal : 0);
    setNeighborMovedCount(alignOn ? movedTotal : 0);
  }, [
    pointOverlayActive,
    neighborPcds,
    poseByFrame,
    neighborsData,
    neighborPointCull,
    boxes,
    currentBoxesByTrack,
    neighborBoxesByTask,
  ]);

  const selectedCameraVis = useMemo(() => {
    if (!selectedBox) return [] as { role: string; name: string; count: number }[];
    const corners = psrToCorners(selectedBox.center, selectedBox.size, selectedBox.rotation);
    return cameras
      .filter((c) => c.calibration)
      .map((c) => ({
        role: c.role,
        name: c.name,
        count: projectPoints(corners, c.calibration!).visible.filter(Boolean).length,
      }))
      .filter((v) => v.count > 0)
      .sort((a, b) => b.count - a.count);
  }, [selectedBox, cameras]);
  const bestCameraRole = selectedCameraVis[0]?.role ?? null;

  const [triZoomByAnnotation, setTriZoomByAnnotation] = useState(
    () => new Map<string, Record<TriView, number>>(),
  );
  const triZoomAnnotationId = !multiBoxSelected ? (selectedBox?.id ?? null) : null;
  const triZoomByView = useMemo(
    () =>
      triZoomAnnotationId
        ? (triZoomByAnnotation.get(triZoomAnnotationId) ?? DEFAULT_TRI_ZOOM_BY_VIEW)
        : DEFAULT_TRI_ZOOM_BY_VIEW,
    [triZoomAnnotationId, triZoomByAnnotation],
  );
  const handleTriZoomChange = useCallback(
    (view: TriView, zoom: number) => {
      if (!triZoomAnnotationId) return;
      setTriZoomByAnnotation((currentByAnnotation) => {
        const current = currentByAnnotation.get(triZoomAnnotationId) ?? DEFAULT_TRI_ZOOM_BY_VIEW;
        const next = new Map(currentByAnnotation);
        next.set(triZoomAnnotationId, { ...current, [view]: clampTriZoom(zoom) });
        return next;
      });
    },
    [triZoomAnnotationId],
  );
  const triSelected = useMemo<TriSelected | null>(
    () =>
      selectedBox && !multiBoxSelected
        ? {
            center: selectedBox.center,
            size: selectedBox.size,
            rotation: selectedBox.rotation,
            color: selectedBox.color,
          }
        : null,
    [selectedBox, multiBoxSelected],
  );
  const handleEditPsr = useCallback(
    (psr: Psr, commit: boolean) => {
      if (!selectedId) return;
      const center: [number, number, number] = [psr.center[0], psr.center[1], psr.center[2]];
      const size: [number, number, number] = [psr.size[0], psr.size[1], psr.size[2]];
      const rotation: [number, number, number] = [
        psr.rotation[0],
        psr.rotation[1],
        psr.rotation[2],
      ];
      setForm(psrToForm({ center, size, rotation }));
      if (commit) {
        setDraftPsr(null);
        const geometry = boxGeometryFromPsr(
          { center, size, rotation },
          geometryConvention(selectedAnn?.geometry, axisConvention),
        );
        updateAnnotationWithHistory(
          selectedId,
          selectedAnn?.geometry?.type === "box_3d" ? { geometry: selectedAnn.geometry } : {},
          { geometry },
        );
      } else {
        setDraftPsr({ id: selectedId, psr, source: "triview" });
      }
    },
    [selectedId, selectedAnn?.geometry, axisConvention, updateAnnotationWithHistory],
  );

  const handleCancelCameraEdit = useCallback(
    (boxId: string, original: Psr) => {
      setDraftPsr(null);
      if (selectedId === boxId) setForm(psrToForm(original));
    },
    [selectedId],
  );

  const handleCameraEditError = useCallback(() => {
    pushToast({ msg: "相机标定不可逆，无法在图像中调整 3D 框", kind: "" });
  }, [pushToast]);

  const handleReprojectSelectedToCurrentConvention = useCallback(() => {
    if (!selectedId || !selectedBox || !selectedConventionMismatch || !selectedEditable) return;
    const src = unapplyConventionToPsr(
      {
        center: selectedBox.center,
        size: selectedBox.size,
        rotation: selectedBox.rotation,
      },
      selectedConventionMismatch,
    );
    const next = applyConventionToPsr(src, axisConvention);
    const geometry = boxGeometryFromPsr(next, axisConvention);
    setForm(psrToForm(next));
    updateAnnotationWithHistory(
      selectedId,
      selectedAnn?.geometry?.type === "box_3d" ? { geometry: selectedAnn.geometry } : {},
      { geometry },
    );
  }, [
    selectedId,
    selectedBox,
    selectedConventionMismatch,
    selectedEditable,
    axisConvention,
    selectedAnn?.geometry,
    updateAnnotationWithHistory,
  ]);

  const boxSelectRectRef = useElementStyle<HTMLDivElement>(
    previewRect
      ? ({
          "--rect-l": `${previewRect.l}px`,
          "--rect-t": `${previewRect.t}px`,
          "--rect-w": `${previewRect.w}px`,
          "--rect-h": `${previewRect.h}px`,
        } as CSSProperties)
      : undefined,
  );
  const petLinked = petDock?.enabled === true;
  const petDetailsVisible = Boolean(
    petLinked && ((selectedBox && form && psrPanel.expanded) || (selectedPointMask && selectedAnn)),
  );
  const onPetDetailsVisibleChange = petDock?.onDetailsVisibleChange;
  useLayoutEffect(() => {
    onPetDetailsVisibleChange?.(petDetailsVisible);
    return () => onPetDetailsVisibleChange?.(false);
  }, [onPetDetailsVisibleChange, petDetailsVisible]);
  const editPanelRef = useElementStyle<HTMLDivElement>(
    petLinked
      ? ({
          "--psr-pet-x": `${petDock.position.x + WORKBENCH_PET_SIZE.w / 2}px`,
          "--psr-pet-y": `${petDock.position.y + WORKBENCH_PET_SIZE.h / 2}px`,
        } as CSSProperties)
      : ({
          "--psr-dx": `${psrPanel.dx}px`,
          "--psr-dy": `${psrPanel.dy}px`,
        } as CSSProperties),
  );
  const editPanelClass = [
    EDIT_PANEL_SURFACE,
    petLinked ? EDIT_PANEL_PET_LINKED : EDIT_PANEL_FLOATING,
    psrDragging ? EDIT_PANEL_DRAGGING : "",
  ]
    .filter(Boolean)
    .join(" ");
  const modalRect = viewportWrapRef.current?.getBoundingClientRect();
  const cameraModalRef = useElementStyle<HTMLDivElement>({
    "--camera-edit-left": (modalRect?.left ?? 0) + "px",
    "--camera-edit-top": (modalRect?.top ?? 0) + "px",
    "--camera-edit-width": (modalRect?.width ?? 0) + "px",
    "--camera-edit-height": (modalRect?.height ?? 0) + "px",
    clipPath:
      modalRect && viewportRef.current && dockLayout
        ? viewportRegionClipPath(dockLayout.getVisibleRegions(viewportRef.current), modalRect)
        : undefined,
  } as CSSProperties);
  const renderPsrPanel = (panel: ReactNode) =>
    petLinked ? createPortal(panel, document.body) : panel;

  const [canvasVisible, setCanvasVisible] = useState(true);
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => {
      const rect = viewport.getBoundingClientRect();
      setCanvasVisible(
        rect.width > 0 &&
          rect.height > 0 &&
          (!dockLayout || dockLayout.getVisibleRegions(viewport).length > 0),
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [dockLayout]);
  const originalError = manifest?.original_error
    ? "点云原件不可用，请检查原件归属、存储位置和访问权限"
    : null;
  const frameError = error || originalError || annotationsQuery.error || loadError || rendererError;
  const frameState = {
    taskId,
    status: frameError
      ? ("error" as const)
      : taskId &&
          manifest?.task_id === taskId &&
          annotationsQuery.isSuccess &&
          manifest.point_cloud_url &&
          loadedPointCloudUrl === manifest.point_cloud_url &&
          stats &&
          !pointCloudLoading
        ? ("ready" as const)
        : ("loading" as const),
    error: frameError
      ? String(frameError instanceof Error ? frameError.message : frameError)
      : null,
  };
  const playbackBlockedReason =
    shellPlaybackBlockedReason ||
    (hasInvalidDraft
      ? "请先修正或取消未完成的数值输入"
      : hasPendingPatch
        ? "数值修改正在等待保存"
        : isSaving ||
            createAnnotation.isPending ||
            deleteAnnotation.isPending ||
            history.busy ||
            boxCreationSaving
          ? "标注正在保存"
          : saveError || createAnnotation.error || deleteAnnotation.error || boxCreationIssue
            ? "标注保存失败，请先重试或修正"
            : draftPsr || isBoxSelecting || psrDragging
              ? "请先结束当前拖动"
              : pointMaskPolygonPoints.length > 0 ||
                  measurementDraft.length > 0 ||
                  threeDTool === "box" ||
                  threeDTool === "point-mask"
                ? "请先完成或取消绘制，再切回选择工具"
                : enlargedRole || calibrationSheetOpen || framePicker || classPickerAnchor
                  ? "请先关闭编辑面板"
                  : null);

  return (
    <div
      className={ROOT}
      data-scene-playback={playbackActive ? "playing" : "paused"}
      data-scene-frame-state={frameState.status}
      onPointerDownCapture={(event) => {
        const target = event.target as HTMLElement;
        if (playbackActive && target.closest('[data-testid="tri-view-renderer-panel"]')) {
          event.preventDefault();
          event.stopPropagation();
          onPlaybackActiveChange?.(false);
        }
      }}
    >
      <div ref={viewportWrapRef} className={VIEWPORT_WRAP}>
        <div
          ref={viewportRef}
          className={
            drawingSelection || measuring
              ? `${VIEWPORT} ${boxCreationSaving && placing ? CREATION_BLOCKED : PLACING}`
              : VIEWPORT
          }
          data-testid="pc-viewport"
          onMouseDown={handleViewportMouseDown}
          onMouseMove={handleViewportMouseMove}
          onClick={handleViewportClick}
          onDoubleClick={handleViewportDoubleClick}
          onContextMenu={handleContextMenu}
        />

        {previewRect && <div ref={boxSelectRectRef} className={BOX_SELECT_RECT} />}
        {(previewPath.length > 0 || pointMaskPolygonPoints.length > 0) &&
          (() => {
            const r = viewportRef.current?.getBoundingClientRect();
            const draft = r
              ? pointMaskPolygonPoints.map((p) => ({ x: p.x - r.left, y: p.y - r.top }))
              : [];
            const points =
              previewPath.length > 0
                ? previewPath
                : [...draft, ...(pointMaskCursor ? [pointMaskCursor] : [])];
            const svgPoints = points.map((p) => `${p.x},${p.y}`).join(" ");
            return (
              <svg className={POINT_MASK_PATH_PREVIEW} aria-hidden="true">
                <polyline
                  className="[fill:none] [stroke:var(--sc-brand)] [stroke-width:2] [stroke-linejoin:round] [stroke-linecap:round]"
                  points={svgPoints}
                />
                {draft.map((p, i) => (
                  <circle
                    className="[fill:var(--sc-card)] [stroke:var(--sc-brand)] [stroke-width:2]"
                    key={`${p.x}:${p.y}:${i}`}
                    cx={p.x}
                    cy={p.y}
                    r="3"
                  />
                ))}
              </svg>
            );
          })()}

        <div ref={controlsRef} className={CONTROLS}>
          <div className={CONTROL_BAR}>
            <Button
              type="button"
              size="xs"
              className="h-7"
              variant={pointCloudViewMode === "bev" ? "primary" : "ghost"}
              onClick={handleBevView}
              aria-pressed={pointCloudViewMode === "bev"}
            >
              俯视
            </Button>
            <Tooltip name="重置视角" side="bottom">
              <Button
                type="button"
                size="xs"
                className="size-7 p-0"
                variant="ghost"
                aria-label="重置视角"
                onClick={handleResetView}
              >
                <Icon name="rotate-ccw" size={14} />
              </Button>
            </Tooltip>
            {threeDTool === "point-mask" && (
              <label className={SIZE_CTL}>
                选点
                <select
                  className={SELECT_CTL}
                  data-testid="pointmask-mode-select"
                  value={pointMaskSelectMode}
                  disabled={!canPlacePointMask}
                  onChange={(e) => {
                    const next = e.target.value;
                    if (isPointMaskSelectMode(next)) {
                      onWorkbenchConfigChange({
                        pointcloud: { pointMaskSelectMode: next },
                      });
                    }
                  }}
                >
                  <option value="rect">矩形</option>
                  <option value="lasso">套索</option>
                  <option value="polygon">多边形</option>
                </select>
              </label>
            )}
          </div>
        </div>

        <ContextMenu
          open={contextMenu.open && contextMenuItems.length > 0}
          x={contextMenu.x}
          y={contextMenu.y}
          items={contextMenuItems}
          onClose={closeContextMenu}
        />
        {classPickerAnchor && (
          <ClassPickerPopover
            position="fixed"
            anchor={classPickerAnchor}
            classes={boxClasses}
            recent={[]}
            defaultClass={selectedClass ?? boxClasses[0] ?? ""}
            title="改类别"
            onPick={(cls) => {
              handleChangeClass(cls);
              setClassPickerAnchor(null);
            }}
            onCancel={() => setClassPickerAnchor(null)}
          />
        )}
        {framePicker && taskId && (
          <FramePicker
            taskId={taskId}
            frameIndex={manifest?.frame_index ?? null}
            sceneTotalFrames={manifest?.scene_total_frames ?? null}
            mode={framePicker.mode}
            anchor={framePicker.anchor}
            pushToast={pushToast}
            onConfirm={({ targetTaskId, targetFrame }) => {
              if (framePicker.mode === "propagate") {
                onCrossFramePropagateToTask(targetTaskId, targetFrame);
              } else if (selectedTrackId != null) {
                onCrossFrameInterpolate(selectedTrackId, targetTaskId);
              }
              setFramePicker(null);
            }}
            onCancel={() => setFramePicker(null)}
          />
        )}

        {conventionMismatches.length > 0 && (
          <div className={MISMATCH_BANNER}>
            <span>{conventionMismatches.length} 个 3D 标注创建时的坐标系与当前数据集不一致。</span>
            <button
              type="button"
              className={BTN}
              disabled={!selectedConventionMismatch || !selectedEditable}
              onClick={handleReprojectSelectedToCurrentConvention}
            >
              按当前约定重投影选中
            </button>
          </div>
        )}

        <div className={STATUS_BAR}>
          {colorizing && <span>相机上色…</span>}
          {isLoading && <span>加载 manifest…</span>}
          {pointCloudLoading && <span data-testid="pointcloud-loading">加载点云…</span>}
          {error && <span className={ERR}>manifest 加载失败</span>}
          {originalError && <span className={ERR}>{originalError}</span>}
          {loadError && (
            <span className={ERR}>
              点云加载失败: <CopyableError message={loadError} />
            </span>
          )}
          {rendererError && (
            <span className={ERR} data-testid="pointcloud-renderer-error">
              Renderer 初始化失败: <CopyableError message={rendererError} />
            </span>
          )}
          <Badge
            variant={rendererStatus?.actualBackend === "webgpu" ? "accent" : "outline"}
            data-testid="pointcloud-renderer-backend"
            data-backend={
              rendererError ? "failed" : (rendererStatus?.actualBackend ?? "initializing")
            }
            title={rendererError ?? rendererStatus?.fallbackReason ?? undefined}
          >
            {rendererError ? "Renderer 不可用" : rendererBackendLabel(rendererStatus)}
          </Badge>
          {stats && (
            <span data-testid="pointcloud-stats">
              {stats.renderedPoints.toLocaleString()} 点
              {stats.decimated && `(已抽稀自 ${stats.totalPoints.toLocaleString()})`}
            </span>
          )}
          {boxes.length > 0 && <span>· {boxes.length} 框</span>}
          {neighborCulledCount > 0 && (
            <span>· 邻帧剔除 {neighborCulledCount.toLocaleString()} 动态点</span>
          )}
          {neighborMovedCount > 0 && (
            <span>· 邻帧对齐 {neighborMovedCount.toLocaleString()} 动态点</span>
          )}
          {pointMasks.length > 0 && <span>· {pointMasks.length} 分割</span>}
          {placing && (
            <Badge
              variant={boxCreationIssue ? "danger" : boxCreationSaving ? "warning" : "accent"}
              dot
              data-testid="three-d-creation-status"
              data-phase={
                boxCreationSaving
                  ? "saving"
                  : boxCreationIssue
                    ? "error"
                    : isBoxSelecting
                      ? "drawing"
                      : "armed"
              }
            >
              {boxCreationSaving
                ? `连续建框 · 正在保存 ${boxPlaceClass ?? ""}`
                : boxCreationIssue
                  ? `连续建框 · ${boxCreationIssue} · V/Esc 退出`
                  : isBoxSelecting
                    ? `连续建框 · 正在拟合 ${boxPlaceClass ?? ""}`
                    : `连续建框 · ${boxPlaceClass ?? ""} · 点击放置 / 拖框拟合 · V/Esc 退出`}
            </Badge>
          )}
          {pointMasking && (
            <span>
              · {pointMaskSelectMode === "polygon" ? "点击多边形闭合" : "拖动选点"}
              {selectedPointMaskEditable
                ? "编辑分割(Alt 减点)"
                : `生成分割 ${pointMaskPlaceClass ?? ""}`}
              · V/Esc 取消
            </span>
          )}
          {threeDTool === "point-mask" && !canPlacePointMask && (
            <span className={ERR}>· 当前项目未启用 point_mask_3d 类别</span>
          )}
          {measuring && (
            <span data-testid="measurement-status">
              · 测量 · 单击点云添加锚点 · Enter 完成 · Esc 取消草稿 · V 退出
            </span>
          )}
          {completedMeasurements.length > 0 && <span>· {completedMeasurements.length} 条测量</span>}
          {selectedBox && (
            <span>
              · 选中 {selectedClass ?? ""} 中心 [
              {selectedBox.center.map((n) => n.toFixed(2)).join(", ")}]
            </span>
          )}
          {selectedBox && selectedCameraVis.length > 0 && (
            <span>
              · 投影可见于 {selectedCameraVis.length} 相机 · 正对 {selectedCameraVis[0].name}
            </span>
          )}
        </div>

        {measuring && (
          <aside
            className={MEASUREMENT_PANEL}
            aria-label="点云测量"
            data-testid="measurement-panel"
          >
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="text-sm font-semibold">点云测量</div>
                <div className="text-muted-foreground">结果仅保留在当前任务会话</div>
              </div>
              {completedMeasurements.length > 0 && (
                <button
                  type="button"
                  className={RESET_BTN}
                  onClick={() => setCompletedMeasurements([])}
                >
                  全部清除
                </button>
              )}
            </div>

            <div className={MEASUREMENT_ROW} data-testid="measurement-draft">
              <div className="min-w-0 flex-1">
                <div className="font-medium">当前测量 · {measurementDraft.length} 个锚点</div>
                {measurementDraft.length === 0 ? (
                  <div className="mt-1 text-muted-foreground">在主点云中依次单击真实点。</div>
                ) : measurementDraft.length === 1 ? (
                  <div className="mt-1 text-muted-foreground">继续选择第二个点以形成距离。</div>
                ) : (
                  <div className={`${MEASUREMENT_VALUE_GRID} mt-1`}>
                    <span>三维总长</span>
                    <span>{formatMeasurementMeters(measurementDraftSummary.distance3d)}</span>
                    <span>水平总长</span>
                    <span>
                      {formatMeasurementMeters(measurementDraftSummary.horizontalDistance)}
                    </span>
                    <span>首尾高差</span>
                    <span>
                      {formatMeasurementMeters(measurementDraftSummary.elevationChange, true)}
                    </span>
                  </div>
                )}
                <div className="mt-2 flex gap-1.5">
                  <button
                    type="button"
                    className={BTN}
                    disabled={measurementDraft.length < 2}
                    onClick={completeMeasurement}
                  >
                    完成当前
                  </button>
                  <button
                    type="button"
                    className={BTN}
                    disabled={measurementDraft.length === 0}
                    onClick={() => setMeasurementDraft([])}
                  >
                    取消草稿
                  </button>
                </div>
              </div>
            </div>

            {completedMeasurements.map((measurement, index) => {
              const summary = summarizeMeasurement(measurement.anchors);
              return (
                <div
                  key={measurement.id}
                  className={MEASUREMENT_ROW}
                  data-testid={`measurement-item-${measurement.id}`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">
                      测量 {index + 1} · {summary.segmentCount} 段
                    </div>
                    <div className={`${MEASUREMENT_VALUE_GRID} mt-1`}>
                      <span>三维总长</span>
                      <span>{formatMeasurementMeters(summary.distance3d)}</span>
                      <span>水平总长</span>
                      <span>{formatMeasurementMeters(summary.horizontalDistance)}</span>
                      <span>首尾高差</span>
                      <span>{formatMeasurementMeters(summary.elevationChange, true)}</span>
                    </div>
                  </div>
                  <div className={MEASUREMENT_ACTIONS}>
                    <button
                      type="button"
                      className={ICON_BTN}
                      aria-label={
                        measurement.visible ? `隐藏测量 ${index + 1}` : `显示测量 ${index + 1}`
                      }
                      onClick={() => toggleMeasurementVisible(measurement.id)}
                    >
                      <Icon name={measurement.visible ? "eye" : "eyeOff"} size={13} />
                    </button>
                    <button
                      type="button"
                      className={ICON_BTN}
                      aria-label={`删除测量 ${index + 1}`}
                      onClick={() => deleteMeasurement(measurement.id)}
                    >
                      <Icon name="trash" size={13} />
                    </button>
                  </div>
                </div>
              );
            })}
          </aside>
        )}

        {selectedBox &&
          form &&
          renderPsrPanel(
            <div
              ref={editPanelRef}
              className={editPanelClass}
              data-testid="three-d-selection-panel"
              data-pet-linked={petLinked || undefined}
            >
              <div
                className={psrDragging ? `${EDIT_HEADER} !cursor-grabbing` : EDIT_HEADER}
                onPointerDown={onPsrHeaderPointerDown}
                data-testid="three-d-selection-panel-handle"
              >
                <div className={EDIT_TITLE}>
                  <Icon name="move" size={12} className={DRAG_HINT} />
                  {boxClasses.length > 0 ? (
                    <select
                      className={CLASS_SELECT}
                      value={selectedClass ?? ""}
                      aria-label="框类别"
                      disabled={!selectedEditable}
                      onChange={(e) => handleChangeClass(e.target.value)}
                    >
                      {selectedClass && !boxClasses.includes(selectedClass) && (
                        <option value={selectedClass}>{selectedClass}</option>
                      )}
                      {boxClasses.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span>3D 框 · {selectedClass ?? ""}</span>
                  )}
                  {!readOnly && (
                    <button
                      type="button"
                      className={selectedLocked ? `${LOCK_BTN} ${LOCK_BTN_ON}` : LOCK_BTN}
                      aria-pressed={selectedLocked}
                      onClick={handleToggleLock}
                    >
                      {selectedLocked ? "已锁定" : "锁定"}
                    </button>
                  )}
                  {!readOnly && selectedBoxIds.length > 0 && (
                    <button
                      type="button"
                      className={ICON_BTN}
                      onClick={handleDeleteSelected}
                      aria-label={
                        multiBoxSelected ? `删除选中 ${selectedBoxIds.length} 个框` : "删除框"
                      }
                      title={multiBoxSelected ? `删除选中 ${selectedBoxIds.length} 个框` : "删除框"}
                    >
                      <Icon name="trash" size={14} />
                    </button>
                  )}
                  <button
                    type="button"
                    className={ICON_BTN}
                    onClick={togglePsrExpanded}
                    aria-expanded={psrPanel.expanded}
                    aria-label={psrPanel.expanded ? "收起详情" : "展开详情"}
                    title={psrPanel.expanded ? "收起" : "展开"}
                  >
                    <Icon name={psrPanel.expanded ? "chevUp" : "chevDown"} size={14} />
                  </button>
                </div>
                <div className={EDIT_SUMMARY}>
                  {multiBoxSelected
                    ? `${selectedBoxIds.length} 个框已选中`
                    : `尺寸 ${selectedBox.size.map((n) => n.toFixed(2)).join(" × ")} m`}
                </div>
              </div>
              {psrPanel.expanded && (
                <div className={EDIT_BODY}>
                  <div className={EDIT_GROUP_LABEL}>
                    {readOnly
                      ? "只读 · 锁定 / 审阅态"
                      : multiBoxSelected
                        ? `${selectedBoxIds.length} 个框已选中 · 可批量改类 / 删除`
                        : selectedLocked
                          ? "已锁定 · 点「已锁定」解锁后可编辑"
                          : "拖 gizmo 或改数值 · W 平移 / E 转 / R 缩放"}
                  </div>
                  {selectedPsrEditable && (
                    <div className={FIT_GROUP} role="group" aria-label="自动贴合">
                      <button
                        type="button"
                        className={BTN}
                        onClick={handleFitDefault}
                        title="贴合 (Q):收尺寸 + 贴地"
                      >
                        贴合
                      </button>
                      <button
                        type="button"
                        className={BTN}
                        onClick={handleFitSize}
                        title="只收尺寸 (Shift+Q)"
                      >
                        收尺寸
                      </button>
                      <button
                        type="button"
                        className={BTN}
                        onClick={handleFitBottom}
                        title="只贴地 (Alt+Q)"
                      >
                        贴地
                      </button>
                      <button
                        type="button"
                        className={BTN}
                        onClick={handleFitYaw}
                        title="贴朝向(实验):点云稀疏时主轴可能反转 180°"
                      >
                        朝向⚗
                      </button>
                    </div>
                  )}
                  {PSR_GROUPS.map((g) => (
                    <div key={g.label}>
                      <div className={EDIT_GROUP_LABEL_ROW}>
                        <span className={EDIT_GROUP_LABEL}>{g.label}</span>
                        {g.reset && selectedPsrEditable && (
                          <button
                            type="button"
                            className={RESET_BTN}
                            onClick={handleResetRotation}
                            title="把偏航/俯仰/翻滚全部归零"
                          >
                            归零
                          </button>
                        )}
                      </div>
                      <div className={EDIT_ROW}>
                        {g.keys.map((k) => (
                          <input
                            key={k}
                            type="number"
                            step={g.step}
                            min={g.min}
                            value={form[k]}
                            aria-label={k}
                            disabled={!selectedPsrEditable}
                            onChange={(e) => handleField(k, e.target.value)}
                            onBlur={() => handleFieldBlur(k)}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                  {!multiBoxSelected && (
                    <AttributeForm
                      schema={boxAttributeSchema}
                      className={selectedClass ?? ""}
                      attributes={selectedAnn?.attributes ?? {}}
                      readOnly={!selectedEditable}
                      onChange={handleChangeAttributes}
                    />
                  )}
                </div>
              )}
            </div>,
          )}

        {selectedPointMask &&
          selectedAnn &&
          renderPsrPanel(
            <div
              ref={editPanelRef}
              className={editPanelClass}
              data-testid="three-d-selection-panel"
              data-pet-linked={petLinked || undefined}
            >
              <div
                className={
                  petLinked
                    ? psrDragging
                      ? `${EDIT_TITLE} cursor-grabbing`
                      : `${EDIT_TITLE} cursor-grab`
                    : EDIT_TITLE
                }
                onPointerDown={petLinked ? onPsrHeaderPointerDown : undefined}
                data-testid={petLinked ? "three-d-selection-panel-handle" : undefined}
              >
                {petLinked && <Icon name="move" size={12} className={DRAG_HINT} />}
                {pointMaskClasses.length > 0 ? (
                  <select
                    className={CLASS_SELECT}
                    value={selectedClass ?? ""}
                    aria-label="分割类别"
                    disabled={!selectedPointMaskEditable}
                    onChange={(e) => handleChangeClass(e.target.value)}
                  >
                    {selectedClass && !pointMaskClasses.includes(selectedClass) && (
                      <option value={selectedClass}>{selectedClass}</option>
                    )}
                    {pointMaskClasses.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span>点云分割 · {selectedClass ?? ""}</span>
                )}
                {!readOnly && (
                  <button
                    type="button"
                    className={selectedLocked ? `${LOCK_BTN} ${LOCK_BTN_ON}` : LOCK_BTN}
                    aria-pressed={selectedLocked}
                    onClick={handleToggleLock}
                  >
                    {selectedLocked ? "已锁定" : "锁定"}
                  </button>
                )}
              </div>
              <div className={EDIT_GROUP_LABEL}>
                {selectedPointMask.point_indices.length.toLocaleString()} 点 · P 后圈选加点,Alt
                圈选减点
              </div>
              {!readOnly && selectedPointMaskEditable && (
                <button
                  type="button"
                  className={DELETE_BTN}
                  onClick={() => {
                    void history.commit([{ kind: "delete", annotation: selectedAnn }]);
                    onSelectBox(null);
                  }}
                >
                  删除分割
                </button>
              )}
            </div>,
          )}

        {dockLayout?.targets["tri-view"] &&
          createPortal(
            <div className="flex h-full min-h-0 flex-col">
              <TriViewPanel
                scene={sceneRef.current}
                selected={triSelected}
                editable={!!triSelected && selectedPsrEditable}
                active={dockLayout.panelVisible["tri-view"]}
                layoutKey={dockLayout.layoutKey}
                getVisibleRegions={dockLayout.getVisibleRegions}
                zoomByView={triZoomByView}
                onZoomChange={handleTriZoomChange}
                onEditPsr={handleEditPsr}
              />
            </div>,
            dockLayout.targets["tri-view"],
          )}
        {dockLayout?.targets["camera-view"] &&
          createPortal(
            <CameraDockPanel
              cameras={cameras}
              boxes={boxes}
              highlightedIds={highlightedIds}
              onSelectBox={onSelectBox}
              bestRole={bestCameraRole}
              pointPositions={pointPositions}
              showDepth={depthOn}
              onEnlarge={setEnlargedRole}
              visible={cameraPresentation === "docked" && dockLayout.panelVisible["camera-view"]}
              loading={isLoading}
              error={error ? "相机数据加载失败" : null}
              resetKey={cameraResetKey}
            />,
            dockLayout.targets["camera-view"],
          )}

        {cameraPresentation === "floating" && (dockLayout?.cameraVisible ?? true) && (
          <div className={CAMERA_LAYER} data-testid="camera-panel-layer">
            {cameraGroups.map(([anchor, cams]) => (
              <div key={anchor} className={`${CAM_GROUP} ${ANCHOR_CLASS[anchor]}`}>
                {cams.map((cam, index) => (
                  <FloatingCameraPanel
                    key={cam.role}
                    role={cam.role}
                    name={cam.name}
                    imageUrl={cam.image_url}
                    calibration={cam.calibration}
                    boxes={boxes}
                    highlightedIds={highlightedIds}
                    onSelectBox={onSelectBox}
                    bestForSelected={cam.role === bestCameraRole}
                    pointPositions={pointPositions}
                    showDepth={depthOn}
                    onEnlarge={() => setEnlargedRole(cam.role)}
                    autoCollapsed={autoCollapseCameras || index >= CAMERA_STACK_VISIBLE}
                    dragBounds={qualitySafeFloatBounds}
                    position={
                      cameraPanels[cam.role]?.x != null && cameraPanels[cam.role]?.y != null
                        ? {
                            x: cameraPanels[cam.role]!.x!,
                            y: cameraPanels[cam.role]!.y!,
                          }
                        : null
                    }
                    collapsed={cameraPanels[cam.role]?.collapsed}
                    onPositionChange={handleCameraPanelPosition}
                    onCollapsedChange={handleCameraPanelCollapsed}
                    onDockAll={
                      dockLayout
                        ? () => dockLayout.commands.setCameraPresentation("docked")
                        : undefined
                    }
                    layoutDisabled={dockLayout?.disabled}
                  />
                ))}
              </div>
            ))}
          </div>
        )}

        {enlargedCam &&
          !calibrationSheetOpen &&
          createPortal(
            <div
              ref={cameraModalRef}
              className={CAM_MODAL}
              data-testid="camera-modal"
              onClick={() => setEnlargedRole(null)}
              role="presentation"
            >
              <div
                className={CAM_MODAL_BODY}
                data-testid="camera-modal-body"
                onClick={(e) => e.stopPropagation()}
                role="presentation"
              >
                <button
                  type="button"
                  className={CAM_MODAL_CLOSE}
                  onClick={() => setEnlargedRole(null)}
                >
                  关闭 ✕
                </button>
                <div className={CAM_MODAL_TOOL_GROUP}>
                  {!readOnly && enlargedCam.calibration && boxPlaceClass && (
                    <button
                      type="button"
                      className={
                        seedMode ? `${CAM_MODAL_SEED} ${CAM_MODAL_SEED_ACTIVE}` : CAM_MODAL_SEED
                      }
                      onClick={() => {
                        setBoxCreationIssue(null);
                        setManualBboxMode(false);
                        setSeedMode((v) => !v);
                      }}
                      aria-pressed={seedMode}
                      title="在相机图上拖一个 2D 框,自动在 3D 里生成框(视锥选点拟合)"
                    >
                      {seedMode
                        ? boxCreationSaving
                          ? `正在保存 ${boxPlaceClass}`
                          : boxCreationIssue
                            ? `保存失败 · 重试 ${boxPlaceClass}`
                            : `连续种框 · ${boxPlaceClass} · 拖矩形`
                        : "种框 ⊹"}
                    </button>
                  )}
                  {enlargedCalibrationSource?.calibration &&
                    enlargedCalibrationSource.calibration_revision &&
                    enlargedCalibrationSource.calibration_digest && (
                      <button
                        type="button"
                        className={CAM_MODAL_SEED}
                        onClick={() => {
                          setSeedMode(false);
                          setManualBboxMode(false);
                          setCalibrationSheetOpen(true);
                        }}
                      >
                        标定 · R{enlargedCalibrationSource.calibration_revision}
                      </button>
                    )}
                </div>
                {cameras.length > 1 && (
                  <>
                    <button
                      type="button"
                      className={`${CAM_MODAL_SWITCH} ${CAM_MODAL_PREV}`}
                      onClick={() => cycleEnlargedCamera(-1)}
                      title="上一视角"
                    >
                      ‹
                    </button>
                    <button
                      type="button"
                      className={`${CAM_MODAL_SWITCH} ${CAM_MODAL_NEXT}`}
                      onClick={() => cycleEnlargedCamera(1)}
                      title="下一视角"
                    >
                      ›
                    </button>
                  </>
                )}
                {selectedAnn?.scene_track_id && enlargedCam.calibration && (
                  <div className={CAM_MODAL_MEMBER_BAR}>
                    <div className={CAM_MODAL_MEMBER_CARD}>
                      <span className="font-medium">多相机 2D 成员</span>
                      {cameraMembers.query.isLoading ? (
                        <span className="text-muted-foreground">读取中…</span>
                      ) : cameraMembers.query.isError ? (
                        <span
                          role="alert"
                          className="text-status-caution"
                          title={
                            cameraMembers.query.error instanceof Error
                              ? cameraMembers.query.error.message
                              : "请检查相机标定与图像尺寸"
                          }
                        >
                          2D 成员不可用：
                          <CopyableError
                            message={
                              cameraMembers.query.error instanceof Error
                                ? cameraMembers.query.error.message
                                : "上下文读取失败"
                            }
                          />
                        </span>
                      ) : (
                        <>
                          <button
                            type="button"
                            className={manualBboxMode ? `${BTN} ${BTN_ACTIVE}` : BTN}
                            disabled={
                              readOnly || cameraMembers.busy || !cameraMembers.data?.track_revision
                            }
                            onClick={() => {
                              setSeedMode(false);
                              setManualBboxMode((value) => !value);
                            }}
                          >
                            {manualBboxMode
                              ? "退出 2D 编辑"
                              : enlargedCameraMember
                                ? "编辑 2D 框"
                                : "创建 2D 框"}
                          </button>
                          {!enlargedCameraMember && cameraMembers.data?.projected_bbox && (
                            <button
                              type="button"
                              className={BTN}
                              disabled={readOnly || cameraMembers.busy}
                              onClick={() =>
                                void handleManualBboxCommit(cameraMembers.data!.projected_bbox!)
                              }
                            >
                              采用当前投影
                            </button>
                          )}
                          {enlargedCameraMember && (
                            <>
                              <select
                                className={SELECT_CTL}
                                value={enlargedCameraMember.visibility}
                                disabled={readOnly || cameraMembers.busy}
                                aria-label="2D 成员可见性"
                                onChange={(event) =>
                                  void handleManualVisibilityChange(
                                    event.target.value as
                                      | "visible"
                                      | "occluded"
                                      | "truncated"
                                      | "unknown",
                                  )
                                }
                              >
                                <option value="visible">可见</option>
                                <option value="occluded">遮挡</option>
                                <option value="truncated">截断</option>
                                <option value="unknown">未知</option>
                              </select>
                              {enlargedCameraMember.residual && (
                                <span className="text-muted-foreground">
                                  IoU {enlargedCameraMember.residual.iou.toFixed(2)} · 边差{" "}
                                  {enlargedCameraMember.residual.max_edge_residual_px.toFixed(1)}px
                                </span>
                              )}
                              {enlargedCameraMember.relation_status === "stale" && (
                                <button
                                  type="button"
                                  className={BTN}
                                  disabled={readOnly || cameraMembers.busy}
                                  onClick={() =>
                                    void handleManualBboxCommit(enlargedCameraMember.bbox)
                                  }
                                >
                                  标定已变更 · 重新确认
                                </button>
                              )}
                              <button
                                type="button"
                                className={DELETE_BTN}
                                disabled={readOnly || cameraMembers.busy}
                                onClick={() => void handleDeleteManualBbox()}
                              >
                                删除 2D 框
                              </button>
                            </>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                )}
                <CameraProjectionView
                  name={enlargedCam.name}
                  imageUrl={enlargedCam.image_url}
                  calibration={enlargedCam.calibration}
                  boxes={boxes}
                  highlightedIds={highlightedIds}
                  onSelectBox={onSelectBox}
                  bestForSelected={enlargedCam.role === bestCameraRole}
                  pointPositions={pointPositions}
                  showDepth={depthOn}
                  seedMode={seedMode && !manualBboxMode}
                  interactionDisabled={readOnly || boxCreationSaving || cameraMembers.busy}
                  onSeedBox={handleSeedBox}
                  editableBox={
                    threeDTool === "select" && selectedPsrEditable && !seedMode && !manualBboxMode
                      ? selectedBox
                      : null
                  }
                  onEditPsr={handleEditPsr}
                  onCancelEditPsr={handleCancelCameraEdit}
                  onEditError={handleCameraEditError}
                  manualBbox={enlargedCameraMember?.bbox ?? null}
                  manualBboxMode={manualBboxMode}
                  manualBboxStale={enlargedCameraMember?.relation_status === "stale"}
                  onManualBboxCommit={(bbox) => void handleManualBboxCommit(bbox)}
                  expanded
                />
              </div>
            </div>,
            document.body,
          )}
        {qualityPanelOpen && project && manifest?.scene_id && (
          <PointCloudQualityPanel
            projectId={project.id}
            sceneId={manifest.scene_id}
            taskId={taskId ?? ""}
            canScanScene={qualityCanScanScene}
            canGovern={qualityCanScanScene}
            qualityConfig={
              project.point_cloud_quality_config as PointCloudQualityConfig | undefined
            }
            classes={boxClasses}
            onClose={() => setQualityPanelOpen(false)}
            onLocate={handleLocateQualityIssue}
          />
        )}
        {enlargedCalibrationSource?.calibration &&
          enlargedCalibrationSource.calibration_revision &&
          enlargedCalibrationSource.calibration_digest && (
            <SensorCalibrationSheet
              open={calibrationSheetOpen}
              onOpenChange={setCalibrationSheetOpen}
              taskId={taskId}
              projectId={task?.project_id ?? null}
              cameraName={enlargedCalibrationSource.name}
              cameraRole={enlargedCalibrationSource.role}
              calibration={enlargedCalibrationSource.calibration}
              revision={enlargedCalibrationSource.calibration_revision}
              digest={enlargedCalibrationSource.calibration_digest}
              canManage={!readOnly && canManageCalibration}
            />
          )}
      </div>
      <SceneTimeline
        taskId={taskId}
        trackId={playbackActive ? (playbackTrackId ?? selectedTrackId) : selectedTrackId}
        frameState={frameState}
        onRetryFrame={() => {
          void manifestQuery.refetch();
          void annotationsQuery.refetch();
          retryLoad();
        }}
        playbackActive={playbackActive}
        onPlaybackActiveChange={onPlaybackActiveChange}
        playbackBlockedReason={playbackBlockedReason}
        onPlaybackTrackChange={setPlaybackTrackId}
        visible={canvasVisible}
        prefetchDepthRasters={rendererStatus?.actualBackend === "webgpu"}
        prefetchDecimateThreshold={performanceConfig.pcdDecimate}
        selectedAnnotationIds={boxes
          .filter((box) => selectedIdSet.has(box.id))
          .map((box) => box.id)}
        boxCount={boxes.length}
        readOnly={readOnly}
        onNavigateFrame={handleTimelineNavigate}
        qualityMarkers={qualityMarkers}
        qualityIssueCount={qualityIssuesQuery.data?.total ?? 0}
        onOpenQuality={qualityAllowed ? () => setQualityPanelOpen(true) : undefined}
      />
    </div>
  );
}

export default ThreeDWorkbench;
