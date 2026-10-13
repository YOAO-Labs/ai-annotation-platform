/**
 * v0.13.2 · 点云查看器(只读 MVP)。
 *
 * 拉 point-cloud manifest → 用裸 Three.js(PointCloudScene)渲染主点云 + OrbitControls,
 * 旁边平铺各相机图(只读,不画投影框 —— 投影联动是 v0.13.4)。与 Konva 2D 工作台双栈隔离。
 */
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
import {
  centralRay,
  depthGate,
  gatherPoints,
  selectPointsInRect,
  type SeedRect,
} from "./geometry/frustum";
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

// v0.17.6 · Tailwind class constants (was ThreeDWorkbench.module.css).
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
  /** v0.13.3 · 锁定 task / viewer 角色时只读:不放置 / 不编辑 / 无 gizmo,仅看 + 选中查看数值。 */
  readOnly?: boolean;
  playbackActive?: boolean;
  onPlaybackActiveChange?: (active: boolean) => void;
  playbackBlockedReason?: string | null;
  /** v0.13.3-5 · 壳层共享选中态(与标注列表 / 右栏面板同一份),驱动选中高亮 / gizmo / 数值面板。 */
  selectedId: string | null;
  selectedIds: string[];
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  /** v0.13.3-5 · 壳层激活类别(左栏 ClassPalette 选);放置新框的 class_name 取它。 */
  activeClass: string;
  /** v0.13.3-5 · 3D 工具态(左栏 ToolDock 选,壳层共享):select 拾取 / box 点地面放置。 */
  threeDTool: ThreeDTool;
  onSetThreeDTool: (t: ThreeDTool) => void;
  /** v0.14.1 · 跨帧目标延续 (Shift+→ / Shift+←): 把选中框 propagate 到同 scene 邻帧。 */
  onCrossFramePropagate: (direction: "next" | "prev") => void;
  /** v0.15.1 · 批量延续 (Ctrl+Shift+→/←): 当前帧全部 box_3d 延续到邻帧。 */
  onCrossFramePropagateBatch: (direction: "next" | "prev") => void;
  /** v0.15.1 · 把选中框延续到 scene 内指定帧(插值工作流建链)。 */
  onCrossFramePropagateToTask: (targetTaskId: string, targetFrameIndex: number) => void;
  /** v0.15.1 · 区间插值填充(当前 task 为起点帧)。v0.21.2 · 按 track_id 认链。 */
  onCrossFrameInterpolate: (trackId: string, toTaskId: string) => void;
  /** Scene 时间轴导航，服从壳层的未保存保护。 */
  onNavigateSceneFrame: (targetTaskId: string) => Promise<boolean>;
  /** v0.13.10 · 右栏避让与三视图浮窗持久化。 */
  rightSidebarOpen: boolean;
  rightSidebarWidth: number;
  triViewFloat: TriViewFloatState;
  /** v0.15.x · 悬浮相机面板位置 + 折叠态(按相机 role 分桶,迁自旧 localStorage)。 */
  cameraPanels: Record<string, CameraPanelState>;
  pointcloudCamera: PointcloudCameraState | null;
  onWorkbenchLayoutChange: (patch: WorkbenchLayoutPatch) => void;
  workbenchCommon: WorkbenchCommonPreferences;
  workbenchPointcloud: WorkbenchPointcloudPreferences;
  workbenchConfigLoaded: boolean;
  onWorkbenchConfigChange: (patch: WorkbenchConfigPatch) => void;
  onWorkbenchConfigUpdate: (patch: WorkbenchConfigPatch) => Promise<void>;
  box3dDefaultSize?: [number, number, number] | null;
  /** 桌宠开启时，让 3D 选中信息栏与桌宠共用拖动锚点。 */
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
  // Capture before renderer and Shell shortcuts: the first editing gesture only pauses.
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
  useEffect(() => retainPointCloudComputeSession(), []);
  const manifestQuery = usePointCloudManifest(taskId, true);
  const { data: manifest, isLoading, error } = manifestQuery;
  const retainedManifestRef = useRef(manifest);
  if (manifest) retainedManifestRef.current = manifest;
  const displayManifest = manifest ?? retainedManifestRef.current;
  const pushToast = useToastStore((st) => st.push);
  const userId = useAuthStore((s) => s.user?.id ?? null);
  const userRole = useAuthStore((s) => s.user?.role ?? null);
  // v0.13.11 · dataset 声明的 lidar 系约定;前端把点云 positions + 相机 extrinsic 一次性
  // 旋转到 ISO 8855 (+X 前 / +Y 左 / +Z 上),上层几何代码继续锁死 ISO。null / 缺省 = iso_8855。
  const axisConvention: LidarAxisConvention = manifest?.axis_convention ?? "iso_8855";
  // v0.14.0 · scene 字段(跨 task 帧序列地基)仅做调试透出,本期 UX 不消费;
  // v0.14.1 会上 useFrameNeighbors hook + Shift+→ propagate 等。
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
  // 场景实例 ref —— 壳层各交互 handler 共用;生命周期由 usePointCloudScene 管理。
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
  // v0.15.17 · 邻帧框叠加范围:selected=仅选中对象 group(现状);all=不选对象也叠全部邻帧框。
  const overlayScope = workbenchCommon.crossFrameOverlayScope === "all" ? "all" : "selected";
  // v0.15.19 · 邻帧点云叠加开关 + 独立帧数(点云比框重,前后各 ≤3 帧)。
  const pointOverlayOn = workbenchPointcloud.neighborPointOverlay;
  const pointOverlayFrameK =
    workbenchPointcloud.neighborPointOverlayK === 2 ||
    workbenchPointcloud.neighborPointOverlayK === 3
      ? workbenchPointcloud.neighborPointOverlayK
      : 1;
  const pointOverlayK = pointOverlayOn ? pointOverlayFrameK : 0;
  // v0.15.22 · §C.8-B / v0.15.23 · §C.8-A 邻帧点云动态点处理:
  // cull=剔除落在当前帧 box 内的邻帧点;align=逐目标把邻帧点搬到当前帧位置(无拖影)。
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
  // v0.13.7 · 放大查看的相机 role(L3);null = 无放大。点⛶开,ESC/遮罩/关闭钮收。
  const [enlargedRole, setEnlargedRole] = useState<string | null>(null);
  const [calibrationSheetOpen, setCalibrationSheetOpen] = useState(false);
  // v0.15.24 · 放大相机模态里的「种框」模式:拖 2D 框 → 视锥选点 → 拟合 box_3d。仅放大视图启用。
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
  // 选中态来自壳层(selectedId / onSelectBox props),与标注列表 / 右栏面板共享同一份。

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
  // scene 的拖拽回调只设一次,用 ref 取最新 mutate,避免闭包旧值。
  const updateMutateRef = useRef(updateAnnotation.mutate);
  updateMutateRef.current = updateAnnotation.mutate;
  const historyPushRef = useRef(history.push);
  historyPushRef.current = history.push;

  // 创建新 3D 标注需要对应工具单位的类别(后端按 tool_bindings 校验 class_name)。
  const { data: task } = useTask(taskId ?? "");
  const { data: project } = useProject(task?.project_id ?? "");
  // 点云质量由项目能力门控（与后端 review.write 授权一致）：项目负责人/超级管理员
  // 经 is_manager 放行，普通项目质检员经 review.write 放行；全局角色不再是授权来源。
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

  // 工具态 / 待放置类别全来自壳层(ToolDock 的 threeDTool + 左栏 ClassPalette 的 activeClass);
  // canPlace 兜底:只读 / 未配类别时不允许放置。class 在 activeClass 不在类集合时回落首个。
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
  // 相机面板位置/折叠落库与窄屏自动折叠，
  // 整簇抽到 useCameraPanels;viewportWrapRef 由壳层共用故传入。
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

  // 选中框的 PSR 编辑表单(字符串值,允许清空 / 中间态如 "-" / "1.";解析有效时才提交)。
  // PATCH 防抖 250ms;yaw 以度展示。
  const [form, setForm] = useState<Record<PsrField, string> | null>(null);

  // 本地草稿 PSR：三视图或主视图 gizmo 拖拽时覆盖选中框，实时同步全部投影视图；提交后清空。
  const [draftPsr, setDraftPsr] = useState<{
    id: string;
    psr: Psr;
    source: "gizmo" | "triview";
  } | null>(null);
  const sceneDraftPsr = draftPsr?.source === "triview" ? draftPsr : null;

  // 标注里的 3D 框(geometry.type==="box_3d")→ 渲染层输入(PSR + 类别色 + 选中态 + 标签)。
  const sceneBoxes = useMemo<SceneBox[]>(() => {
    // 标签内容复用 common.labelContent 的 track 段(点云框是跨帧 track 语义):类别名恒显,
    // 轨迹号 / 属性按开关。state 后缀点云渲染层无逐帧态,留空。可见性走 labelVisibility 门控。
    const trackContent = workbenchCommon.labelContent.track;
    const labelVisibility = workbenchCommon.labelVisibility;
    // 轨迹号:当前帧内按 track_id 字典序派生 1..N,供 track 段的 id token 显示。
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
      if (a.is_hidden) continue; // 隐藏的框(列表 H 切换)不渲染,与 2D 画布同语义
      const g = a.geometry as {
        type?: string;
        center?: number[];
        size?: number[];
        rotation?: number[];
      };
      if (g?.type !== "box_3d" || !g.center || !g.size || !g.rotation) continue;
      // 三视图拖拽中:用本地草稿覆盖该框的 PSR(实时预览, 不发请求)。
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
        // 尺寸取绝对值兜底:历史/缩放翻转可能写入负 size,负值会让框翻转、且卡住数值面板
        // 的 size>0 提交校验。渲染与面板初始化统一按正尺寸。
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

  // 主 gizmo 已经直接改变 Three.js group；其草稿只覆盖依赖 React 的三视图/相机，不能再喂回
  // PointCloudScene，否则每次 objectChange 都会重挂 TransformControls 并把一次拖动拆成多次提交。
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
  // v0.14.1 · 给 Shift+→ keydown 闭包读最新选中 annotation 的几何类型(避免频繁重绑监听)。
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
  // 单框锁定(列表 L 切换)→ 不可编辑(无 gizmo / 面板禁用 / 不可删),但仍可选中查看。
  const selectedLocked = !!selectedAnn?.is_locked;
  const selectedHidden = !!selectedAnn?.is_hidden;
  // 可编辑 = 任务级非只读 且 该框未锁定。
  const selectedEditable = !readOnly && !selectedLocked;
  const selectedPsrEditable = selectedEditable && !multiBoxSelected;
  const selectedPointMaskEditable = selectedEditable && !!selectedPointMask && !multiBoxSelected;

  // gizmo 拖拽中只更新本地 PSR，三视图与相机投影跟随主框实时刷新。
  const handleTransformPreview = useCallback((id: string, psr: BoxPsr) => {
    if (!writeAllowedRef.current) return;
    setForm(psrToForm(psr));
    setDraftPsr({ id, psr, source: "gizmo" });
  }, []);

  // gizmo 拖拽结束:清理预览并回写 PSR 表单 + 几何 PATCH 持久化。
  // 作为回调注入 usePointCloudScene —— form / mutate / history 仍由本壳组件持有,边界干净。
  const handleTransformCommit = useCallback((id: string, psr: BoxPsr) => {
    if (!writeAllowedRef.current) return;
    setDraftPsr(null);
    setForm(psrToForm(psr));
    const ann = annotationsRef.current?.find((a) => a.id === id);
    const geometry = boxGeometryFromPsr(
      psr,
      geometryConvention(ann?.geometry, axisConventionRef.current),
    );
    updateMutateRef.current({ annotationId: id, payload: { geometry } });
    if (ann?.geometry?.type === "box_3d") {
      historyPushRef.current({
        kind: "update",
        annotationId: id,
        before: { geometry: ann.geometry },
        after: { geometry },
      });
    }
    // 依赖全为稳定 ref / setState / 纯函数 import,故空依赖。
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

  // Three.js 场景生命周期(实例化/销毁 · 偏好同步 · 点云加载 · 框图层 · gizmo 挂载 · W-E-R)。
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
    pointCloudUrl: displayManifest?.point_cloud_url,
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

  // v0.14.1 · Shift+→ / Shift+← 跨帧目标延续: 选中 box_3d 时 propagate 到同 scene
  // 邻帧并跳过去自动选中(orchestration 在壳层 onCrossFramePropagate)。
  useEffect(() => {
    if (readOnly) return;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      // v0.14.1 · 阻断按住 Shift+→ 的 auto-repeat: 否则连发多个 propagate POST,
      // 在目标帧造出共享同一新 track_id 的重复 annotation。
      if (e.repeat) return;
      if (!e.shiftKey || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      // 命中 Shift+左右(跨帧 propagate 组合)即抢占该按键: 即便后续因无选中 /
      // 非 box_3d 提前 return, 也要阻止壳层 arrowNudge 同键误处理(否则会把
      // NaN 写进 nudgeMap)。
      e.preventDefault();
      e.stopPropagation();
      const dir = e.key === "ArrowRight" ? "next" : "prev";
      // v0.15.1 · Ctrl+Shift+→/←: 批量延续当前帧全部 box_3d(不要求选中)。
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

  // 切任务回到选择工具，并让旧 task 的迟到创建响应失效。
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

  // 只在进入工具的那个事件周期清一次旧选择。持续建框成功后即使仍为 box，
  // 新框也必须保持选中，不能被 placing=true 的 effect 再次清空。
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

  // 只读或工具类别被撤销时立即安全退出，不保留看似可用的 armed 状态。
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

  // B/P/M 切工具；Enter 完成多边形或测量；V/Esc 回选择或取消当前测量草稿。
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

  // 选中目标切换时用其 PSR 初始化表单(编辑期间不被服务端回写覆盖,故仅依赖 selectedId)。
  useEffect(() => {
    setForm(selectedBox ? psrToForm(selectedBox) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // undo / redo 的乐观缓存更新也要回写面板；普通字段编辑仍不受服务端回写打断。
  useEffect(() => {
    if (!history.busy || draftPsr) return;
    setForm(selectedBox ? psrToForm(selectedBox) : null);
  }, [draftPsr, history.busy, selectedBox]);

  // v0.16.x 第 3 批 · PSR 数值字段防抖落库管线抽到 usePsrPatchPipeline(单一职责、
  // 单消费者 handleField、不碰共享 form/setForm);完整 usePsrEditor 因 form 被多处共享
  // 无法干净切分(见计划 §5/§7),此处仅"缩小范围"抽这条管线。
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

  // 失焦:该字段空 / 非法时从选中框当前值恢复,避免留下空字段。
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
      updateAnnotation.mutate({ annotationId, payload: after });
      history.push({ kind: "update", annotationId, before, after });
    },
    [history, updateAnnotation],
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
    for (const ann of targets) deleteAnnotation.mutate(ann.id);
    history.pushBatch(targets.map((annotation) => ({ kind: "delete", annotation })));
    onSelectBox(null);
  }, [readOnly, editableSelectedBoxAnnotations, deleteAnnotation, history, onSelectBox]);

  // 改选中框类别(3D 原生:面板下拉;2D 的画布锚定 popover 不适用 3D)。
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
        .map((ann) => {
          updateAnnotation.mutate({ annotationId: ann.id, payload: { class_name: cls } });
          return {
            kind: "update" as const,
            annotationId: ann.id,
            before: { class_name: ann.class_name },
            after: { class_name: cls },
          };
        });
      history.pushBatch(commands);
    },
    [
      readOnly,
      selectedPointMask,
      selectedId,
      selectedAnn,
      updateAnnotationWithHistory,
      editableSelectedBoxAnnotations,
      updateAnnotation,
      history,
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

  // v0.13.5 · 朝向归零:把三轴旋转复位为 [0,0,0](保留中心/尺寸),并同步表单。
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

  // v0.13.8 · 自动贴合:把选中框按点云 box-local AABB 收尺寸 / 贴地 / 朝向。
  // 共用 helper:拿当前点云 positions + 选中框 PSR → 跑 transform → 立即提交 + 同步表单。
  // 不走 schedulePatch 250ms 防抖(一键操作期望即时生效)。
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

  // Q (默认连击=收尺寸+贴地) / Shift+Q (仅收尺寸) / Alt+Q (仅贴地)。
  // 焦点在输入框时不拦截;未选中 / 不可编辑时跳过。Ctrl+Q 让给浏览器/系统。
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

  // v0.13.8 · Delete/Backspace 删选中框:全局 dispatchKey 通路在 3D 台实测未触发,
  // 故 3D 本地接管(同 useWorkbenchShellModel.threeDOwnedKeys 把这俩键交给本地)。
  // 焦点在输入框时不拦截(避免 PSR 数值面板里 Backspace 删字误删框)。
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

  // 锁定 / 解锁选中框(与列表 L 切换同源 is_locked;锁定后不可编辑,解锁需此按钮 / 列表)。
  const handleToggleLock = useCallback(() => {
    if (!writeAllowedRef.current) return;
    if (!selectedId) return;
    updateAnnotation.mutate({
      annotationId: selectedId,
      payload: { is_locked: !selectedLocked },
    });
  }, [selectedId, selectedLocked, updateAnnotation]);

  // v0.15.20 · 隐藏 / 显示选中框(与右栏列表同源 is_hidden;仅可见性,渲染侧读 a.is_hidden)。
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

  // 放置:点地面 → 默认尺寸框(落在地面上)→ 持久化 → 选中新框，工具继续 armed。
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

  // v0.13.9 · 框选画框 (frustum 选点): 在 box 工具下按住拖出屏幕矩形 → 选中投影落在矩形内的真实
  // 点 → 取其 world AABB 建框 (psrFromPoints)。用屏幕投影选点而非投地面平面 → 对物体高度/视角零
  // 视差 (SUSTechPOINTS 范式)。拖动 < 阈值退化为旧的「点击放置固定框」(向后兼容)。
  // 仍建议在俯视(BEV)下框, 框选体验最佳; 但本法不再依赖视角无视差性。
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

  // v0.15.24 · §Phase1 相机图「2D 框种 3D 框」:在放大相机图上拖矩形 → 该相机标定反算视锥选点
  // → depthGate 取最近簇 → psrFromPoints + fitYaw/fitBottom 拟合 → 落 box_3d 并选中微调。
  // 空簇(图上可见但无 lidar 返回)→ 沿中央射线放默认尺寸框 + 提示微调。唯一产物是 box_3d(2D 不落库)。
  const handleSeedBox = useCallback(
    (rect: SeedRect, calibration: SensorCalibration) => {
      const scene = sceneRef.current;
      if (!scene || !boxPlaceClass || boxCreationInFlightRef.current) return;
      const positions = scene.getPointPositions();
      if (!positions) {
        setBoxCreationIssue("点云尚未就绪，请稍后重试");
        return;
      }
      const gated = depthGate(selectPointsInRect(positions, rect, calibration));
      let psr: Psr;
      if (gated.length >= 3) {
        const cluster = gatherPoints(positions, gated);
        psr = fitBottom(cluster, fitYaw(cluster, psrFromPoints(cluster)));
      } else {
        // 空簇 / 点太少:沿矩形中心射线按固定估计深度放默认尺寸框(用户再微调)。
        const ray = centralRay(rect, calibration);
        const center: [number, number, number] = [
          ray.origin[0] + ray.direction[0] * SEED_FALLBACK_RANGE_M,
          ray.origin[1] + ray.direction[1] * SEED_FALLBACK_RANGE_M,
          ray.origin[2] + ray.direction[2] * SEED_FALLBACK_RANGE_M,
        ];
        psr = { center, size: defaultBoxSize, rotation: [0, 0, 0] };
        pushToast({ msg: "视锥内无点云,已按默认尺寸放置,请微调", kind: "" });
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

  // mousedown 落点(像素): click 时若位移超阈值判为「转视角拖拽」, 不改选中/不放置。
  const pointerDownRef = useRef<{ x: number; y: number } | null>(null);
  const selectionBeforeDoubleClickRef = useRef<string[]>([]);
  const DRAG_CLICK_TOL = 4; // px
  // v0.13.9 · 框选拖拽起点(client px)与屏上预览矩形(相对 viewportWrap px)。
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
    // 控件条、状态条和其他覆盖层都在 viewport DOM 内，事件会冒泡到这里。
    // 共享画布位于工作区底层，只接受主视图交互宿主（独立模式则为 canvas）的事件。
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
      // 框选: 禁 orbit, 记起点; 实际 move/up 走 window 监听(见下方 effect), 拖出视口也能收尾。
      sceneRef.current?.setBoxSelecting(true);
      boxSelectStartRef.current = { x: e.clientX, y: e.clientY };
      lassoPointsRef.current =
        pointMaskSelectMode === "lasso" ? [{ x: e.clientX, y: e.clientY }] : [];
      setIsBoxSelecting(true);
    }
  };

  // v0.13.9 · 框选期 window 级 move/up: move 画预览矩形; up 收尾(拖动大 → 框选, 否则 → 点击放置)。
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
        if (placing) handlePlace(e.clientX, e.clientY); // 退化为点击放置
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
    // v0.13.12 · 拖拽期取消: Escape / 右键 → 丢弃这一笔(清起点+预览+scene 状态),
    // 避免 preview 矩形挂着、或落进 up 收尾的全 false 分支被静默丢弃。
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
    // 拖拽 gizmo 结束的 click 不应改选中。
    if (sceneRef.current?.shouldIgnoreClick()) return;
    // 放置/框选已全程由 mousedown→window mouseup 接管, click 不再处理放置。
    if (drawingSelection) {
      pointerDownRef.current = null;
      return;
    }
    // OrbitControls 转视角拖拽松手也会触发 click: 位移超阈值视为拖拽, 保持当前选中。
    const down = pointerDownRef.current;
    pointerDownRef.current = null;
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_CLICK_TOL) return;
    onSelectBox(sceneRef.current?.pickBox(e.clientX, e.clientY) ?? null, { shift: e.shiftKey });
  };

  // ── v0.15.20 · 画布右键菜单(命中框/空白分流;右键拖动=相机 pan 则抑制)────────────
  const contextMenu = useCanvasContextMenu();
  const [ctxTargetId, setCtxTargetId] = useState<string | null>(null);
  const [classPickerAnchor, setClassPickerAnchor] = useState<{ left: number; top: number } | null>(
    null,
  );
  const [framePicker, setFramePicker] = useState<{
    mode: FramePickerMode;
    anchor: { left: number; top: number };
  } | null>(null);

  // v0.15.21 · 选中框 PSR 面板:渐进展开 + 整体拖动,展开态与位置偏移按用户记忆(localStorage)。
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
    // 右键拖动 = OrbitControls 相机平移, 抑制菜单; 右键点击才弹(与 click 判定同阈值)。
    const down = pointerDownRef.current;
    pointerDownRef.current = null;
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_CLICK_TOL) {
      closeContextMenu();
      return;
    }
    // 放置/框选进行中右键 = 取消这一笔(window 级 cancel 处理), 不弹菜单。
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

  // v0.13.11 · 相机列表的 extrinsic 在 hook 出口处归一化,下游 (frontCameraForward /
  // cameraAnchor / projectPoints / loadCameraSample) 无感知 convention。
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
  // v0.13.7 · 相机按物理朝向分组(悬浮环绕):每个 anchor 一个定位容器,同朝向沿边堆叠。
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
    // 等待决定期间成员/轨迹修订可能已变化;重验后仍以乐观锁提交,不匹配由服务端拒绝。
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
  // v0.13.7 · resetView 默认视向跟随 front 相机光轴(健壮于任意 lidar 前向约定)。
  // loadPcd 的首次 frameView 在 await fetch 之后异步触发,本同步 effect 必先于其设好前方。
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const fwd = frontCameraForward(cameras);
    scene.setViewForward(fwd?.[0] ?? 0, fwd?.[1] ?? 1);
  }, [cameras]);
  // v0.13.7 · 放大浮层:ESC 关闭(v0.15.24:种框模式下 ESC 先退出种框,再次 ESC 才关浮层)。
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
  // v0.15.24 · 关闭放大浮层时复位种框模式(种框仅在放大视图内有意义)。
  useEffect(() => {
    if (!enlargedRole) {
      setSeedMode(false);
      setCalibrationSheetOpen(false);
    }
  }, [enlargedRole]);
  // v0.13.6 · 点云坐标(载帧后稳定);供相机视图建深度栅格。stats 变化即点云换帧。
  const pointPositions = useMemo(
    () => (stats ? (sceneRef.current?.getPointPositions() ?? null) : null),
    [stats],
  );

  // Legacy 仍由持久 Worker 生成颜色 attribute；实验 renderer 直接采样 ImageBitmap texture，
  // 不执行 Canvas getImageData / 逐点 RGB 回传。两条路径都先显示高度色，不再隐藏点云。
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
          if (controller.signal.aborted) return;
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
        if (controller.signal.aborted || samples.length === 0) return;
        const colors = await colorizePointsAsync(positions, scene.getBaseColors(), samples, {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          colorizedRawRef.current = colors;
          adjustedColorBufferRef.current = null;
          scene.setPointColors(
            isNeutralAdjust(colorAdjust) ? colors : adjustColors(colors, colorAdjust),
          );
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          console.warn("[pointcloud-colorize] failed; showing height colors", error);
          scene.setCameraTextureColorization(null);
          scene.setPointColors(null);
        }
      } finally {
        if (!controller.signal.aborted) {
          scene.setPointCloudVisible(true);
          setColorizing(false);
          markPointCloudPaint(
            "camera-color-ready",
            pointCloudUrl,
            () => !controller.signal.aborted,
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
    // colorAdjust changes are handled by the lightweight remap effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
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

  // v0.13.4 · 跨模态高亮集合:选中框 + 同 track_id 成员。3D 主视图仍按 selected 单框高亮,
  // overlay 按本集合高亮(为未来同链 2D 框成员预留;孤立框 track_id 为空时退化为仅选中本身)。
  // v0.21.2 · ADR-0045 · 跨帧链按 track_id 认同一对象 (原 group_id 高位段)。
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

  // v0.15.18 · 框叠加(overlayK)或点云叠加(pointOverlayK)任一开启都要邻帧 + 轨迹;
  // 取两者最大帧数拉取,各自再按需切片(框用 overlayK,点云用 pointOverlayK),互不放大。
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
  // v0.15.17 · 批量端点拉邻帧标注。scope=selected 需选中对象(传 track_id 服务端过滤);
  // scope=all 不依赖选中(track_id=null,回全部框)。
  const boxAnnotationOverlayEnabled =
    overlayK > 0 && (overlayScope === "all" || selectedTrackId != null);
  const overlayTrackId = overlayScope === "all" ? null : selectedTrackId;
  const { byTask: neighborAnnsByTask } = useNeighborAnnotations(
    overlayK > 0 ? taskId : null,
    overlayK,
    overlayTrackId,
    boxAnnotationOverlayEnabled,
  );
  // v0.15.1 · overlay ego 对齐: 邻帧框经 trajectory 变换到当前帧 ego 系再叠加,
  // 静止物参考框与当前帧重合。无轨迹 scene → poseByFrame=null,退回原样叠加。
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
    // scope=selected:必须选中对象;scope=all:不选也叠全部邻帧框。
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
        // scope=all 且有选中对象:非选中 track 的邻帧框弱化(dim),突出当前对象轨迹。
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

  // v0.15.18 · 邻帧点云叠加。需 scene + ego 轨迹(无轨迹直接叠会乱,故 gate on poseByFrame)。
  const pointOverlayActive = pointOverlayK > 0 && !!neighborsData?.scene_id && !!poseByFrame;
  const pointNeighbors = useMemo(() => {
    if (!pointOverlayActive || !neighborsData) return [];
    return [
      ...(neighborsData.prev ?? []).slice(0, pointOverlayK),
      ...(neighborsData.next ?? []).slice(0, pointOverlayK),
    ].map((n) => ({ taskId: n.task_id, frameIndex: n.frame_index }));
  }, [pointOverlayActive, neighborsData, pointOverlayK]);
  // v0.15.23 · §C.8-A align 模式:把落在邻帧 box 内的点按目标位姿搬到当前帧位置(逐目标
  // 补偿,真·消除拖影)。需邻帧「全部」框(按 track_id 与当前帧框配对),独立于框叠加
  // (overlayK)且 scope 恒为 all;仅 align 时拉取,避免无谓请求。
  const alignActive = pointOverlayActive && neighborPointCull === "align";
  const { byTask: alignAnnsByTask } = useNeighborAnnotations(
    alignActive ? taskId : null,
    pointOverlayK,
    null,
    alignActive,
  );
  // 当前帧 box:track_id → PSR(逐目标搬运的配对目标)。
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
  // 邻帧 box(原始邻帧 ego 系 PSR + track_id),按 task 分组;不预对齐(搬运矩阵自带 ego 补偿)。
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
  // 邻帧点云下采样目标:当前帧抽稀阈值的 1/8,上限 8 万(邻帧仅作参考,远低于当前帧)。
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
    // v0.15.22 · cull:把对齐到当前帧后落在 tracked box 内的邻帧点剔除,只叠静止背景。
    // v0.15.23 · align:落在邻帧 box 内的点按目标位姿搬到当前帧位置(逐目标补偿,无拖影);
    //   搬运后点已是当前帧 ego 系坐标,渲染走 identity 矩阵(其余背景点仍 ego 对齐)。
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
          renderMatrix = IDENTITY_MATRIX; // 点已预变换到当前帧 ego 系
        } else if (cullOn) {
          const res = cullPointsInBoxes(positions, matrix, boxes);
          positions = res.kept;
          culledTotal += res.culledCount;
        }
        // 前/后帧分色 + 按帧距淡出(视觉缓解动态拖影)。
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

  // v0.13.4 · 选中框被哪些相机看到(可见角点数 > 0),按可见角点数降序;首个 = 最正对。
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
  // v0.13.5 · 三视图拖边/角回写: 拖拽中 (commit=false) 只更新本地草稿 (实时四方同步);
  // 松手 (commit=true) 走与 gizmo 同一条 PATCH 管线持久化, 并清草稿 (乐观更新已写入缓存)。
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

  // v0.17.6 · useElementStyle for dynamic CSS custom properties (was style={} JSX attribute).
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
  const frameError = error || annotationsQuery.error || loadError || rendererError;
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

        {/* v0.13.9 · 框选预览矩形(地面 footprint), 仅拖拽期出现, 不拦事件。 */}
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

        {/* 控件浮条 */}
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

        {/* 状态条 */}
        <div className={STATUS_BAR}>
          {colorizing && <span>相机上色…</span>}
          {isLoading && <span>加载 manifest…</span>}
          {pointCloudLoading && <span data-testid="pointcloud-loading">加载点云…</span>}
          {error && <span className={ERR}>manifest 加载失败</span>}
          {loadError && <span className={ERR}>点云加载失败: {loadError}</span>}
          {rendererError && (
            <span className={ERR} data-testid="pointcloud-renderer-error">
              Renderer 初始化失败: {rendererError}
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

        {/* 选中框 PSR 数值编辑面板：桌宠开启时与其共用锚点，否则保持右上浮层。 */}
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
                      {/* 当前类别若不在配置集合内(历史数据)仍可见,不丢选中项 */}
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
                  {/* v0.13.8 · 选中框自动贴合:Q 默认连击(收尺寸+贴地);
                    Shift+Q 仅收尺寸;Alt+Q 仅贴地;朝向(实验)仅按钮触发。 */}
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
                    deleteAnnotation.mutate(selectedAnn.id);
                    history.pushBatch([{ kind: "delete", annotation: selectedAnn }]);
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

        {/* v0.13.7 · 悬浮相机面板:按物理朝向贴主视图边缘,同朝向沿边堆叠。
            投影 overlay / 上色 / 深度命中沿用 CameraProjectionView,布局对其透明。 */}
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

        {/* v0.13.7 · 相机放大浮层(L3):点⛶弹大图,遮罩 / 关闭钮 / ESC 关闭。
            复用 CameraProjectionView(同 props,大尺寸),投影 / 上色 / 深度 overlay 一致。 */}
        {enlargedCam &&
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
                          {cameraMembers.query.error instanceof Error
                            ? cameraMembers.query.error.message
                            : "上下文读取失败"}
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
